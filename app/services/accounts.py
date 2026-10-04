"""Accounts, and the balances derived from them.

Six types, each of which changes something real. The previous build had twelve
driving three behaviours, five of them loan types it never modelled -- and
opening a mortgage booked the principal as income, which told the household it
was two hundred thousand euros better off than it was.

Balances are computed in SQL, never by loading every row and summing in Python.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import countries
from ..errors import Conflict, NotFound, ValidationError
from ..models import Account, AccountType, ClearedState, Household, Transaction
from . import payees as payee_service

#: Letters only. `Intl.NumberFormat` throws on a code like `€€€` or `12A`,
#: and the client renders money on every screen, so one stored code that is
#: three characters but not three letters blanked the whole household (#193).
CURRENCY_CODE = re.compile(r"[A-Z]{3}")


def create_account(
    session: Session,
    *,
    household: Household,
    name: str,
    type: AccountType | str,
    currency: str | None = None,
    note: str | None = None,
    institution: str | None = None,
    country: str | None = None,
    opening_balance: int = 0,
    opening_date: date | None = None,
) -> Account:
    """Create an account, and record what was in it when you started.

    The opening balance is a **real transaction**, not a column on the account.
    That is what makes it show up in the register, count toward the balance by
    the same arithmetic as everything else, and be correctable later if the
    figure was wrong -- rather than being a number that only one code path
    knows about.
    """
    if not name.strip():
        raise ValidationError("an account needs a name")
    account_type = AccountType(type)
    code = (currency or household.base_currency).strip().upper()
    if not CURRENCY_CODE.fullmatch(code):
        raise ValidationError(f"{currency!r} is not a three-letter currency code")

    _refuse_taken_name(session, household.id, name)

    when = opening_date or date.today()
    if when > date.today():
        raise ValidationError("an account cannot have been opened in the future")

    account = Account(
        household_id=household.id,
        name=name.strip(),
        type=account_type,
        currency=code,
        note=note,
        institution=institution,
        country=countries.check(country),
    )
    session.add(account)
    session.flush()

    if opening_balance:
        _write_opening_balance(session, account, opening_balance, when)
    return account


def _write_opening_balance(session: Session, account: Account, amount: int, when: date) -> None:
    """Reconciled from the start: it is what the bank said, by definition."""
    from ..models import ClearedState, SystemPayee, Transaction
    from . import payees as payee_service

    # Marked, not merely named. The name is what a person sees and is free to
    # change; `system` is what a flow report reads, so that renaming this payee
    # cannot turn an account's starting balance into a month of income.
    payee = payee_service.get_or_create(
        session,
        account.household_id,
        "Opening balance",
        system=SystemPayee.opening_balance,
    )
    session.add(
        Transaction(
            household_id=account.household_id,
            account_id=account.id,
            date=when,
            amount=amount,
            payee_id=payee.id,
            memo="Opening balance",
            cleared=ClearedState.reconciled,
        )
    )
    session.flush()


def get_for_household(session: Session, account_id: str, household_id: str) -> Account:
    account = session.execute(
        select(Account).where(Account.id == account_id, Account.household_id == household_id)
    ).scalar_one_or_none()
    if account is None:
        raise NotFound("no such account")
    return account


def list_for_household(session: Session, household_id: str, *, include_closed: bool = False) -> list[Account]:
    stmt = select(Account).where(Account.household_id == household_id)
    if not include_closed:
        stmt = stmt.where(Account.closed.is_(False))
    return list(session.execute(stmt.order_by(Account.sort_order, Account.name)).scalars())


def rename(session: Session, account: Account, name: str) -> Account:
    if not name.strip():
        raise ValidationError("an account needs a name")
    _refuse_taken_name(session, account.household_id, name, except_id=account.id)
    account.name = name.strip()
    session.flush()
    return account


def _refuse_taken_name(
    session: Session, household_id: str, name: str, *, except_id: str | None = None
) -> None:
    """Conflict if another account in the household already has this name.

    Compared the way the account's transfer payee will be -- `payees.fold`,
    case and whitespace folded -- because "Transfer : <name>" sits under a
    unique constraint on the folded name. Compared exactly, "Pot" and "POT"
    were two accounts and one payee, and every transfer into the second
    failed on that constraint as a 500 (#98). Folded in Python rather than
    with SQL `lower()`, which in SQLite folds ASCII only.
    """
    wanted = payee_service.fold(name)
    for other_id, other_name in session.execute(
        select(Account.id, Account.name).where(Account.household_id == household_id)
    ):
        if other_id != except_id and payee_service.fold(other_name) == wanted:
            raise Conflict(f"there is already an account called {other_name!r}")


def set_closed(session: Session, account: Account, closed: bool) -> Account:
    account.closed = closed
    session.flush()
    return account


def balances(session: Session, account_id: str, *, as_of: date | None = None) -> dict[str, int]:
    """Working, cleared and uncleared, in one query.

    Aggregated in SQL rather than by materialising every transaction: the
    previous build loaded the whole register per account and summed in Python,
    which is an N+1 measured in rows rather than queries.
    """
    cleared_states = (ClearedState.cleared.value, ClearedState.reconciled.value)
    stmt = select(
        func.coalesce(func.sum(Transaction.amount), 0),
        func.coalesce(
            func.sum(
                func.coalesce(Transaction.amount, 0)
            ).filter(Transaction.cleared.in_(cleared_states)),
            0,
        ),
    ).where(Transaction.account_id == account_id)
    if as_of is not None:
        stmt = stmt.where(Transaction.date <= as_of)

    total, cleared = session.execute(stmt).one()
    return {"balance": total, "cleared": cleared, "uncleared": total - cleared}


@dataclass(frozen=True, slots=True)
class Activity:
    """What an account has actually seen: how much, and over what span.

    Dates, not amounts, so this is a separate read from `balances_for_household`
    rather than three more columns on it -- that one returns a dict of ints and
    is typed as one, and widening it to carry two dates would make every caller
    handle a union to get at a balance.

    An account with no rows has `transactions == 0` and both dates `None`. It is
    not left out: the Accounts screen draws a line per account whatever is in
    it, and a missing key would be an em dash that looked like a fault.
    """

    transactions: int
    oldest: date | None
    newest: date | None


def activity(session: Session, account_id: str) -> Activity:
    """One account's row count and date span.

    The single-account half of `activity_for_household`, paired with it the way
    `balances` is paired with `balances_for_household`. A route that answers
    about one account uses this rather than asking for the whole household and
    throwing the rest away -- and, more to the point, rather than leaving the
    three fields at their defaults, which would have `GET /accounts/{id}` say
    an account with five hundred rows in it has none.
    """
    count, oldest, newest = session.execute(
        select(
            func.count(Transaction.id),
            func.min(Transaction.date),
            func.max(Transaction.date),
        ).where(Transaction.account_id == account_id)
    ).one()
    return Activity(transactions=int(count), oldest=oldest, newest=newest)


def activity_for_household(session: Session, household_id: str) -> dict[str, Activity]:
    """Every account's row count and date span, in one query rather than one each.

    The same shape as `balances_for_household` and for the same reason: the
    Accounts screen draws every account at once, so a per-account query is a
    request per row on the one screen that has all of them.

    `min` and `max` over an indexed column, and the outer join is what keeps an
    account that has never been used in the answer.
    """
    rows = session.execute(
        select(
            Account.id,
            func.count(Transaction.id),
            func.min(Transaction.date),
            func.max(Transaction.date),
        )
        .select_from(Account)
        .outerjoin(Transaction, Transaction.account_id == Account.id)
        .where(Account.household_id == household_id)
        .group_by(Account.id)
    ).all()
    return {
        account_id: Activity(transactions=int(count), oldest=oldest, newest=newest)
        for account_id, count, oldest, newest in rows
    }


def balances_for_household(session: Session, household_id: str) -> dict[str, dict[str, int]]:
    """Every account's three numbers, in one query rather than one each."""
    cleared_states = (ClearedState.cleared.value, ClearedState.reconciled.value)
    rows = session.execute(
        select(
            Account.id,
            func.coalesce(func.sum(Transaction.amount), 0),
            func.coalesce(
                func.sum(Transaction.amount).filter(Transaction.cleared.in_(cleared_states)), 0
            ),
        )
        .select_from(Account)
        .outerjoin(Transaction, Transaction.account_id == Account.id)
        .where(Account.household_id == household_id)
        .group_by(Account.id)
    ).all()
    return {
        account_id: {"balance": total, "cleared": cleared, "uncleared": total - cleared}
        for account_id, total, cleared in rows
    }
