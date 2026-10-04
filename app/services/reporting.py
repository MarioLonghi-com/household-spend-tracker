"""Flow reporting: what came in, what went out, and what it was for.

Every function here answers in **one currency at a time**. That is not a
convenience -- `insights.py` already carries the argument at length, and this
module is bound by the same rule:

> **No response ever sums two currencies.**

The ledger never converts. Currency lives on the account, there is no rate
anywhere in this schema, and `Household.base_currency` is a default for new
accounts rather than the roll-up its docstring claims. So currency here is a
**facet**: the caller names one, and gets a report about that one. There is no
"all currencies" total, not as a zero and not as a null, because the number
does not exist.

Where this differs from `insights.py` is the predicate. `insights` answers
questions about *rows* -- an agent asking "what did I spend at Mercadona" wants
the register's own filter vocabulary. A report answers a question about
*money entering and leaving the household*, and three kinds of row are in the
register without being that:

- **Transfer legs.** Moving money from Santander to the Visa is not spending
  it. Measured on the real ledger, counting them overstated outflow by 41.5%
  -- the largest single source of error available to this feature.
- **Opening balances.** What an account held when tracking started is not
  income. Left in, the first month of a freshly imported ledger reports the
  account balance as money earned.
- **Work expenses, and the payments that repaid them.** A hotel the employer
  pays back is not the household's spending, and the transfer that pays it
  back is not the household's income. Left in, both sides of the report are
  inflated by the same figure -- net is right, and every other number on the
  page is wrong, exactly as transfers made it before they were excluded.
  Decided for this build (G(i), 2026-09-25): money that comes back is not spending,
  and money still owed is a receivable rather than a cost, so an ``expected``
  row is out whether or not its payment has arrived. A ``written_off`` row is
  a cost after all and stays in, in its own month.
- **Rows in another currency.** Not excluded so much as never asked for; see
  above.

The first three are what :func:`flow_rows` removes, and it is written **once**
here so that every report composes the same predicate rather than the same
intentions. Two copies of this rule is how they come to disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from sqlalchemy import Boolean, Select, and_, func, literal_column, or_, select
from sqlalchemy.orm import Session, aliased

from ..models import (
    Account,
    Category,
    CategoryGroup,
    Payee,
    Receipt,
    ReimbursementState,
    SystemPayee,
    Transaction,
)

#: What an uncategorised row is called on a report.
#:
#: A first-class slice, never dropped and never folded into "Other". On the
#: real ledger 99.2% of outflow value carries no category, so a report that
#: hid it would be a report about 0.8% of the money. The category model is
#: already explicit that "an uncategorised row is a real state, not a missing
#: one"; this agrees with it.
UNCATEGORISED = "Uncategorised"


def flow_rows(household_id: str) -> Select:
    """Rows that represent money entering or leaving the household.

    Transfers move money *within* it and opening balances describe where it
    started, so neither is flow. Counting them costs 41.5% of outflow and a
    whole first month of phantom income respectively -- both measured against
    the real ledger.

    Every flow query composes this. **No balance query does**: an account
    balance is the sum of everything that ever touched the account, and a
    transfer leg and an opening balance are both entirely real there. There is
    no third option, and no query writes its own version.

    A transfer is identified by its own columns rather than by its payee, and
    both of them: `transfer_transaction_id` is the mirrored leg and
    `transfer_account_id` is the account it names. A leg whose counterpart was
    deleted keeps the second and loses the first -- `ondelete="SET NULL"` on
    the self-reference says so -- and it is still not spending.

    Work expenses leave too (decided for this build, G(i), 2026-09-25), on both sides:

    - A row flagged ``expected`` is out **whether or not it has been repaid**.
      Repaid, it was never the household's spending; not yet repaid, it is
      money owed to the household -- a receivable, not a cost. Either way it
      is not an expense, and waiting for the payment before removing it would
      make last month's figures move when the money arrives.
    - A row some other row's ``reimbursed_by_id`` points at is the payment,
      and money coming back is not income. Leaving it in would inflate income
      by exactly what the first rule took out of expense.
    - ``written_off`` **stays**. Work declined to pay, so it was spending
      after all, in the month it happened.

    An advance with nothing linked to it yet is still income here, because
    nothing yet says what it is. The moment an expense points at it, it
    leaves -- the same way an expense flagged today leaves a month already
    looked at.
    """
    return (
        select(Transaction)
        .where(Transaction.household_id == household_id)
        .where(_mostly(Transaction.transfer_transaction_id.is_(None)))
        .where(_mostly(Transaction.transfer_account_id.is_(None)))
        .where(_not_reimbursement(household_id))
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(
            or_(
                Payee.system.is_(None),
                Payee.system != SystemPayee.opening_balance,
            )
        )
    )


def _mostly(condition):
    """`condition`, telling SQLite it holds for nearly every row.

    Both transfer columns are indexed and NULL on nearly every row, and
    `sqlite_stat1` keeps only an average rows-per-value, which says an
    `IS NULL` on them matches a handful. So with statistics the planner drove
    every flow report off `ix_transactions_transfer_transaction_id`, read
    every non-transfer row of every household, and kept one household's
    afterwards (#238). `likelihood()` is the documented hint and changes no
    answer; the figure is a literal because SQLite refuses a bound one.
    """
    return func.likelihood(condition, literal_column("0.9375"), type_=Boolean)


def _is_payment(household_id: str):
    """True for a row that some row in the household names as its repayment.

    The ids are read **once**, as a subquery SQLite keeps as a list, and never
    as a list in Python -- not a correlated EXISTS probed once per row. The
    probe was 14s a query over 10,403 transactions, three queries per report
    (#297), because its speed was the planner's guess: `reimbursed_by_id` is
    NULL on nearly every row, so exact statistics call its index useless and
    every row rescanned the table, and no statistics at all let it probe the
    household index instead, which in a one-household ledger is the same
    thing. Read once, it is one pass over the household whatever
    `sqlite_stat1` says.

    `IS NOT NULL` is what keeps the negation honest. `id NOT IN (..., NULL)`
    is NULL for every row, and a NULL predicate drops the row: one expense
    with no link in the list and the report would be empty.
    """
    expense = aliased(Transaction)
    return Transaction.id.in_(
        select(expense.reimbursed_by_id)
        .where(expense.household_id == household_id)
        .where(expense.reimbursed_by_id.is_not(None))
    )


def _is_reimbursement(household_id: str):
    """What :func:`flow_rows` leaves out for being a work expense or its payment."""
    return or_(
        Transaction.reimbursement == ReimbursementState.expected,
        _is_payment(household_id),
    )


def _not_reimbursement(household_id: str):
    # `reimbursement != 'expected'` alone is NULL on the NULL rows -- nearly
    # every row -- and a NULL predicate drops the row. Say it out loud.
    return and_(
        or_(
            Transaction.reimbursement.is_(None),
            Transaction.reimbursement != ReimbursementState.expected,
        ),
        ~_is_payment(household_id),
    )


def excluded_counts(session: Session, household_id: str, **window) -> dict[str, int]:
    """How many rows the predicate removed, so the screen can say so.

    A report that quietly drops a fifth of the register is a report the reader
    cannot check. These numbers travel with every response and the screen
    prints them, which is also how the 41.5% finding would have been caught the
    first time anybody looked.

    Each row is counted once, under the first reason that applies: a transfer
    leg, then an opening balance, then a work expense or its payment. The
    rules that set a flag already refuse transfer legs, so the overlap should
    be empty -- but "should" is not what a count is for, and three numbers
    that add up to more rows than were dropped would be.
    """
    base = select(func.count()).select_from(Transaction).where(
        Transaction.household_id == household_id
    )
    base = _windowed(base, **window)

    transfers = session.execute(
        base.where(
            or_(
                Transaction.transfer_transaction_id.is_not(None),
                Transaction.transfer_account_id.is_not(None),
            )
        )
    ).scalar_one()
    openings = session.execute(
        base.join(Payee, Payee.id == Transaction.payee_id).where(
            Payee.system == SystemPayee.opening_balance
        )
    ).scalar_one()
    reimbursements = session.execute(
        base.where(Transaction.transfer_transaction_id.is_(None))
        .where(Transaction.transfer_account_id.is_(None))
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(
            or_(
                Payee.system.is_(None),
                Payee.system != SystemPayee.opening_balance,
            )
        )
        .where(_is_reimbursement(household_id))
    ).scalar_one()
    return {
        "transfers": int(transfers or 0),
        "opening_balances": int(openings or 0),
        "reimbursements": int(reimbursements or 0),
    }


def _windowed(
    stmt: Select,
    *,
    since: Date | None = None,
    until: Date | None = None,
    currency: str | None = None,
    account_ids: list[str] | None = None,
    category_ids: list[str] | None = None,
    include_uncategorised: bool = True,
) -> Select:
    """The filter vocabulary every report on this module shares.

    ``category_ids`` and ``include_uncategorised`` are two halves of one
    filter and are deliberately separate arguments. "Uncategorised" is a slice
    a person can tick and untick like any other, but it is not an id, so a list
    of ids cannot express it -- and a `None` inside that list would be a value
    the caller has to remember to special-case.
    """
    if since:
        stmt = stmt.where(Transaction.date >= since)
    if until:
        stmt = stmt.where(Transaction.date <= until)
    if currency or account_ids:
        stmt = stmt.join(Account, Account.id == Transaction.account_id)
    if currency:
        stmt = stmt.where(Account.currency == currency)
    if account_ids:
        stmt = stmt.where(Transaction.account_id.in_(account_ids))

    if category_ids is not None and include_uncategorised:
        stmt = stmt.where(
            or_(
                Transaction.category_id.in_(category_ids),
                Transaction.category_id.is_(None),
            )
        )
    elif category_ids is not None:
        stmt = stmt.where(Transaction.category_id.in_(category_ids))
    elif not include_uncategorised:
        stmt = stmt.where(Transaction.category_id.is_not(None))
    return stmt


def flow_span(
    session: Session,
    household_id: str,
    *,
    currency: str,
    account_ids: list[str] | None = None,
) -> tuple[Date, Date] | None:
    """The first and last dates this household has any *flow* on, in one currency.

    What "all dates" resolves to. Deliberately measured over `flow_rows` and
    not over the register: the earliest row in most ledgers is an opening
    balance, and letting that set the left edge would open every all-dates
    report with a month whose only content the report then refuses to show --
    a leading empty column, every time, on the one view where somebody is
    looking at the whole history.

    Narrowed by currency and by the account filter for the same reason the
    report is: the span of a EUR report is the span of the EUR rows. Returns
    None when there is no flow at all, which is a real state -- a household
    whose only transactions are transfers has nothing to report and no window
    to report it over.
    """
    stmt = flow_rows(household_id).with_only_columns(
        func.min(Transaction.date), func.max(Transaction.date)
    )
    stmt = _windowed(stmt, currency=currency, account_ids=account_ids)
    first, last = session.execute(stmt).one()
    if first is None or last is None:
        return None
    # SQLite hands dates back as `date` through the Date type, but a raw
    # `func.min` over a TEXT column can arrive as a string.
    return (_as_date(first), _as_date(last))


def _as_date(value) -> Date:
    return value if isinstance(value, Date) else Date.fromisoformat(str(value))


def months_between(since: Date, until: Date) -> list[str]:
    """Every `YYYY-MM` in the window, including the ones with no rows.

    The calendar decides the columns, not the data. Four of the twelve months
    in the real ledger have no transactions at all, and those are months
    nobody imported rather than months of frugality -- so a report that let the
    data name its own columns would silently close the gap and show eight
    months in a row, each one labelled with a date that is not next to the one
    beside it.

    A column that is present and empty says "nothing here". A column that is
    absent says nothing at all, and the reader supplies the wrong explanation.
    """
    out: list[str] = []
    year, month = since.year, since.month
    while (year, month) <= (until.year, until.month):
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


@dataclass(slots=True)
class Row:
    """One category's line across the window.

    `by_month` is keyed by `YYYY-MM` and holds only the months this category
    actually has; the caller reads it against `months_between`, so a missing
    key is a zero and does not need storing as one.
    """

    key: str | None
    name: str
    group_name: str | None
    by_month: dict[str, int] = field(default_factory=dict)
    total_minor: int = 0
    count: int = 0
    #: Mean per calendar month in the window, filled in by the caller.
    #:
    #: Not computed by the row, because the divisor is a property of the
    #: *window* rather than of the row: a row dividing by the months it happens
    #: to appear in turns "you spend 50 a month on this" into "you spent 50 the
    #: one month you bought it". Every row in one report divides by the same
    #: number, which is the count of calendar months on screen.
    average_minor: int = 0


@dataclass(slots=True)
class Section:
    """Income or Expense: its rows, and its own totals per month."""

    rows: list[Row] = field(default_factory=list)
    by_month: dict[str, int] = field(default_factory=dict)
    total_minor: int = 0
    count: int = 0
    average_minor: int = 0


def income_expense(
    session: Session,
    household_id: str,
    *,
    currency: str,
    since: Date | None = None,
    until: Date | None = None,
    account_ids: list[str] | None = None,
    category_ids: list[str] | None = None,
    include_uncategorised: bool = True,
) -> dict:
    """Income against expense, by month, broken down by category.

    **One currency.** The caller names it and this reports on it; there is no
    variant that spans two, because adding them is the error this whole module
    is arranged to make impossible.

    Either end of the window may be omitted, and an omitted end is resolved to
    where this household's flow actually starts or stops -- which is what "all
    dates" means. It is resolved to a **real date** here rather than left open,
    because the columns of this report are calendar months and an open end has
    no last column; the answer says which dates it settled on, so the screen
    never has to guess what it is showing.

    Which side a row lands on is decided by the **sign of the transaction**,
    not by anything stored about the category. A category is classification --
    it answers "what was this?" and carries no notion of direction -- so a
    refund against Groceries is money coming in and belongs in income for the
    month it arrived. Deciding by category instead would need a direction
    column that does not exist and would be wrong the first time a category saw
    both signs, which "Company refunds" does by definition.

    Zero-amount rows are counted in neither: they move no money, and putting
    them on one side would make that side's row count disagree with its total.

    One query, grouped in SQL. The register's own measurement is the warning
    against the alternative: 863ms to build ORM objects for twenty thousand
    rows against 228ms to read the columns.
    """
    if since is None or until is None:
        span = flow_span(
            session, household_id, currency=currency, account_ids=account_ids
        )
        # No flow at all: an empty report over the month somebody is standing
        # in, rather than no columns and a division by zero on the average.
        if span is None:
            today = Date.today()
            span = (today.replace(day=1), today)
        since = since or span[0]
        until = until or span[1]

    window = dict(
        since=since,
        until=until,
        currency=currency,
        account_ids=account_ids,
        category_ids=category_ids,
        include_uncategorised=include_uncategorised,
    )
    months = months_between(since, until)
    divisor = len(months) or 1

    period = func.strftime("%Y-%m", Transaction.date)
    # `direction` is the sign, resolved in SQL so the grouping happens in the
    # database rather than over rows dragged into Python.
    direction = func.iif(Transaction.amount > 0, "in", "out")

    stmt = (
        flow_rows(household_id)
        .with_only_columns(
            direction.label("direction"),
            period.label("period"),
            Category.id.label("category_id"),
            Category.name.label("category_name"),
            CategoryGroup.name.label("group_name"),
            func.sum(Transaction.amount).label("total"),
            func.count().label("n"),
        )
        .outerjoin(Category, Category.id == Transaction.category_id)
        .outerjoin(CategoryGroup, CategoryGroup.id == Category.group_id)
        .where(Transaction.amount != 0)
        .group_by(direction, period, Category.id, Category.name, CategoryGroup.name)
    )
    stmt = _windowed(stmt, **window)

    sections = {"in": Section(), "out": Section()}
    seen: dict[tuple[str, str | None], Row] = {}

    for way, month, cat_id, cat_name, group_name, total, n in session.execute(stmt).all():
        section = sections[way]
        row = seen.get((way, cat_id))
        if row is None:
            row = Row(
                key=cat_id,
                name=cat_name or UNCATEGORISED,
                group_name=group_name,
            )
            seen[(way, cat_id)] = row
            section.rows.append(row)
        amount, count = int(total or 0), int(n or 0)
        row.by_month[month] = row.by_month.get(month, 0) + amount
        row.total_minor += amount
        row.count += count
        section.by_month[month] = section.by_month.get(month, 0) + amount
        section.total_minor += amount
        section.count += count

    for section in sections.values():
        for row in section.rows:
            row.average_minor = _mean(row.total_minor, divisor)
        # Largest first, by magnitude: an expense total is negative, so sorting
        # on the raw figure would put the biggest outgoing at the bottom.
        section.rows.sort(key=lambda r: (-abs(r.total_minor), r.name))
        section.average_minor = _mean(section.total_minor, divisor)

    with_activity = {
        month
        for section in sections.values()
        for month in section.by_month
    }

    return {
        "currency": currency,
        "since": since,
        "until": until,
        "months": months,
        "income": sections["in"],
        "expense": sections["out"],
        # Net is the one sum this module *does* make, and it is safe: both
        # sides are already the same currency by construction, because the
        # currency was a filter rather than a grouping.
        "net_by_month": {
            month: sections["in"].by_month.get(month, 0)
            + sections["out"].by_month.get(month, 0)
            for month in months
        },
        "net_total_minor": sections["in"].total_minor + sections["out"].total_minor,
        "excluded": excluded_counts(session, household_id, **window),
        "coverage": {
            "months_in_range": len(months),
            "months_with_activity": len(with_activity),
        },
    }


def _mean(total: int, divisor: int) -> int:
    """A mean of minor units that stays an integer, rounded half away from zero.

    Money is never a float here, so this is integer arithmetic rather than
    `round(total / divisor)`. Half away from zero matches `money.py`'s
    ROUND_HALF_UP on the magnitude, so an average outgoing and an average
    incoming of the same size round to the same figure.

    `divisor` is the count of calendar months on screen and the one caller
    passes `len(months) or 1`, so it is never zero. There is deliberately no
    guard for that here: an unreachable branch is a branch no test can hold
    honest, and a zero arriving from some future caller should raise rather
    than quietly answer nought.
    """
    sign = -1 if total < 0 else 1
    magnitude = abs(total)
    return sign * ((magnitude * 2 + divisor) // (divisor * 2))


def currencies_in_use(session: Session, household_id: str) -> list[str]:
    """Every currency this household actually holds an account in.

    The toggle at the top of the report is built from this rather than from
    `Household.base_currency`, which is a default for new accounts and not a
    statement about what exists. Ordered by how much of the ledger each one
    carries, so the currency a household mostly uses is the one the report
    opens on.
    """
    rows = session.execute(
        select(Account.currency, func.count(Transaction.id))
        .select_from(Account)
        .outerjoin(Transaction, Transaction.account_id == Account.id)
        .where(Account.household_id == household_id)
        .group_by(Account.currency)
        .order_by(func.count(Transaction.id).desc(), Account.currency)
    ).all()
    return [row[0] for row in rows]


#: How many rows one drill-through will hand over.
#:
#: A cell on the Total column of an all-dates report can be thousands of rows,
#: and this is a bubble on top of a table rather than the register. The answer
#: is not to quietly return fewer -- that is the defect the register's own
#: ceiling comment describes -- so the response says it was capped and the
#: bubble says so too, pointing at the month columns, which are the tool for
#: the job.
DRILL_CEILING = 500


@dataclass(slots=True)
class Entry:
    """One transaction behind a figure, as the bubble shows it."""

    id: str
    date: Date
    account_name: str
    payee_name: str | None
    category_name: str | None
    memo: str | None
    amount_minor: int


def behind(
    session: Session,
    household_id: str,
    *,
    currency: str,
    since: Date | None = None,
    until: Date | None = None,
    account_ids: list[str] | None = None,
    category_ids: list[str] | None = None,
    include_uncategorised: bool = True,
    period: str | None = None,
    row_category_id: str | None = None,
    row_uncategorised: bool = False,
    direction: str | None = None,
    limit: int = DRILL_CEILING,
) -> dict:
    """The transactions that add up to one figure on the report.

    **It composes `flow_rows` and `_windowed` exactly as `income_expense`
    does**, and that is the entire point. A drill-through written against the
    register instead would show a different set of rows from the ones that made
    the number -- transfer legs and opening balances among them -- and the
    person checking a figure would be handed the evidence that it was wrong
    when it was right. The sum of what comes back here is the figure that was
    clicked, and there is a test that asserts exactly that.

    The narrowing arguments each correspond to something clickable:

    - ``period`` is one month column, absent for the Total column.
    - ``row_category_id`` / ``row_uncategorised`` is one category row, absent
      for a Total row. They are two arguments rather than one because
      "Uncategorised" is a real row but **is not an id**, which is the same
      reason `include_uncategorised` is separate from `category_ids`.
    - ``direction`` is which band, absent for Net -- where both sides are the
      honest answer, because a net figure is made of both.
    """
    stmt = flow_rows(household_id).where(Transaction.amount != 0)
    stmt = _windowed(
        stmt,
        since=since,
        until=until,
        currency=currency,
        account_ids=account_ids,
        category_ids=category_ids,
        include_uncategorised=include_uncategorised,
    )

    if period:
        stmt = stmt.where(func.strftime("%Y-%m", Transaction.date) == period)
    if row_uncategorised:
        stmt = stmt.where(Transaction.category_id.is_(None))
    elif row_category_id:
        stmt = stmt.where(Transaction.category_id == row_category_id)
    if direction == "in":
        stmt = stmt.where(Transaction.amount > 0)
    elif direction == "out":
        stmt = stmt.where(Transaction.amount < 0)

    # The total is counted over the whole match, before the cap, so a capped
    # bubble still reconciles against the cell it came from.
    totals = session.execute(
        stmt.with_only_columns(func.sum(Transaction.amount), func.count())
    ).one()
    total_minor = int(totals[0] or 0)
    count = int(totals[1] or 0)

    rows = session.execute(
        stmt.with_only_columns(
            Transaction.id,
            Transaction.date,
            Account.name,
            Payee.name,
            Category.name,
            Transaction.memo,
            Transaction.amount,
        )
        # `Account` is already joined -- `_windowed` attaches it for the
        # currency, which is required here. Joining it again is not a second
        # copy of a filter but an ambiguous column and a failed query.
        .outerjoin(Category, Category.id == Transaction.category_id)
        .order_by(Transaction.date.desc(), Transaction.created_at.desc())
        .limit(limit)
    ).all()

    return {
        "entries": [
            Entry(
                id=row[0],
                date=_as_date(row[1]),
                account_name=row[2],
                payee_name=row[3],
                category_name=row[4],
                memo=row[5],
                amount_minor=int(row[6]),
            )
            for row in rows
        ],
        "total_minor": total_minor,
        "count": count,
        "capped": count > len(rows),
    }


# --------------------------------------------------------------------------- #
# Reimbursements: what work owes, what came back, and what it covered
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class OutstandingRow:
    """One work expense still waiting for its money, as the table shows it."""

    id: str
    date: Date
    payee_name: str | None
    account_id: str
    account_name: str
    #: Positive: what is owed, in the account's minor units.
    amount: int
    has_receipt: bool
    #: What the row says about itself, for the table's Memo column and the
    #: detail dialog (#142). The category is the one a person gave it; a work
    #: expense is still somebody's Travel or Meals.
    memo: str | None = None
    category_name: str | None = None


@dataclass(slots=True)
class ClaimSettlement:
    """The money that came back."""

    id: str
    date: Date
    amount: int
    currency: str
    account_id: str
    account_name: str
    payee_name: str | None
    memo: str | None = None
    category_name: str | None = None


@dataclass(slots=True)
class ClaimExpense:
    """One expense a payment repaid. Positive, in its own account's currency."""

    id: str
    date: Date
    payee_name: str | None
    account_id: str
    account_name: str
    currency: str
    amount: int
    memo: str | None = None
    category_name: str | None = None


@dataclass(slots=True)
class Claim:
    """A payment and everything that points at it -- the claim, discovered.

    There is no claim table. The rows sharing one ``reimbursed_by_id`` *are*
    the claim, so this is assembled rather than read.

    ``covered`` and ``difference`` are **None, not zero**, whenever
    ``currencies`` holds more than one code. A GBP hotel repaid in EUR has no
    honest difference without a rate this ledger does not have, and zero would
    say it balanced.
    """

    settlement: ClaimSettlement
    expenses: list[ClaimExpense]
    covered: int | None
    difference: int | None
    currencies: list[str]


@dataclass(slots=True)
class ReimbursementMonth:
    """One expense month: what was flagged, and where it stands now."""

    month: str
    flagged: int
    recovered: int
    written_off: int
    outstanding: int


@dataclass(slots=True)
class Reimbursements:
    """The Reimbursements report, for one currency.

    Every figure is a **positive** count of minor units in ``currency``. The
    sign is dropped once, here, because every row this report can total is
    money out (the writer refuses anything else), and "work owes you -23.40"
    is a sentence nobody says.
    """

    currency: str
    since: Date | None
    until: Date | None
    available_currencies: list[str]
    outstanding: int
    outstanding_count: int
    oldest_outstanding: Date | None
    recovered: int
    recovered_count: int
    written_off: int
    written_off_count: int
    unmatched: int
    outstanding_rows: list[OutstandingRow]
    claims: list[Claim]
    months: list[ReimbursementMonth]


def _payment_ids(household_id: str) -> Select:
    """Every row some row in the household names as the payment that repaid it.

    Uncorrelated on purpose: it is used inside queries over `transactions`
    itself, and auto-correlation would quietly turn "any row points at this
    one" into "this row points at itself".
    """
    return (
        select(Transaction.reimbursed_by_id)
        .where(Transaction.household_id == household_id)
        .where(Transaction.reimbursed_by_id.is_not(None))
        .correlate(None)
    )


def reimbursement_currencies(session: Session, household_id: str) -> list[str]:
    """Currencies of the accounts holding a flagged row or a payment, busiest first.

    Not narrowed by the window: this is what the currency toggle is built
    from, and a toggle whose chips come and go as the dates move reads as the
    data changing rather than the question. Empty when nothing is flagged,
    which the screen says rather than drawing zeroes.
    """
    n = func.count(Transaction.id)
    rows = session.execute(
        select(Account.currency, n)
        .select_from(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.household_id == household_id)
        .where(
            or_(
                Transaction.reimbursement.is_not(None),
                Transaction.id.in_(_payment_ids(household_id)),
            )
        )
        .group_by(Account.currency)
        .order_by(n.desc(), Account.currency)
    ).all()
    return [row[0] for row in rows]


def reimbursements(
    session: Session,
    household_id: str,
    *,
    currency: str,
    since: Date | None = None,
    until: Date | None = None,
) -> Reimbursements:
    """What work owes this household, what it has paid back, and what it covered.

    **One currency**, like every report here. The totals and the months count
    the flagged rows in accounts of ``currency``; a GBP hotel is in the GBP
    answer and nowhere else, and there is no field anywhere that adds the two.

    ``since`` and ``until`` narrow on the **expense** date, never the
    payment's. An advance is paid before the expense it covers and a slow
    employer pays months after, so a window on the payment date would move
    expenses between periods according to when work got round to it.

    A claim is listed when any of its expenses falls in the window, and is
    then listed **whole** -- every expense the payment covered, in or out of
    the window. A claim cut in half by a date picker would show a difference
    that is an artefact of the picker, and ``unmatched`` would add it up.

    Aggregation is SQL (``SUM ... FILTER``, ``GROUP BY``). The only rows
    loaded are the ones the tables list -- the outstanding expenses, and the
    payments with what they covered -- never the ledger.
    """
    code = currency.upper()
    expected = Transaction.reimbursement == ReimbursementState.expected
    written_off = Transaction.reimbursement == ReimbursementState.written_off
    owed = and_(expected, Transaction.reimbursed_by_id.is_(None))
    repaid = and_(expected, Transaction.reimbursed_by_id.is_not(None))

    def flagged_in_currency(stmt: Select) -> Select:
        stmt = (
            stmt.select_from(Transaction)
            .join(Account, Account.id == Transaction.account_id)
            .where(Transaction.household_id == household_id)
            .where(Transaction.reimbursement.is_not(None))
            .where(Account.currency == code)
        )
        if since:
            stmt = stmt.where(Transaction.date >= since)
        if until:
            stmt = stmt.where(Transaction.date <= until)
        return stmt

    def owing(condition) -> tuple:
        # Every row here is money out, so the figure owed is minus the sum.
        return (
            func.coalesce(-func.sum(Transaction.amount).filter(condition), 0),
            func.count().filter(condition),
        )

    totals = session.execute(
        flagged_in_currency(
            select(
                *owing(owed),
                func.min(Transaction.date).filter(owed),
                *owing(repaid),
                *owing(written_off),
            )
        )
    ).one()

    month = func.strftime("%Y-%m", Transaction.date)
    month_rows = session.execute(
        flagged_in_currency(
            select(
                month,
                -func.sum(Transaction.amount),
                owing(repaid)[0],
                owing(written_off)[0],
                owing(owed)[0],
            )
        )
        .group_by(month)
        .order_by(month)
    ).all()

    has_receipt = (
        select(Receipt.id).where(Receipt.transaction_id == Transaction.id).exists()
    )
    outstanding_rows = [
        OutstandingRow(
            id=row[0],
            date=_as_date(row[1]),
            payee_name=row[2],
            account_id=row[3],
            account_name=row[4],
            amount=-int(row[5]),
            has_receipt=bool(row[6]),
            memo=row[7],
            category_name=row[8],
        )
        for row in session.execute(
            flagged_in_currency(
                select(
                    Transaction.id,
                    Transaction.date,
                    Payee.name,
                    Account.id,
                    Account.name,
                    Transaction.amount,
                    has_receipt,
                    Transaction.memo,
                    Category.name,
                )
            )
            .outerjoin(Payee, Payee.id == Transaction.payee_id)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(owed)
            .order_by(Transaction.date, Transaction.created_at)
        ).all()
    ]

    claims = _claims(session, household_id, currency=code, since=since, until=until)
    unmatched = sum(
        claim.difference
        for claim in claims
        if claim.settlement.currency == code
        and claim.difference is not None
        and claim.difference > 0
    )

    return Reimbursements(
        currency=code,
        since=since,
        until=until,
        available_currencies=reimbursement_currencies(session, household_id),
        outstanding=int(totals[0]),
        outstanding_count=int(totals[1]),
        oldest_outstanding=_as_date(totals[2]) if totals[2] is not None else None,
        recovered=int(totals[3]),
        recovered_count=int(totals[4]),
        written_off=int(totals[5]),
        written_off_count=int(totals[6]),
        unmatched=unmatched,
        outstanding_rows=outstanding_rows,
        claims=claims,
        months=[
            ReimbursementMonth(
                month=row[0],
                flagged=int(row[1] or 0),
                recovered=int(row[2]),
                written_off=int(row[3]),
                outstanding=int(row[4]),
            )
            for row in month_rows
        ],
    )


def _claims(
    session: Session,
    household_id: str,
    *,
    currency: str,
    since: Date | None,
    until: Date | None,
) -> list[Claim]:
    """Every payment an expense in the window points at, touching ``currency``.

    A claim touches a currency when its payment is in it **or** any of its
    expenses is, so a GBP hotel repaid in EUR is listed under both -- with no
    difference in either, because there is none to state.
    """
    expense_account = aliased(Account)
    settlement_account = aliased(Account)
    expense = aliased(Transaction)

    # Uncorrelated for the reason `_payment_ids` gives.
    in_window = _payment_ids(household_id)
    if since:
        in_window = in_window.where(Transaction.date >= since)
    if until:
        in_window = in_window.where(Transaction.date <= until)

    touching = (
        select(expense.reimbursed_by_id)
        .join(expense_account, expense_account.id == expense.account_id)
        .where(expense.household_id == household_id)
        .where(expense_account.currency == currency)
    )

    settlements = session.execute(
        select(
            Transaction.id,
            Transaction.date,
            Transaction.amount,
            settlement_account.currency,
            settlement_account.id,
            settlement_account.name,
            Payee.name,
            Transaction.memo,
            Category.name,
        )
        .join(settlement_account, settlement_account.id == Transaction.account_id)
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(Transaction.household_id == household_id)
        .where(Transaction.id.in_(in_window))
        .where(
            or_(
                settlement_account.currency == currency,
                Transaction.id.in_(touching),
            )
        )
        .order_by(Transaction.date.desc(), Transaction.created_at.desc())
    ).all()
    if not settlements:
        return []
    ids = [row[0] for row in settlements]

    # What each payment covered, summed in SQL per payment and per currency.
    # Grouping on the currency as well is what keeps a mixed claim from ever
    # producing a figure: its sums stay apart and `covered` is left None.
    # Both reads go a chunk of payments at a time: SQLite refuses more than
    # 32,766 bound variables (#231). Every expense of one payment is in that
    # payment's chunk, so their order under it is kept.
    covered: dict[str, dict[str, int]] = {}
    expenses: dict[str, list[ClaimExpense]] = {sid: [] for sid in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        for sid, code, total in session.execute(
            select(
                Transaction.reimbursed_by_id,
                expense_account.currency,
                -func.sum(Transaction.amount),
            )
            .join(expense_account, expense_account.id == Transaction.account_id)
            .where(Transaction.household_id == household_id)
            .where(Transaction.reimbursed_by_id.in_(chunk))
            .group_by(Transaction.reimbursed_by_id, expense_account.currency)
        ).all():
            covered.setdefault(sid, {})[code] = int(total or 0)

        for row in session.execute(
            select(
                Transaction.reimbursed_by_id,
                Transaction.id,
                Transaction.date,
                Payee.name,
                expense_account.id,
                expense_account.name,
                expense_account.currency,
                Transaction.amount,
                Transaction.memo,
                Category.name,
            )
            .join(expense_account, expense_account.id == Transaction.account_id)
            .outerjoin(Payee, Payee.id == Transaction.payee_id)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(Transaction.household_id == household_id)
            .where(Transaction.reimbursed_by_id.in_(chunk))
            .order_by(Transaction.date, Transaction.created_at)
        ).all():
            expenses[row[0]].append(
                ClaimExpense(
                    id=row[1],
                    date=_as_date(row[2]),
                    payee_name=row[3],
                    account_id=row[4],
                    account_name=row[5],
                    currency=row[6],
                    amount=-int(row[7]),
                    memo=row[8],
                    category_name=row[9],
                )
            )

    claims: list[Claim] = []
    for (
        sid, when, amount, code, account_id, account_name, payee_name, memo, category_name
    ) in settlements:
        by_currency = covered.get(sid, {})
        currencies = sorted({code, *by_currency})
        single = len(currencies) == 1
        total = by_currency.get(code, 0) if single else None
        claims.append(
            Claim(
                settlement=ClaimSettlement(
                    id=sid,
                    date=_as_date(when),
                    amount=int(amount),
                    currency=code,
                    account_id=account_id,
                    account_name=account_name,
                    payee_name=payee_name,
                    memo=memo,
                    category_name=category_name,
                ),
                expenses=expenses[sid],
                covered=total,
                difference=int(amount) - total if total is not None else None,
                currencies=currencies,
            )
        )
    return claims
