"""Arithmetic over the ledger, done in SQL and in integers.

The only read an agent had was the register, up to 25 000 rows -- roughly
9.5 MB of JSON, as that endpoint's own comment says. Handing that to a model to
add up is wrong three times over: on cost, on latency, and on correctness,
because the arithmetic ends up being done in floating point on money by a
system with no invariant that would catch the error.

So the sums happen here, as `func.sum` over an indexed column, and the model
gets a few hundred tokens with the totals already right.

> [!danger] The one rule every function in this module exists to keep
> **No response ever sums two currencies.**
>
> The ledger never converts -- currency lives on the account, and there is no
> rate anywhere in this schema. The previous build produced three wrong
> reports from exactly this, by the simple route of somebody
> writing `SUM(amount)` across a household that had both.
>
> Every answer here is therefore grouped by currency at the top level, and
> **there is no grand total**. Not a zero, not a null, not a field an agent
> could mistake for one: the key is absent, because the number does not exist.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date as Date

from sqlalchemy import String, func, select
from sqlalchemy.orm import Session

from .. import money
from ..errors import ValidationError
from ..models import (
    Account,
    Category,
    CategoryGroup,
    ClearedState,
    Payee,
    Transaction,
)
from . import transactions as txn_service


class GroupBy(enum.StrEnum):
    """What the rows are gathered into. One value per join this module makes."""

    category = "category"
    category_group = "category_group"
    payee = "payee"
    account = "account"
    month = "month"


class Bucket(enum.StrEnum):
    day = "day"
    week = "week"
    month = "month"


#: SQLite's own date arithmetic, so the grouping happens in the database rather
#: than over rows dragged into Python.
_BUCKET_FORMAT = {Bucket.day: "%Y-%m-%d", Bucket.month: "%Y-%m"}


def _bucket_label(bucket: Bucket):
    """The column expression a timeseries gathers by.

    A week is labelled with the **date of its Monday**, not with a week number.
    `%Y-W%W` was both: `%W` counts weeks from the first Monday of the calendar
    year, and pairing it with `%Y` is not ISO week numbering -- so a Monday-to-
    Sunday week straddling 1 January was cut in half. 29 Dec 2025 to 4 Jan 2026
    came back as `2025-W52` holding Monday to Wednesday and `2026-W00` holding
    Thursday to Sunday: two consecutive weeks at roughly half their real spend,
    every January, with nothing saying either was partial.

    A real date cannot straddle a year, sorts correctly as a string, and two
    adjacent weeks cannot collide -- which they could, since `2026-W00` is a
    partial week sitting next to a full `2026-W01`.

    `%w` is 0 for Sunday, so `(%w + 6) % 7` is days since Monday.
    """
    if bucket is not Bucket.week:
        return func.strftime(_BUCKET_FORMAT[bucket], Transaction.date)
    days_since_monday = (func.strftime("%w", Transaction.date) + 6) % 7
    return func.date(Transaction.date, "-" + func.cast(days_since_monday, String) + " days")


@dataclass(frozen=True, slots=True)
class Group:
    """One row of an answer: what it is, what it came to, how many."""

    key: str | None
    name: str
    sum_minor: int
    count: int


@dataclass(slots=True)
class CurrencyTotals:
    """Everything in one currency, and nothing from any other."""

    total_minor: int = 0
    count: int = 0
    groups: list[Group] = field(default_factory=list)


def _label_for(group_by: GroupBy):
    """The column to gather by, and the join it needs.

    Returned together because they are one decision: a `group_by` whose join is
    forgotten silently gathers everything into one bucket labelled None.
    """
    if group_by is GroupBy.category:
        return Category.id, Category.name, "category"
    if group_by is GroupBy.category_group:
        return CategoryGroup.id, CategoryGroup.name, "category_group"
    if group_by is GroupBy.payee:
        return Payee.id, Payee.name, "payee"
    if group_by is GroupBy.account:
        return Account.id, Account.name, "account"
    return None, None, "month"


def _joined(stmt, needs: str):
    """Attach exactly the joins a grouping needs, and no others.

    `Account` is always joined because the currency comes from it, and the
    currency is what every answer is partitioned by.
    """
    stmt = stmt.join(Account, Account.id == Transaction.account_id)
    if needs in ("category", "category_group"):
        stmt = stmt.outerjoin(Category, Category.id == Transaction.category_id)
    if needs == "category_group":
        stmt = stmt.outerjoin(CategoryGroup, CategoryGroup.id == Category.group_id)
    if needs == "payee":
        stmt = stmt.outerjoin(Payee, Payee.id == Transaction.payee_id)
    return stmt


#: What an unlabelled bucket is called. Null is a real and common answer --
#: most households have transactions with no category yet -- so it gets a name
#: a person and a model can both read, rather than a null the caller must
#: special-case.
UNSET_NAME = {
    GroupBy.category: "Uncategorised",
    GroupBy.category_group: "Uncategorised",
    GroupBy.payee: "No payee",
    GroupBy.account: "No account",
    GroupBy.month: "No date",
}


def summary(
    session: Session,
    household_id: str,
    *,
    group_by: GroupBy = GroupBy.category,
    account_id: str | None = None,
    since: Date | None = None,
    until: Date | None = None,
    search: str | None = None,
    cleared: ClearedState | None = None,
    uncategorised: bool = False,
) -> dict[str, CurrencyTotals]:
    """Totals by whatever you asked for, partitioned by currency.

    The filter vocabulary is `transactions.filtered`'s, literally -- the same
    function the register narrows with. Its WHERE clause is applied to the
    aggregate itself: `filtered` joins nothing, so the clause is about
    `transactions` alone, and filtering through `id IN (SELECT id ...)`
    materialised every matching id and probed the key once per row -- two to
    three times the cost for the same answer (#238).
    """
    id_column, name_column, needs = _label_for(group_by)

    base = txn_service.filtered(
        household_id,
        account_id=account_id,
        since=since,
        until=until,
        search=search,
        cleared=cleared,
        uncategorised=uncategorised,
    )
    if group_by is GroupBy.month:
        label_id = func.strftime("%Y-%m", Transaction.date)
        label_name = label_id
    else:
        label_id, label_name = id_column, name_column

    stmt = _joined(
        select(
            Account.currency,
            label_id.label("key"),
            label_name.label("name"),
            func.sum(Transaction.amount).label("sum_minor"),
            func.count().label("n"),
        ).select_from(Transaction),
        needs,
    ).where(base.whereclause)
    stmt = stmt.group_by(Account.currency, label_id, label_name)

    answer: dict[str, CurrencyTotals] = {}
    for currency, key, name, total, count in session.execute(stmt).all():
        bucket = answer.setdefault(currency, CurrencyTotals())
        bucket.groups.append(
            Group(
                key=key if group_by is not GroupBy.month else None,
                name=name or UNSET_NAME[group_by],
                sum_minor=int(total or 0),
                count=int(count or 0),
            )
        )
        # Summed per currency, from the same integers -- never across the dict.
        bucket.total_minor += int(total or 0)
        bucket.count += int(count or 0)

    for bucket in answer.values():
        bucket.groups.sort(key=lambda g: (g.sum_minor, g.name))
    return answer


def timeseries(
    session: Session,
    household_id: str,
    *,
    bucket: Bucket = Bucket.month,
    account_id: str | None = None,
    since: Date | None = None,
    until: Date | None = None,
    search: str | None = None,
    cleared: ClearedState | None = None,
    uncategorised: bool = False,
) -> dict[str, CurrencyTotals]:
    """The same arithmetic, gathered by time instead of by thing."""
    base = txn_service.filtered(
        household_id,
        account_id=account_id,
        since=since,
        until=until,
        search=search,
        cleared=cleared,
        uncategorised=uncategorised,
    )
    label = _bucket_label(bucket)

    stmt = (
        select(
            Account.currency,
            label.label("name"),
            func.sum(Transaction.amount).label("sum_minor"),
            func.count().label("n"),
        )
        .select_from(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .where(base.whereclause)
        .group_by(Account.currency, label)
        .order_by(label)
    )

    answer: dict[str, CurrencyTotals] = {}
    for currency, name, total, count in session.execute(stmt).all():
        totals = answer.setdefault(currency, CurrencyTotals())
        totals.groups.append(
            Group(key=None, name=name, sum_minor=int(total or 0), count=int(count or 0))
        )
        totals.total_minor += int(total or 0)
        totals.count += int(count or 0)
    return answer


def balances(
    session: Session, household_id: str, *, as_of: Date | None = None
) -> list[tuple[Account, int]]:
    """What each account holds, as of a date. Per account, so never summed.

    There is deliberately no household total. Accounts carry different
    currencies and this ledger does not convert, so the only honest answer is
    the list -- and a caller wanting a total per currency can group the list,
    which is arithmetic they can see themselves doing.
    """
    stmt = (
        select(Account, func.coalesce(func.sum(Transaction.amount), 0))
        .select_from(Account)
        .outerjoin(
            Transaction,
            (Transaction.account_id == Account.id)
            & ((Transaction.date <= as_of) if as_of else (Transaction.id.is_not(None))),
        )
        .where(Account.household_id == household_id)
        .group_by(Account.id)
        .order_by(Account.sort_order, Account.name)
    )
    return [(account, int(total or 0)) for account, total in session.execute(stmt).all()]


def formatted(minor: int, currency: str) -> str:
    """The same figure as a string, for the sentence the agent writes.

    Both are returned everywhere: the integer is for arithmetic and the string
    is for prose. `YNAB — Feature Analysis` records YNAB adding `*_formatted`
    companions for exactly this reason -- a model asked for a number and a
    caption will otherwise format the number itself, in a locale it guessed.
    """
    return money.format_amount(minor, currency, with_symbol=False)


def parse_group_by(value: str) -> GroupBy:
    try:
        return GroupBy(value)
    except ValueError:
        raise ValidationError(
            f"group_by must be one of {', '.join(g.value for g in GroupBy)}"
        ) from None
