"""Counts for the two management screens, done in SQL and in integers.

`insights.py` is the house pattern this follows: the arithmetic happens in the
database, as `func.count` over an indexed column, and the screen is handed
numbers that are already right. The alternative -- and what the category screen
used to do one query at a time -- is a request per row.

> [!note] Why the currency rule in `insights` does not bite here
> Every figure in this module is a **count of rows**, not an amount. Nothing
> here reads `Transaction.amount`, so there is no sum of two currencies to get
> wrong and no total that has to be partitioned by currency. If a money figure
> is ever added to one of these answers it inherits `insights`' rule whole:
> grouped by currency, with no grand total.

Two reads, one grouped query each:

- :func:`category_stats` -- per category, how many transactions carry it and
  how many distinct payees those transactions name, plus the busiest few of
  those payees by name.
- :func:`payee_stats` -- per payee, how many transactions name it and which
  categories they were filed under, with a count on each.

Both are a *single* grouped `select` over `transactions`, gathered by the pair
`(category, payee)` and `(payee, category)` respectively. One query answers both
halves of each screen's stat column because the per-row total is the sum of that
row's groups -- asking separately for the count and for the breakdown would be
two scans of the same rows for two views of the same numbers.

**Every row of both answers is filtered by `household_id`**, which is indexed,
and the join to `payees` / `categories` is by primary key. A household a caller
is not in never reaches here at all -- the routes are scoped by
`current_household`, which answers 404 -- but the queries are written so that
even called directly they cannot see across a household boundary.

Nothing is excluded. A transfer leg and an opening balance are rows in the
register and are counted as rows; the flow reports exclude them because adding
them to *spending* would be wrong, which is not a thing a count of transactions
can be. The payee screen hides the auto-managed transfer payees itself, because
that is a question about which payees are worth listing rather than about what
the number under one of them means.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Category, Payee, Transaction
from .insights import UNSET_NAME, GroupBy

#: How many payees a category's breakdown carries. The requirement is "a short
#: list of payees", and a category on a real ledger has hundreds -- so the list
#: is the busiest few and `more_payees` says how many were left out, rather than
#: a panel that is itself a thousand-row table.
TOP_PAYEES = 8


@dataclass(frozen=True, slots=True)
class Tally:
    """One line of a breakdown: what it is, and how many rows carry it.

    `key` is null for the unset bucket -- transactions with no payee, or none
    with no category. That is a real and common state rather than missing data,
    so it is named (from `insights.UNSET_NAME`, so the two modules call it the
    same thing) and counted, and only excluded from the *distinct* counts,
    where "no payee" is plainly not a payee.
    """

    key: str | None
    name: str
    count: int


@dataclass(slots=True)
class CategoryStat:
    """The stat column for one category, and what is behind it."""

    category_id: str
    transaction_count: int = 0
    #: Distinct real payees. Transactions with no payee are not one of them.
    payee_count: int = 0
    #: The busiest `TOP_PAYEES` of them, biggest first.
    payees: list[Tally] = field(default_factory=list)
    #: Real payees the list left out, so the screen can say "+N more" without
    #: doing arithmetic on a truncated list and a total that counts differently.
    more_payees: int = 0


@dataclass(slots=True)
class PayeeStat:
    """The stat column for one payee, and what is behind it."""

    payee_id: str
    transaction_count: int = 0
    #: Distinct real categories. Uncategorised transactions are not one.
    category_count: int = 0
    #: Every category this payee has been filed under, biggest first. Not
    #: truncated: a payee's categories are bounded by the household's category
    #: list, which is tens, and the screen has to be able to show the lot.
    categories: list[Tally] = field(default_factory=list)


def _ordered(tallies: list[Tally]) -> list[Tally]:
    """Busiest first, then by name. The name breaks the tie so two equal counts
    do not swap places between two requests over the same data."""
    return sorted(tallies, key=lambda one: (-one.count, one.name))


def category_stats(session: Session, household_id: str) -> dict[str, CategoryStat]:
    """Per category: transactions, distinct payees, and the busiest payees.

    Keyed by category id, and **categories nothing is filed under are absent**
    rather than present with zeros. The caller already has the category tree --
    this answers "what is behind each of these", and a row for a category with
    no transactions carries no information the tree does not already have.

    Uncategorised transactions are not here either: they are not a category, and
    the screen that shows this has no row to put them on.
    """
    rows = session.execute(
        select(
            Transaction.category_id,
            Transaction.payee_id,
            Payee.name,
            func.count().label("n"),
        )
        .select_from(Transaction)
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.category_id.is_not(None),
        )
        .group_by(Transaction.category_id, Transaction.payee_id, Payee.name)
    ).all()

    gathered: dict[str, list[Tally]] = {}
    totals: dict[str, int] = {}
    for category_id, payee_id, payee_name, count in rows:
        gathered.setdefault(category_id, []).append(
            Tally(
                key=payee_id,
                name=payee_name or UNSET_NAME[GroupBy.payee],
                count=int(count),
            )
        )
        totals[category_id] = totals.get(category_id, 0) + int(count)

    answer: dict[str, CategoryStat] = {}
    for category_id, tallies in gathered.items():
        ordered = _ordered(tallies)
        named = [one for one in ordered if one.key is not None]
        shown = ordered[:TOP_PAYEES]
        answer[category_id] = CategoryStat(
            category_id=category_id,
            transaction_count=totals[category_id],
            payee_count=len(named),
            payees=shown,
            more_payees=len(named) - len([one for one in shown if one.key is not None]),
        )
    return answer


def payee_stats(session: Session, household_id: str) -> dict[str, PayeeStat]:
    """Per payee: transactions, and every category they were filed under.

    Keyed by payee id, and payees with no transactions are absent for the same
    reason as above -- the screen has the payee list already.

    A payee's *uncategorised* transactions are a line in `categories` with a
    null key, because "Carrefour, 40 transactions, 31 of them uncategorised" is
    the sentence this stat exists to let somebody read. They are counted in
    `transaction_count` and not in `category_count`.
    """
    rows = session.execute(
        select(
            Transaction.payee_id,
            Transaction.category_id,
            Category.name,
            func.count().label("n"),
        )
        .select_from(Transaction)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.payee_id.is_not(None),
        )
        .group_by(Transaction.payee_id, Transaction.category_id, Category.name)
    ).all()

    gathered: dict[str, list[Tally]] = {}
    totals: dict[str, int] = {}
    for payee_id, category_id, category_name, count in rows:
        gathered.setdefault(payee_id, []).append(
            Tally(
                key=category_id,
                name=category_name or UNSET_NAME[GroupBy.category],
                count=int(count),
            )
        )
        totals[payee_id] = totals.get(payee_id, 0) + int(count)

    answer: dict[str, PayeeStat] = {}
    for payee_id, tallies in gathered.items():
        ordered = _ordered(tallies)
        answer[payee_id] = PayeeStat(
            payee_id=payee_id,
            transaction_count=totals[payee_id],
            category_count=len([one for one in ordered if one.key is not None]),
            categories=ordered,
        )
    return answer
