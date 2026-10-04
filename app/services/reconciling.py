"""Proving an account against a statement.

`cleared` says the bank has confirmed one row. **Reconciled says something
stronger and about a whole period**: that everything on this account up to a
date has been checked against what the bank said the balance was, and the two
agreed. Its payoff is that it bounds the search -- when a balance is wrong, you
look back to the last reconciliation rather than to the beginning.

Which is why reconciling is an act and not a checkbox: this module's one real
rule is that the act is refused unless it *balanced*:

    already locked  +  what you ticked  ==  what the bank said

Everything else here serves that sentence. The rows become locked in one batch,
so undoing the reconciliation is the same button as undoing an import.

**It is not the only way a row becomes locked**, and this said it was until
#217. An opening balance is born reconciled, and the register's transaction
panel offers "Locked" as a cleared state, so a person can lock one row by hand
(`PATCH /transactions/{id}` with `cleared: reconciled`) with no
`Reconciliation` behind it. `locked_balance` counts such a row like any other
locked one -- it reads `cleared`, not the reconciliations -- so a hand-locked
row moves the figure the next statement is measured from. That is deliberate:
the lock is the person's say-so, and the edit is in History like any other.
What this module guarantees is narrower: a reconciliation *it* records balanced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..errors import Conflict, NotFound, ValidationError
from ..models import (
    Account,
    ClearedState,
    Payee,
    Reconciliation,
    Transaction,
)


@dataclass(slots=True)
class Candidate:
    """A row that could be part of this reconciliation."""

    id: str
    date: Date
    payee: str | None
    memo: str | None
    amount: int
    cleared: str


@dataclass(slots=True)
class Worksheet:
    """Everything the screen needs to reconcile one account."""

    account_id: str
    currency: str
    #: The sum of every row already locked. The figure a new statement's
    #: closing balance is measured *from*, so it is not re-proved each time.
    locked_balance: int
    last_statement_date: Date | None
    last_statement_balance: int | None
    candidates: list[Candidate]


def locked_balance(session: Session, account_id: str, *, as_of: Date | None = None) -> int:
    """The sum of rows already reconciled -- the floor a new statement builds on."""
    stmt = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.account_id == account_id,
        Transaction.cleared == ClearedState.reconciled,
    )
    if as_of is not None:
        stmt = stmt.where(Transaction.date <= as_of)
    return session.execute(stmt).scalar_one()


def last_for(session: Session, account_id: str) -> Reconciliation | None:
    return session.execute(
        select(Reconciliation)
        .where(Reconciliation.account_id == account_id)
        .order_by(Reconciliation.statement_date.desc(), Reconciliation.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()


#: How far past the last statement an unbounded worksheet reaches (#237). A
#: statement is a month, so this is the next one and a fortnight of slack;
#: the screen always sends its closing date, so only a caller that sends none
#: meets it.
DEFAULT_REACH_DAYS = 45


def worksheet(session: Session, account: Account, *, until: Date | None = None) -> Worksheet:
    """What is still unaccounted for on this account, up to a date.

    Only rows that are *not* already locked: a reconciled row has been proved
    and is folded into `locked_balance`, so offering it again would invite
    counting it twice.

    Read as columns, not `Transaction` objects (#237): the audit hook makes
    every column fetch its history, so building 22k objects to copy five
    fields each was 617 ms and 49 MiB per open. With no ``until``, the sheet
    stops `DEFAULT_REACH_DAYS` after the last statement, when there is one.
    """
    last = last_for(session, account.id)
    if until is None and last is not None:
        until = last.statement_date + timedelta(days=DEFAULT_REACH_DAYS)

    stmt = (
        select(
            Transaction.id,
            Transaction.date,
            Payee.name,
            Transaction.memo,
            Transaction.amount,
            Transaction.cleared,
        )
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(
            Transaction.account_id == account.id,
            Transaction.cleared != ClearedState.reconciled,
        )
        .order_by(Transaction.date, Transaction.created_at)
    )
    if until is not None:
        stmt = stmt.where(Transaction.date <= until)

    candidates = [
        Candidate(
            id=txn_id,
            date=when,
            payee=payee_name,
            memo=memo,
            amount=amount,
            cleared=cleared.value,
        )
        for txn_id, when, payee_name, memo, amount, cleared in session.execute(stmt).all()
    ]

    return Worksheet(
        account_id=account.id,
        currency=account.currency,
        locked_balance=locked_balance(session, account.id),
        last_statement_date=last.statement_date if last else None,
        last_statement_balance=last.statement_balance if last else None,
        candidates=candidates,
    )


def reconcile(
    session: Session,
    account: Account,
    *,
    statement_date: Date,
    statement_balance: int,
    transaction_ids: list[str],
    batch_id: str | None = None,
) -> Reconciliation:
    """Lock the ticked rows, and record what they were proved against.

    Refuses rather than rounds. A reconciliation that does not balance is not a
    reconciliation -- it is a note that says one happened, which is worse than
    nothing because the next person will trust it and stop looking further back.
    """
    if not transaction_ids:
        raise ValidationError("tick the rows that appear on the statement first")

    wanted = set(transaction_ids)
    if len(wanted) != len(transaction_ids):
        raise ValidationError("the same row was ticked twice")

    rows = list(
        session.execute(
            select(Transaction).where(
                Transaction.id.in_(wanted), Transaction.account_id == account.id
            )
        ).scalars()
    )
    if len(rows) != len(wanted):
        raise NotFound("some of those rows are not on this account")

    for row in rows:
        if row.cleared is ClearedState.reconciled:
            raise Conflict("one of those rows is already locked by an earlier reconciliation")
        if row.date > statement_date:
            raise ValidationError(
                f"a row dated {row.date.isoformat()} is after the statement closes "
                f"on {statement_date.isoformat()}, so it cannot be on it"
            )

    floor = locked_balance(session, account.id)
    ticked = sum(row.amount for row in rows)
    difference = statement_balance - (floor + ticked)
    if difference != 0:
        raise Conflict(
            f"that does not balance: {_signed(difference, account.currency)} out. "
            "Tick or untick rows until the difference is zero, or add the transaction "
            "the statement has and the register does not."
        )

    for row in rows:
        row.cleared = ClearedState.reconciled

    record = Reconciliation(
        account_id=account.id,
        statement_date=statement_date,
        statement_balance=statement_balance,
        batch_id=batch_id,
    )
    session.add(record)
    session.flush()
    return record


def _signed(minor: int, currency: str) -> str:
    """The difference, as the message should read it.

    Imported here rather than at module scope: `app.money` is the only place
    that knows a currency's exponent, and nothing else in this module needs it.
    """
    from ..money import format_amount

    return format_amount(minor, currency)


def history(session: Session, account_id: str) -> list[Reconciliation]:
    return list(
        session.execute(
            select(Reconciliation)
            .where(Reconciliation.account_id == account_id)
            .order_by(Reconciliation.statement_date.desc())
        ).scalars()
    )
