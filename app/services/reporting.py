"""Flow reporting: what came in, what went out, and what it was for.

Every function here answers in **one currency at a time**. That is not a
convenience -- `insights.py` already carries the argument at length, and this
module is bound by the same rule:

> **No response ever sums two currencies.**

The ledger never converts. Currency lives on the account, and the only
place an amount meets a rate is `services/fx.py` (#396): a report that offers
figures in the base currency converts each row there, says which rates it
used, and carries `rates_status`. Nothing in this module divides by a rate,
and `tests/test_fx_service.py` greps for it. So currency here is a
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
- **Rows in another currency.** Not excluded so much as never asked for; see
  above.

The first two are what :func:`flow_rows` removes, and it is written **once**
here so that every report composes the same predicate rather than the same
intentions. Two copies of this rule is how they come to disagree.

Work reimbursements are flow -- money did leave and arrive -- but they are not
the household's income or spending: a hotel the employer pays back was never
its spending, and the payment back is not its income. Left in, both sides are
inflated by the same figure, net is right and every other number on the page
is wrong. So Income vs Expense **sets them apart** in a block of their own
rather than dropping them (#372), and :func:`side` is the one place that says
which rows those are. A ``written_off`` row is a cost after all and stays an
expense, in its own month (G(i), 2026-09-25).

**What a repayment differs by is counted.** A repayment of 300 against 200 of
linked expenses leaves the household 100 better off, and that 100 is income;
one of 150 against 200 leaves it 50 worse off, and that is a cost. So each
repayment in the Reimbursement category is compared with the expenses linked
to it, and the difference lands in Income as *Reimbursement surplus* or in
Expenses as *Reimbursement shortfall*, in the repayment's month. The part that
merely pays back what was spent stays set apart. A repayment linked to nothing
has no difference to state -- counting it whole would overstate income
whenever linking lags -- and neither has one whose expenses are in another
currency, which this ledger cannot convert. :func:`claim_lines` is the one
place that says which repayments those are.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from sqlalchemy import Boolean, Select, and_, case, func, literal_column, or_, select
from sqlalchemy.orm import Session, aliased

from ..models import (
    Account,
    Category,
    CategoryGroup,
    CategoryGroupRole,
    CategoryRole,
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

    Work expenses and their repayments are **kept**: they are flow, and
    :func:`side` is what puts them in a block of their own rather than in
    income or expenses (#372).
    """
    return (
        select(Transaction)
        .where(Transaction.household_id == household_id)
        .where(_mostly(Transaction.transfer_transaction_id.is_(None)))
        .where(_mostly(Transaction.transfer_account_id.is_(None)))
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


# --------------------------------------------------------------------------- #
# Where a row lands on Income vs Expense (#372)
# --------------------------------------------------------------------------- #

#: In the Income group: income, whatever the sign.
INCOME = "in"
#: Anywhere else, or uncategorised: an expense line, whatever the sign.
EXPENSE = "out"
#: In the Reimbursement category: money work paid back, linked or not.
REIMBURSEMENT_INCOME = "reimbursement_income"
#: Flagged `expected` and linked to the payment that repaid it.
REIMBURSEMENT_SPENT = "reimbursement_spent"
#: Flagged `expected` and still waiting for its payment.
REIMBURSEMENT_PENDING = "reimbursement_pending"
#: A repayment that came to more than the expenses linked to it: the
#: difference, counted in income in the repayment's month.
REIMBURSEMENT_SURPLUS = "reimbursement_surplus"
#: A repayment that came to less: the difference, counted in expenses.
REIMBURSEMENT_SHORTFALL = "reimbursement_shortfall"
#: A repayment linked to no expense: set apart, with no difference to state.
REIMBURSEMENT_UNMATCHED = "reimbursement_unmatched"
#: The block's view of the two counted lines together: surplus less shortfall.
REIMBURSEMENT_DIFFERENCE = "reimbursement_difference"

#: The two sides that make income, expenses and net.
COUNTED = (INCOME, EXPENSE)
#: The block shown for information and counted in none of them.
SET_APART = (REIMBURSEMENT_INCOME, REIMBURSEMENT_SPENT, REIMBURSEMENT_PENDING)
SIDES = COUNTED + SET_APART
#: The lines worked out per repayment rather than read off one row's columns.
#: The first two are counted, in income and expenses; the other two are views
#: of the block. Every one of them can be drilled into.
CLAIM_LINES = (
    REIMBURSEMENT_SURPLUS,
    REIMBURSEMENT_SHORTFALL,
    REIMBURSEMENT_UNMATCHED,
    REIMBURSEMENT_DIFFERENCE,
)
LINES = SIDES + CLAIM_LINES


def side():
    """Which line of Income vs Expense a row lands on, as one SQL expression.

    The rules, in order -- a row lands in the first that applies:

    1. its category carries the ``reimbursement`` role: Reimbursement income,
       any sign, linked or not;
    2. flagged ``expected`` and linked to a payment: Amount spent, reimbursed;
    3. flagged ``expected`` with no payment yet: Pending reimbursement;
    4. its group carries the ``income`` role: income, under its category,
       whatever the sign;
    5. anything else, uncategorised included: an expense line, whatever the
       sign, so a refund in Groceries reduces Groceries in its month.

    **The sign never decides the side.** Deciding by sign put every refund in
    income and showed Hotel on both halves of the report. Being somebody's
    repayment no longer decides anything either: a salary that also carried a
    claim's repayment is still Salary (rule 4), and only the Reimbursement
    category sets a payment apart. A ``written_off`` row matches none of 1-3,
    so it is an expense, as decided when reimbursements were built.

    Reads the two roles rather than any name, so renaming or translating the
    Income group or the Reimbursement category changes nothing. Needs
    `Category` and `CategoryGroup` joined, which :func:`classified_rows` does.
    It reads only the row's own columns -- no subquery over who repaid whom,
    which was 14s a query over 10,403 rows until #297 rewrote it.
    """
    expected = Transaction.reimbursement == ReimbursementState.expected
    return case(
        (Category.role == CategoryRole.reimbursement, REIMBURSEMENT_INCOME),
        (and_(expected, Transaction.reimbursed_by_id.is_not(None)), REIMBURSEMENT_SPENT),
        (expected, REIMBURSEMENT_PENDING),
        (CategoryGroup.role == CategoryGroupRole.income, INCOME),
        else_=EXPENSE,
    )


def classified_rows(household_id: str) -> Select:
    """:func:`flow_rows` with what :func:`side` reads joined on.

    Outer joins, because an uncategorised row is a real state and rule 5's.
    """
    return (
        flow_rows(household_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .outerjoin(CategoryGroup, CategoryGroup.id == Category.group_id)
    )


def claim_lines(household_id: str, **window) -> Select:
    """Every repayment in the window, with what its linked expenses add up to.

    One row per transaction in the category carrying the ``reimbursement``
    role that :func:`income_expense` would draw: ``(id, period, amount,
    covered, linked, foreign)``. ``covered`` sums the linked expenses in the
    report's currency (negative, as the ledger signs them), ``linked`` counts
    them, and ``foreign`` counts the ones in any other currency.

    Read by :func:`claim_side`: linked to nothing is unmatched, any foreign
    expense means no difference can be stated, and otherwise ``amount +
    covered`` is the surplus (positive) or the shortfall (negative).

    The repayment is narrowed by the report's window like any row; its
    expenses are not, because an expense is often paid months before the
    money comes back and a claim cut in half by the date picker would show a
    difference that is an artefact of the picker. Only expenses flagged
    ``expected`` count -- rule 2 of :func:`side`, the rows already set apart --
    so a written-off row, which is an expense already, is never counted twice.
    """
    expense = aliased(Transaction)
    expense_account = aliased(Account)
    currency = window.get("currency")
    linked = (
        select(
            expense.reimbursed_by_id.label("payment_id"),
            func.count().label("linked"),
            func.sum(
                case((expense_account.currency == currency, expense.amount), else_=0)
            ).label("covered"),
            func.sum(case((expense_account.currency != currency, 1), else_=0)).label(
                "foreign"
            ),
        )
        .join(expense_account, expense_account.id == expense.account_id)
        .where(expense.household_id == household_id)
        .where(expense.reimbursed_by_id.is_not(None))
        .where(expense.reimbursement == ReimbursementState.expected)
        .group_by(expense.reimbursed_by_id)
        .subquery()
    )
    stmt = (
        classified_rows(household_id)
        .outerjoin(linked, linked.c.payment_id == Transaction.id)
        .where(Category.role == CategoryRole.reimbursement)
        .where(Transaction.amount != 0)
        .with_only_columns(
            Transaction.id,
            func.strftime("%Y-%m", Transaction.date),
            Transaction.amount,
            func.coalesce(linked.c.covered, 0),
            func.coalesce(linked.c.linked, 0),
            func.coalesce(linked.c.foreign, 0),
        )
    )
    return _windowed(stmt, **window)


def claim_side(amount: int, covered: int, linked: int, foreign: int) -> tuple[str | None, int]:
    """Which counted line a repayment's difference lands on, and the difference.

    ``(None, 0)`` when there is nothing to count: a repayment linked to no
    expense (:data:`REIMBURSEMENT_UNMATCHED` says so in the block), one with an
    expense in another currency, or one that came to exactly what was spent.
    """
    if linked == 0 or foreign:
        return None, 0
    difference = int(amount) + int(covered)
    if difference > 0:
        return REIMBURSEMENT_SURPLUS, difference
    if difference < 0:
        return REIMBURSEMENT_SHORTFALL, difference
    return None, 0


def has_income_group(session: Session, household_id: str) -> bool:
    """Whether any group carries the ``income`` role.

    Without one every row is an expense line, which is the rule working
    rather than a fault -- so the report says so in a line rather than
    showing an Income section that is empty for no visible reason.
    """
    return (
        session.execute(
            select(CategoryGroup.id)
            .where(CategoryGroup.household_id == household_id)
            .where(CategoryGroup.role == CategoryGroupRole.income)
            .limit(1)
        ).first()
        is not None
    )


def excluded_counts(session: Session, household_id: str, **window) -> dict[str, int]:
    """How many rows the predicate removed, so the screen can say so.

    A report that quietly drops a fifth of the register is a report the reader
    cannot check. These numbers travel with every response and the screen
    prints them, which is also how the 41.5% finding would have been caught the
    first time anybody looked.

    Each row is counted once, under the first reason that applies: a transfer
    leg, then an opening balance. The work-reimbursement count is not made
    here -- those rows are on the report, in their own block, and
    :func:`income_expense` counts them off the same query that drew it.
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
        base.where(Transaction.transfer_transaction_id.is_(None))
        .where(Transaction.transfer_account_id.is_(None))
        .join(Payee, Payee.id == Transaction.payee_id)
        .where(Payee.system == SystemPayee.opening_balance)
    ).scalar_one()
    return {
        "transfers": int(transfers or 0),
        "opening_balances": int(openings or 0),
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
    #: Set on a row that is no category but a line worked out per repayment
    #: -- :data:`REIMBURSEMENT_SURPLUS` in income, :data:`REIMBURSEMENT_SHORTFALL`
    #: in expenses -- and None on every category row, Uncategorised included.
    line: str | None = None
    #: Mean per calendar month in the window, filled in by the caller.
    #:
    #: Not computed by the row, because the divisor is a property of the
    #: *window* rather than of the row: a row dividing by the months it happens
    #: to appear in turns "you spend 50 a month on this" into "you spent 50 the
    #: one month you bought it". Every row in one report divides by the same
    #: number, which is the count of calendar months on screen.
    average_minor: int = 0


@dataclass(slots=True)
class Line:
    """A figure across the window with no breakdown under it.

    A category group's subtotal, and each of the three work-reimbursement
    lines. Summed by the server like every other figure on the report, so the
    screen adds nothing up itself and every average rounds the same way.
    """

    key: str | None = None
    name: str | None = None
    by_month: dict[str, int] = field(default_factory=dict)
    total_minor: int = 0
    count: int = 0
    average_minor: int = 0

    def add(self, month: str, amount: int, count: int) -> None:
        self.by_month[month] = self.by_month.get(month, 0) + amount
        self.total_minor += amount
        self.count += count


@dataclass(slots=True)
class Section:
    """Income or Expense: its rows, its groups' subtotals, and its own totals."""

    rows: list[Row] = field(default_factory=list)
    #: One subtotal per category group with a row in this section, in the
    #: order the rows are in. Uncategorised belongs to no group and has none.
    groups: list[Line] = field(default_factory=list)
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

    Which line a row lands on is :func:`side` -- **the category group decides,
    never the sign** (#372). Income is the group carrying the ``income`` role;
    every other category, and Uncategorised, is an expense line where refunds
    and charges net per month. Work reimbursements are their own block,
    ``reimbursements``, shown for information and counted in neither income,
    expenses nor net -- except what a repayment differs from its expenses
    by, which :func:`claim_lines` works out and which is counted, as the
    *Reimbursement surplus* row in income or the *Reimbursement shortfall*
    row in expenses. The block shows that difference too, as ``difference``,
    so the reader sees where the counted figure came from.

    Zero-amount rows are counted nowhere: they move no money, and putting
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
    # Resolved in SQL so the grouping happens in the database rather than
    # over rows dragged into Python.
    where = side()

    stmt = (
        classified_rows(household_id)
        .with_only_columns(
            where.label("side"),
            period.label("period"),
            Category.id.label("category_id"),
            Category.name.label("category_name"),
            CategoryGroup.id.label("group_id"),
            CategoryGroup.name.label("group_name"),
            func.sum(Transaction.amount).label("total"),
            func.count().label("n"),
        )
        .where(Transaction.amount != 0)
        .group_by(
            where, period, Category.id, Category.name, CategoryGroup.id, CategoryGroup.name
        )
    )
    stmt = _windowed(stmt, **window)

    sections = {INCOME: Section(), EXPENSE: Section()}
    block = {
        one: Line(key=one)
        for one in (*SET_APART, REIMBURSEMENT_DIFFERENCE, REIMBURSEMENT_UNMATCHED)
    }
    seen: dict[tuple[str, str | None], Row] = {}
    group_lines: dict[tuple[str, str], Line] = {}

    for way, month, cat_id, cat_name, group_id, group_name, total, n in session.execute(
        stmt
    ).all():
        amount, count = int(total or 0), int(n or 0)
        if way in block:
            block[way].add(month, amount, count)
            continue
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
        row.by_month[month] = row.by_month.get(month, 0) + amount
        row.total_minor += amount
        row.count += count
        if group_id is not None:
            line = group_lines.get((way, group_id))
            if line is None:
                line = Line(key=group_id, name=group_name)
                group_lines[(way, group_id)] = line
                section.groups.append(line)
            line.add(month, amount, count)
        section.by_month[month] = section.by_month.get(month, 0) + amount
        section.total_minor += amount
        section.count += count

    # What each repayment differs from its expenses by: counted, in the
    # repayment's month, on a line of its own in income or expenses.
    derived = {
        REIMBURSEMENT_SURPLUS: (INCOME, "Reimbursement surplus"),
        REIMBURSEMENT_SHORTFALL: (EXPENSE, "Reimbursement shortfall"),
    }
    lines: dict[str, Row] = {}
    mixed = 0
    for _id, month, amount, covered, linked, foreign in session.execute(
        claim_lines(household_id, **window)
    ).all():
        if int(linked) == 0:
            block[REIMBURSEMENT_UNMATCHED].add(month, int(amount), 1)
            continue
        if int(foreign):
            mixed += 1
            continue
        line, difference = claim_side(amount, covered, linked, foreign)
        if line is None:
            continue
        way, name = derived[line]
        row = lines.get(line)
        if row is None:
            row = lines[line] = Row(key=None, name=name, group_name=None, line=line)
            sections[way].rows.append(row)
        section = sections[way]
        row.by_month[month] = row.by_month.get(month, 0) + difference
        row.total_minor += difference
        row.count += 1
        section.by_month[month] = section.by_month.get(month, 0) + difference
        section.total_minor += difference
        section.count += 1
        block[REIMBURSEMENT_DIFFERENCE].add(month, difference, 1)

    for way, section in sections.items():
        for row in section.rows:
            row.average_minor = _mean(row.total_minor, divisor)
        for line in section.groups:
            line.average_minor = _mean(line.total_minor, divisor)
        section.average_minor = _mean(section.total_minor, divisor)
        _order(section, largest=(lambda total: -total) if way == INCOME else (lambda total: total))
    for line in block.values():
        line.average_minor = _mean(line.total_minor, divisor)

    with_activity = {
        month
        for lines in (*sections.values(), *block.values())
        for month in lines.by_month
    }

    excluded = excluded_counts(session, household_id, **window)
    # The rows set apart, counted off the query that drew the block, so the
    # screen's "N rows set apart" is the block's own count and nothing else.
    excluded["reimbursements"] = sum(block[one].count for one in SET_APART)

    return {
        "currency": currency,
        "since": since,
        "until": until,
        "months": months,
        "income": sections[INCOME],
        "expense": sections[EXPENSE],
        # Net is the one sum this module *does* make, and it is safe: both
        # sides are already the same currency by construction, because the
        # currency was a filter rather than a grouping. The reimbursement
        # block is not in it -- that is what setting it apart means -- but the
        # surplus and shortfall rows are, being rows of the two sections.
        "net_by_month": {
            month: sections[INCOME].by_month.get(month, 0)
            + sections[EXPENSE].by_month.get(month, 0)
            for month in months
        },
        "net_total_minor": sections[INCOME].total_minor + sections[EXPENSE].total_minor,
        "reimbursements": {
            "income": block[REIMBURSEMENT_INCOME],
            "spent": block[REIMBURSEMENT_SPENT],
            "difference": block[REIMBURSEMENT_DIFFERENCE],
            "unmatched": block[REIMBURSEMENT_UNMATCHED],
            "pending": block[REIMBURSEMENT_PENDING],
        },
        # Repayments whose expenses are in another currency: no difference
        # can be stated without a rate, so the screen says how many there are.
        "reimbursement_mixed": mixed,
        "has_income_group": has_income_group(session, household_id),
        "excluded": excluded,
        "coverage": {
            "months_in_range": len(months),
            "months_with_activity": len(with_activity),
        },
    }


def _order(section: Section, *, largest) -> None:
    """Groups by size, then each group's categories by size; Uncategorised last.

    "Largest" is the side's own sense of it -- the most money in for income,
    the most money out for expenses -- so on the expense side a category that
    refunds outweighed sorts after every category with spending in it rather
    than above them for being large and positive. The rows are put in group
    order here because the screen gathers them under their headings in the
    order they arrive.
    """
    section.groups.sort(key=lambda line: (largest(line.total_minor), line.name or ""))
    rank = {line.name: n for n, line in enumerate(section.groups)}
    section.rows.sort(
        key=lambda row: (
            row.line is not None,
            row.group_name is None,
            rank.get(row.group_name, 0),
            largest(row.total_minor),
            row.name,
        )
    )


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
    row_group_id: str | None = None,
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
    - ``row_group_id`` is one category group's subtotal line (#372).
    - ``direction`` is which line of :data:`LINES` -- income, expense, one
      of the work-reimbursement lines, or one worked out per repayment -- and
      is absent for Net, which is income and expense together and never the
      reimbursement block.

    A line worked out per repayment (:data:`CLAIM_LINES`) is shown as the
    repayments behind it **with the expenses each one covered**: the payment
    of 300 and the two expenses of 100 add up to the surplus of 100, which is
    the figure clicked. Income, Expenses and Net include those rows too when
    no category row narrows them, because the surplus and the shortfall are
    in those totals.
    """
    if direction is not None and direction not in LINES:
        raise ValueError(f"no such line: {direction!r}")
    window = dict(
        since=since,
        until=until,
        currency=currency,
        account_ids=account_ids,
        category_ids=category_ids,
        include_uncategorised=include_uncategorised,
    )
    a_row = bool(row_uncategorised or row_category_id or row_group_id)
    claims: tuple[str, ...] = ()
    if direction in CLAIM_LINES:
        claims = (
            (REIMBURSEMENT_SURPLUS, REIMBURSEMENT_SHORTFALL)
            if direction == REIMBURSEMENT_DIFFERENCE
            else (direction,)
        )
    elif not a_row and direction in (None, INCOME, EXPENSE):
        claims = {
            None: (REIMBURSEMENT_SURPLUS, REIMBURSEMENT_SHORTFALL),
            INCOME: (REIMBURSEMENT_SURPLUS,),
            EXPENSE: (REIMBURSEMENT_SHORTFALL,),
        }[direction]
    extra, extra_total, extra_count = _claim_entries(
        session, household_id, claims, window, period, limit
    )
    if direction in CLAIM_LINES:
        return {
            "entries": extra[:limit],
            "total_minor": extra_total,
            "count": extra_count,
            "capped": extra_count > min(len(extra), limit),
        }

    stmt = classified_rows(household_id).where(Transaction.amount != 0)
    stmt = _windowed(stmt, **window)

    if period:
        stmt = stmt.where(func.strftime("%Y-%m", Transaction.date) == period)
    if row_uncategorised:
        stmt = stmt.where(Transaction.category_id.is_(None))
    elif row_category_id:
        stmt = stmt.where(Transaction.category_id == row_category_id)
    if row_group_id:
        stmt = stmt.where(Category.group_id == row_group_id)
    stmt = stmt.where(side().in_(COUNTED) if direction is None else side() == direction)

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
        # currency, which is required here -- and `Category` is, by
        # `classified_rows`. Joining either again is not a second copy of a
        # filter but an ambiguous column and a failed query.
        .order_by(Transaction.date.desc(), Transaction.created_at.desc())
        .limit(limit)
    ).all()

    entries = [
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
    ]
    if extra:
        # Newest first across both, as each list already is; `sorted` is
        # stable, so rows of one date keep the order their query gave them.
        entries = sorted(entries + extra, key=lambda one: one.date, reverse=True)[:limit]
    total_minor += extra_total
    count += extra_count
    return {
        "entries": entries,
        "total_minor": total_minor,
        "count": count,
        "capped": count > len(entries),
    }


def _claim_entries(
    session: Session,
    household_id: str,
    lines: tuple[str, ...],
    window: dict,
    period: str | None,
    limit: int,
) -> tuple[list[Entry], int, int]:
    """The rows behind lines worked out per repayment: ``(entries, total, count)``.

    For a surplus or shortfall, each repayment on that line and every expense
    linked to it, so the rows sum to the difference. For the unmatched line,
    the repayments alone -- there is nothing linked to show. Entries are
    newest first and capped at ``limit``; the total and the count are not.
    """
    if not lines:
        return [], 0, 0
    stmt = claim_lines(household_id, **window)
    if period:
        stmt = stmt.where(func.strftime("%Y-%m", Transaction.date) == period)
    payments: list[str] = []
    with_expenses: list[str] = []
    for pid, _month, amount, covered, linked, foreign in session.execute(stmt).all():
        if int(linked) == 0:
            if REIMBURSEMENT_UNMATCHED in lines:
                payments.append(pid)
            continue
        line, _difference = claim_side(amount, covered, linked, foreign)
        if line in lines:
            payments.append(pid)
            with_expenses.append(pid)
    if not payments:
        return [], 0, 0

    entries: list[Entry] = []
    expanded = set(with_expenses)
    for start in range(0, len(payments), 500):
        chunk = payments[start : start + 500]
        linked_chunk = [pid for pid in chunk if pid in expanded]
        rows = session.execute(
            select(
                Transaction.id,
                Transaction.date,
                Account.name,
                Payee.name,
                Category.name,
                Transaction.memo,
                Transaction.amount,
            )
            .join(Account, Account.id == Transaction.account_id)
            .outerjoin(Payee, Payee.id == Transaction.payee_id)
            .outerjoin(Category, Category.id == Transaction.category_id)
            .where(Transaction.household_id == household_id)
            .where(
                or_(
                    Transaction.id.in_(chunk),
                    and_(
                        Transaction.reimbursed_by_id.in_(linked_chunk),
                        Transaction.reimbursement == ReimbursementState.expected,
                    ),
                )
            )
            .order_by(Transaction.date.desc(), Transaction.created_at.desc())
        ).all()
        entries.extend(
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
        )
    entries.sort(key=lambda one: one.date, reverse=True)
    total = sum(one.amount_minor for one in entries)
    return entries[:limit], total, len(entries)


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
