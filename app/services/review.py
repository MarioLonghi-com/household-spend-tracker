"""Reading the ledger to review it: rows, categorisation, observed rates (#134).

Facts for an agent to judge, never verdicts. Nothing here writes, nothing
converts between currencies, and nothing adds two currencies together: a row
carries its own currency, a rate is the one the household actually got, and
the review says which category a payee's rows *usually* carry without saying
any row is wrong.

Everything is capped and counted. A list cut short says so by carrying the
total beside it, because a shorter list that looks whole is how somebody
reviews a fifth of their ledger and believes it is all of it.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as Date
from decimal import Decimal

from sqlalchemy import Select, and_, case, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from ..models import (
    Account,
    Category,
    CategoryGroup,
    ClearedState,
    LinkSource,
    Payee,
    Receipt,
    ReimbursementState,
    Transaction,
)
from ..money import minor_factor
from . import transactions as txn_service

# --------------------------------------------------------------------------- #
# Lookups, fetched whole once per request
# --------------------------------------------------------------------------- #


def category_names(session: Session, household_id: str) -> dict[str, str]:
    """id -> "Group: Category", the form the manifest and the categorise route use."""
    rows = session.execute(
        select(Category.id, CategoryGroup.name, Category.name)
        .join(CategoryGroup, CategoryGroup.id == Category.group_id)
        .where(Category.household_id == household_id)
    ).all()
    return {row[0]: f"{row[1]}: {row[2]}" for row in rows}


def payee_names(session: Session, household_id: str) -> dict[str, str]:
    rows = session.execute(
        select(Payee.id, Payee.name).where(Payee.household_id == household_id)
    ).all()
    return {row[0]: row[1] for row in rows}


def receipt_counts(session: Session, household_id: str, ids: list[str]) -> dict[str, int]:
    """How many receipts each of these rows carries. Only the page's rows."""
    if not ids:
        return {}
    rows = session.execute(
        select(Receipt.transaction_id, func.count(Receipt.id))
        .where(Receipt.household_id == household_id, Receipt.transaction_id.in_(ids))
        .group_by(Receipt.transaction_id)
    ).all()
    return {row[0]: int(row[1]) for row in rows}


# --------------------------------------------------------------------------- #
# The register, for reading
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RegisterRow:
    id: str
    date: Date
    account_id: str
    currency: str
    amount_minor: int
    payee_id: str | None
    payee_name: str | None
    bank_text: str | None
    memo: str | None
    category_id: str | None
    category_name: str | None
    is_transfer: bool
    transfer_account_id: str | None
    split_id: str | None
    reimbursement: ReimbursementState | None
    reimbursed_by_id: str | None
    receipt_count: int
    cleared: ClearedState


@dataclass(slots=True)
class RegisterRead:
    rows: list[RegisterRow]
    total: int


def register_read(
    session: Session, household_id: str, stmt: Select, *, limit: int, offset: int
) -> RegisterRead:
    """One page of a `transactions.filtered` statement, in a stable order.

    Newest first, then by when the row was written, then by id: `date` alone
    ties several rows a day, and `created_at` ties every row one import wrote
    in the same instant -- so without the id a page boundary could fall inside
    a tie and the next page repeat or skip a row.
    """
    total = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    ordered = stmt.order_by(
        Transaction.date.desc(), Transaction.created_at.desc(), Transaction.id.desc()
    )
    rows = session.execute(
        ordered.with_only_columns(
            Transaction.id,
            Transaction.date,
            Transaction.account_id,
            Transaction.amount,
            Transaction.payee_id,
            Transaction.import_payee_original,
            Transaction.memo,
            Transaction.category_id,
            Transaction.transfer_account_id,
            Transaction.transfer_transaction_id,
            Transaction.split_id,
            Transaction.reimbursement,
            Transaction.reimbursed_by_id,
            Transaction.cleared,
        )
        .limit(limit)
        .offset(offset)
    ).all()

    currencies = txn_service.account_currencies(session, household_id)
    payees = payee_names(session, household_id)
    categories = category_names(session, household_id)
    receipts = receipt_counts(session, household_id, [row[0] for row in rows])
    return RegisterRead(
        rows=[
            RegisterRow(
                id=row[0],
                date=row[1],
                account_id=row[2],
                currency=currencies[row[2]],
                amount_minor=row[3],
                payee_id=row[4],
                payee_name=payees.get(row[4]) if row[4] else None,
                bank_text=row[5],
                memo=row[6],
                category_id=row[7],
                category_name=categories.get(row[7]) if row[7] else None,
                is_transfer=row[8] is not None or row[9] is not None,
                transfer_account_id=row[8],
                split_id=row[10],
                reimbursement=row[11],
                reimbursed_by_id=row[12],
                receipt_count=receipts.get(row[0], 0),
                cleared=row[13],
            )
            for row in rows
        ],
        total=int(total),
    )


# --------------------------------------------------------------------------- #
# Categorisation review
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CategoryShare:
    category_id: str
    full_name: str
    count: int
    #: Of the payee's *categorised* rows, rounded down. The counts are exact.
    share_percent: int


@dataclass(slots=True)
class PayeeProfile:
    payee_id: str
    payee_name: str
    row_count: int
    categorised_count: int
    uncategorised_count: int
    categories: list[CategoryShare]
    usual_category_id: str | None
    usual_category_name: str | None
    usual_share_percent: int | None


@dataclass(slots=True)
class ReviewRow:
    transaction_id: str
    date: Date
    account_id: str
    currency: str
    amount_minor: int
    payee_id: str | None
    payee_name: str | None
    bank_text: str | None
    category_id: str | None
    category_name: str | None
    usual_category_id: str | None
    usual_category_name: str | None
    usual_share_percent: int | None


@dataclass(slots=True)
class Review:
    rows_considered: int
    excluded_transfer_legs: int
    excluded_system_payee_rows: int
    payees: list[PayeeProfile] = field(default_factory=list)
    payees_total: int = 0
    outliers: list[ReviewRow] = field(default_factory=list)
    outliers_total: int = 0
    uncategorised_with_usual: list[ReviewRow] = field(default_factory=list)
    uncategorised_with_usual_total: int = 0
    uncategorised_without_usual: list[ReviewRow] = field(default_factory=list)
    uncategorised_without_usual_total: int = 0


def _percent_down(part: int, whole: int) -> int:
    return (part * 100) // whole if whole else 0


def categorisation_review(
    session: Session,
    household_id: str,
    *,
    account_id: str | None = None,
    since: Date | None = None,
    until: Date | None = None,
    min_rows: int = 3,
    dominant_percent: int = 80,
    limit: int = 100,
) -> Review:
    """Which categories each payee's rows carry, and the rows that disagree.

    A payee has a **usual** category when at least ``min_rows`` of its rows in
    the window are categorised and one category holds at least
    ``dominant_percent`` of them -- compared in integers (``count * 100 >=
    percent * categorised``), so 4 of 5 is exactly 80 and passes at 80.

    Left out, and counted: transfer legs (they have no category by design,
    #124) and rows whose payee the app made (opening balances, the transfer
    payees). Neither is a categorisation anybody could get wrong.

    The usual category is read from the same window the rows are drawn from,
    so a narrow window gives thin history -- which is why ``min_rows`` exists.

    The answer is read in SQL and scales with it, not with the ledger (#237):
    the payee profiles from one `GROUP BY payee, category`, and the three lists
    -- capped, with their totals -- from one windowed read that brings back at
    most ``limit`` rows of each. 40k rows used to be 40k tuples in memory and
    a `Counter` per payee for an answer capped at a hundred.
    """
    base = txn_service.filtered(household_id, account_id=account_id, since=since, until=until)
    is_leg = or_(
        Transaction.transfer_account_id.is_not(None),
        Transaction.transfer_transaction_id.is_not(None),
    )
    legs, system = session.execute(
        base.outerjoin(Payee, Payee.id == Transaction.payee_id).with_only_columns(
            func.coalesce(func.sum(case((is_leg, 1), else_=0)), 0),
            func.coalesce(
                func.sum(case((and_(~is_leg, Payee.system.is_not(None)), 1), else_=0)), 0
            ),
        )
    ).one()

    considered = (
        base.where(~is_leg)
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(Payee.system.is_(None))
    )
    grouped = session.execute(
        considered.with_only_columns(
            Transaction.payee_id, Transaction.category_id, func.max(Payee.name), func.count()
        ).group_by(Transaction.payee_id, Transaction.category_id)
    ).all()
    currencies = txn_service.account_currencies(session, household_id)
    categories = category_names(session, household_id)

    rows_considered = 0
    counts: dict[str, Counter] = {}
    row_counts: Counter = Counter()
    names: dict[str, str] = {}
    for payee_id, category_id, name, count in grouped:
        rows_considered += count
        if payee_id is None:
            continue
        names[payee_id] = name
        row_counts[payee_id] += count
        its = counts.setdefault(payee_id, Counter())
        if category_id is not None:
            its[category_id] += count

    profiles: list[PayeeProfile] = []
    usual: dict[str, tuple[str, int]] = {}
    for payee_id, total in row_counts.items():
        its = counts[payee_id]
        categorised = sum(its.values())
        # Most first; ties by name then id, so the answer never depends on
        # the order a dict happened to fill in.
        ranked = sorted(its.items(), key=lambda kv: (-kv[1], categories.get(kv[0], ""), kv[0]))
        top = ranked[0] if ranked else None
        if (
            top is not None
            and categorised >= min_rows
            and top[1] * 100 >= dominant_percent * categorised
        ):
            usual[payee_id] = (top[0], _percent_down(top[1], categorised))
        if total >= min_rows:
            chosen = usual.get(payee_id)
            profiles.append(
                PayeeProfile(
                    payee_id=payee_id,
                    payee_name=names[payee_id],
                    row_count=total,
                    categorised_count=categorised,
                    uncategorised_count=total - categorised,
                    categories=[
                        CategoryShare(
                            category_id=cat_id,
                            full_name=categories.get(cat_id, ""),
                            count=count,
                            share_percent=_percent_down(count, categorised),
                        )
                        for cat_id, count in ranked
                    ],
                    usual_category_id=chosen[0] if chosen else None,
                    usual_category_name=categories.get(chosen[0]) if chosen else None,
                    usual_share_percent=chosen[1] if chosen else None,
                )
            )
    profiles.sort(key=lambda p: (-p.row_count, p.payee_name.casefold(), p.payee_id))

    lists = _review_lists(session, considered, usual, limit=limit)

    def as_review(row) -> ReviewRow:
        chosen = usual.get(row.payee_id) if row.payee_id is not None else None
        return ReviewRow(
            transaction_id=row.id,
            date=row.date,
            account_id=row.account_id,
            currency=currencies[row.account_id],
            amount_minor=row.amount,
            payee_id=row.payee_id,
            payee_name=row.payee_name,
            bank_text=row.bank_text,
            category_id=row.category_id,
            category_name=categories.get(row.category_id) if row.category_id else None,
            usual_category_id=chosen[0] if chosen else None,
            usual_category_name=categories.get(chosen[0]) if chosen else None,
            usual_share_percent=chosen[1] if chosen else None,
        )

    found = {kind: [as_review(row) for row in rows] for kind, (rows, _total) in lists.items()}
    totals = {kind: total for kind, (_rows, total) in lists.items()}
    return Review(
        rows_considered=rows_considered,
        excluded_transfer_legs=int(legs),
        excluded_system_payee_rows=int(system),
        payees=profiles[:limit],
        payees_total=len(profiles),
        outliers=found[_OUTLIER],
        outliers_total=totals[_OUTLIER],
        uncategorised_with_usual=found[_WITH_USUAL],
        uncategorised_with_usual_total=totals[_WITH_USUAL],
        uncategorised_without_usual=found[_WITHOUT_USUAL],
        uncategorised_without_usual_total=totals[_WITHOUT_USUAL],
    )


_OUTLIER, _WITH_USUAL, _WITHOUT_USUAL = "outlier", "with_usual", "without_usual"


def _review_lists(
    session: Session, considered: Select, usual: dict[str, tuple[str, int]], *, limit: int
) -> dict[str, tuple[list, int]]:
    """The three lists, each newest first and cut at ``limit``, with its total.

    The usual categories go in as one JSON parameter read by `json_each`, so
    a household with thousands of payees is one bound value, not thousands.
    """
    usual_map = func.json_each(json.dumps({payee: chosen[0] for payee, chosen in usual.items()}))
    usual_table = usual_map.table_valued("key", "value").alias("usual")
    kind = case(
        (
            and_(Transaction.category_id.is_(None), usual_table.c.key.is_not(None)),
            literal(_WITH_USUAL),
        ),
        (Transaction.category_id.is_(None), literal(_WITHOUT_USUAL)),
        (
            and_(usual_table.c.key.is_not(None), Transaction.category_id != usual_table.c.value),
            literal(_OUTLIER),
        ),
        else_=None,
    ).label("kind")
    newest = (Transaction.date.desc(), Transaction.created_at.desc(), Transaction.id.desc())
    inner = (
        considered.outerjoin(usual_table, usual_table.c.key == Transaction.payee_id)
        .with_only_columns(
            Transaction.id,
            Transaction.date,
            Transaction.account_id,
            Transaction.amount,
            Transaction.payee_id,
            Transaction.import_payee_original.label("bank_text"),
            Transaction.category_id,
            Payee.name.label("payee_name"),
            kind,
            func.row_number().over(partition_by=kind, order_by=newest).label("place"),
            func.count().over(partition_by=kind).label("total"),
        )
        .subquery()
    )
    rows = session.execute(
        select(inner)
        .where(inner.c.kind.is_not(None), inner.c.place <= limit)
        .order_by(inner.c.kind, inner.c.place)
    ).all()
    out: dict[str, tuple[list, int]] = {kind: ([], 0) for kind in (_OUTLIER, _WITH_USUAL, _WITHOUT_USUAL)}
    for row in rows:
        listed, _ = out[row.kind]
        listed.append(row)
        out[row.kind] = (listed, int(row.total))
    return out


# --------------------------------------------------------------------------- #
# Observed exchange rates
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Observed:
    date: Date
    out_transaction_id: str
    in_transaction_id: str
    from_account_id: str
    from_currency: str
    #: Positive: what left the source account.
    from_amount_minor: int
    to_account_id: str
    to_currency: str
    #: Positive: what arrived.
    to_amount_minor: int
    #: Units of `to_currency` per one unit of `from_currency`, as a decimal string.
    rate: str
    link_source: LinkSource | None


def implied_rate(from_minor: int, from_currency: str, to_minor: int, to_currency: str) -> str:
    """The rate two legs imply, by the formula the linker stores (`transfers.link`).

    Decimal throughout and handed back as a string, so it is the ledger's own
    figure rather than one a float rounded on the way out.
    """
    rate = (Decimal(to_minor) / minor_factor(to_currency)) / (
        Decimal(from_minor) / minor_factor(from_currency)
    )
    return str(rate)


def observed_rates(
    session: Session,
    household_id: str,
    *,
    since: Date | None = None,
    until: Date | None = None,
    currencies: tuple[str, str] | None = None,
) -> list[Observed]:
    """Every linked transfer pair whose two accounts hold different currencies.

    One entry per pair, read from the leg money left: that leg's date is the
    entry's date and its account is the `from` side. The rate is recomputed
    from the two legs' amounts -- the stored `transfer_fx_rate` is a
    consequence of them, and the amounts are what the bank statements said.

    ``currencies`` narrows to one pair in **either** direction: EUR->GBP and
    GBP->EUR are both evidence about the EUR/GBP rate.
    """
    out_leg = Transaction
    in_leg = aliased(Transaction)
    source = aliased(Account)
    destination = aliased(Account)
    stmt = (
        select(
            out_leg.date,
            out_leg.id,
            in_leg.id,
            source.id,
            source.currency,
            out_leg.amount,
            destination.id,
            destination.currency,
            in_leg.amount,
            out_leg.link_source,
        )
        .join(in_leg, in_leg.id == out_leg.transfer_transaction_id)
        .join(source, source.id == out_leg.account_id)
        .join(destination, destination.id == in_leg.account_id)
        .where(
            out_leg.household_id == household_id,
            in_leg.household_id == household_id,
            out_leg.amount < 0,
            in_leg.amount > 0,
            source.currency != destination.currency,
        )
        .order_by(out_leg.date.desc(), out_leg.id)
    )
    if since:
        stmt = stmt.where(out_leg.date >= since)
    if until:
        stmt = stmt.where(out_leg.date <= until)
    if currencies:
        a, b = currencies
        stmt = stmt.where(
            or_(
                (source.currency == a) & (destination.currency == b),
                (source.currency == b) & (destination.currency == a),
            )
        )
    return [
        Observed(
            date=row[0],
            out_transaction_id=row[1],
            in_transaction_id=row[2],
            from_account_id=row[3],
            from_currency=row[4],
            from_amount_minor=-row[5],
            to_account_id=row[6],
            to_currency=row[7],
            to_amount_minor=row[8],
            rate=implied_rate(-row[5], row[4], row[8], row[7]),
            link_source=row[9],
        )
        for row in session.execute(stmt).all()
    ]
