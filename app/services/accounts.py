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

from .. import countries, currencies
from ..errors import Conflict, NotFound, ValidationError
from ..models import (
    Account,
    AccountType,
    Batch,
    BatchKind,
    BatchStatus,
    Change,
    ChangeOp,
    ClearedState,
    Household,
    Payee,
    Reconciliation,
    SystemPayee,
    Transaction,
)
from . import payees as payee_service
from . import transactions as transaction_service

#: Letters only. `Intl.NumberFormat` throws on a code like `€€€` or `12A`,
#: and the client renders money on every screen, so one stored code that is
#: three characters but not three letters blanked the whole household (#193).
CURRENCY_CODE = re.compile(r"[A-Z]{3}")


def codes_in_use(session: Session, household: Household) -> set[str]:
    """Every currency code this household already holds: its base and its accounts'.

    What `currencies.refusal` accepts whatever it is, so a ledger holding a code
    from before the ISO check can still open another account in it (#110).
    """
    held = set(
        session.execute(
            select(Account.currency).where(Account.household_id == household.id).distinct()
        ).scalars()
    )
    return held | {household.base_currency}


def free_text(value: str | None) -> str | None:
    """An account's bank or note as it is stored: trimmed, and null when blank.

    One spelling for "none" (#20). The edit panel once stored an emptied field
    as `""` beside the new-account panel's null, so anything asking for
    accounts with no bank -- a filter, an export -- missed half of them. Done
    here rather than in each client, so no caller can write `""` again.
    """
    return (value or "").strip() or None


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
    currencies.check_new(code, codes_in_use(session, household))

    _refuse_taken_name(session, household.id, name)

    when = opening_date or date.today()
    if when > date.today():
        raise ValidationError("an account cannot have been opened in the future")

    account = Account(
        household_id=household.id,
        name=name.strip(),
        type=account_type,
        currency=code,
        note=free_text(note),
        institution=free_text(institution),
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
    #
    # A household seeded in another language already has its marked payee,
    # in that language (#268); the row's memo follows the payee's word. An
    # English household finds or makes "Opening balance" by name, as before.
    seeded = _seeded_opening_payee(session, account.household_id)
    payee = seeded or payee_service.get_or_create(
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
            memo=seeded.name if seeded is not None else "Opening balance",
            cleared=ClearedState.reconciled,
        )
    )
    session.flush()


def _seeded_opening_payee(session: Session, household_id: str) -> Payee | None:
    """The opening-balance payee a seed in another language made, if there is one.

    Found by its mark *and* by being one of the catalog's reviewed words for
    "Opening balance" -- the word `categories.seed_defaults` gave it. Nothing
    else qualifies: an English household, and one whose person renamed the
    payee to a word of their own, find or make "Opening balance" by name
    exactly as they did before #268. Until a language is reviewed there are no
    such words, and this is always None.
    """
    from .. import seed_words
    from ..models import SystemPayee

    words = {payee_service.fold(one) for one in seed_words.translations(seed_words.OPENING_BALANCE)}
    if not words:
        return None
    marked = session.execute(
        select(Payee)
        .where(
            Payee.household_id == household_id,
            Payee.system == SystemPayee.opening_balance,
        )
        .order_by(Payee.name)
    ).scalars().all()
    if any(one.name_folded == payee_service.fold("Opening balance") for one in marked):
        return None
    return next((one for one in marked if one.name_folded in words), None)


#: The panel offers a date and a figure side by side, so a date typed with no
#: figure is the one combination with nowhere to go: the date lives on the
#: opening-balance row, and an account opened empty has none (#10).
NO_OPENING_TO_DATE = "this account has no opening balance; set one to give it a date"

#: A warning, never a refusal. Rows older than the opening balance are legal
#: -- a statement imported from further back than tracking started -- but the
#: balance on any day before the opening date then leaves the starting figure
#: out, which nothing else on the screen would say.
OPENS_AFTER_ITS_ROWS = (
    "the opening date is after this account's earliest transaction ({oldest}); "
    "the balance on any day before {opening} leaves the opening balance out"
)


#: A recorded reconciliation is a balance the bank confirmed, and the opening
#: row is part of what it summed: `reconciling.locked_balance` is the floor the
#: next statement is measured from, and the opening row is in it. Moving its
#: figure, or its date across a statement's, would leave that statement no
#: longer matching the ledger and the next one refused as out by the
#: difference, with nothing pointing at why. Unlocking a row in the register is
#: a deliberate act; this panel skips that step, so it refuses instead.
OPENING_ALREADY_PROVED = (
    "this account was reconciled against a statement dated {latest}, on or after "
    "the opening date, so changing the opening balance or its date would move a "
    "balance the bank has already confirmed. Undo {which} in History first, then "
    "change the opening balance"
)

#: The panel edits the opening balance past its lock, and that is only safe on
#: the row the app wrote as one. These say which other row it found instead.
OPENING_IS_A_TRANSFER = (
    "the opening-balance row dated {date} is linked as one side of a transfer; "
    "unlink it in the register before changing the opening balance here"
)
OPENING_RELOCKED = (
    "the opening-balance row dated {date} has since been locked by a reconciliation; "
    "undo that reconciliation in History before changing it here"
)
OPENING_NOT_WRITTEN_AS_ONE = (
    "the row dated {date} carries the opening-balance payee but was not written as "
    "this account's opening balance; change it, or its payee, in the register instead"
)


@dataclass(frozen=True, slots=True)
class Opening:
    """An account's opening-balance row: which one, when, and how much."""

    transaction_id: str
    date: date
    amount: int


def _opening_rows():
    """Opening-balance rows, oldest first, found by the payee's mark.

    By `Payee.system`, never by the name "Opening balance": a person may
    rename that payee, and a row they typed against a payee they happened to
    call that is not one.

    Nothing stops an account having two -- the register can duplicate the row,
    or a person can choose the payee on a new one. The earliest, by date and
    then by when it was written, is *the* opening balance: it is the one that
    marks where the account's history starts, and the others are ordinary
    rows that share its payee. Ordered here so every reader agrees. Being
    found here is enough to be *shown* as the opening balance; `set_opening`
    asks more before it edits one (`_refuse_unless_written_as_opening`).
    """
    return (
        select(Transaction)
        .join(Payee, Payee.id == Transaction.payee_id)
        .where(Payee.system == SystemPayee.opening_balance)
        .order_by(Transaction.date, Transaction.created_at, Transaction.id)
    )


def _opening_row(session: Session, account_id: str) -> Transaction | None:
    return (
        session.execute(_opening_rows().where(Transaction.account_id == account_id).limit(1))
        .scalars()
        .first()
    )


def opening(session: Session, account_id: str) -> Opening | None:
    """One account's opening balance, or None when it was opened empty."""
    row = _opening_row(session, account_id)
    return Opening(row.id, row.date, row.amount) if row is not None else None


def opening_for_household(session: Session, household_id: str) -> dict[str, Opening]:
    """Every account's opening balance in one query, as `balances_for_household`.

    The rows come back oldest first, so the first one seen for an account is
    the one `_opening_rows` says is its opening balance. A household has one
    such row per account, give or take a duplicate, so reading them all costs
    about as much as the accounts themselves.
    """
    stmt = (
        _opening_rows()
        .where(Transaction.household_id == household_id)
        .with_only_columns(
            Transaction.account_id, Transaction.id, Transaction.date, Transaction.amount
        )
    )
    found: dict[str, Opening] = {}
    for account_id, transaction_id, when, amount in session.execute(stmt):
        found.setdefault(account_id, Opening(transaction_id, when, amount))
    return found


def opening_warnings(found: Opening | None, oldest: date | None) -> list[str]:
    """What is worth saying about an opening date, given the account's oldest row.

    `oldest` is over every row, the opening balance included, so it is earlier
    than the opening date exactly when some other row is -- no second query to
    leave the opening row out.
    """
    if found is None or oldest is None or oldest >= found.date:
        return []
    return [
        OPENS_AFTER_ITS_ROWS.format(oldest=oldest.isoformat(), opening=found.date.isoformat())
    ]


def set_opening(
    session: Session,
    account: Account,
    *,
    amount: int | None = None,
    when: date | None = None,
) -> None:
    """Change the opening balance, its date, or both, on the row that holds them (#10).

    Both None leaves it alone, as everywhere else on an account's PATCH. The
    caller's batch is the only one: the account's other edits and this land as
    one act, so one undo puts every one of them back.

    The row is edited through `transactions.update` with `allow_locked`, so it
    stays reconciled and keeps its system payee -- what made it an opening
    balance -- without the person unlocking and relocking it by hand. Its
    cleared state is not touched: if somebody did unlock it in the register,
    that was their choice and this does not reverse it.

    - **No row and a figure that is not zero** writes one, dated `when` or,
      failing that, the account's earliest transaction or today. The earliest
      row rather than today, because a starting figure dated after the history
      it started is exactly what `OPENS_AFTER_ITS_ROWS` warns about.
    - **A figure or date that moves anything is refused** (`Conflict`) while a
      recorded reconciliation is dated on or after the earlier of the old and
      the new opening dates -- `OPENING_ALREADY_PROVED`. The lock is skipped
      here, so this is what stands in for the person unlocking it on purpose.
    - **Only the row the app wrote as the opening balance is edited.** A row a
      person gave the opening-balance payee, one relocked by a reconciliation
      and a transfer leg are each refused: see `_refuse_unless_written_as_opening`.
    - **A row and a zero figure** deletes the row, through the service's own
      hard delete, so the batch's before-image is what an undo restores. A
      zero row would keep a date, but it would be a line in the register that
      moves no money and that `create_account` itself never writes -- two
      ways for an empty start to look, where now there is one.
    - **No row and a date alone** is refused with `NO_OPENING_TO_DATE`. The
      date has nowhere to live without a column, and inventing one would be a
      second stored number for a fact the row already holds.
    """
    if amount is None and when is None:
        return
    if when is not None and when > date.today():
        raise ValidationError("an account cannot have been opened in the future")

    row = _opening_row(session, account.id)
    if row is None:
        if not amount:
            if when is not None:
                raise ValidationError(NO_OPENING_TO_DATE)
            return
        oldest = activity(session, account.id).oldest
        written = when or oldest or date.today()
        _refuse_if_proved(session, account, written)
        _write_opening_balance(session, account, amount, written)
        return

    moves_amount = amount is not None and amount != row.amount
    moves_date = when is not None and when != row.date
    if not moves_amount and not moves_date:
        return
    _refuse_unless_written_as_opening(session, row)
    _refuse_if_proved(session, account, min(row.date, when or row.date))

    if amount == 0:
        transaction_service.delete(session, row, allow_locked=True)
        return
    transaction_service.update(session, row, date=when, amount=amount, allow_locked=True)


def _refuse_if_proved(session: Session, account: Account, since: date) -> None:
    """Conflict if a recorded reconciliation is dated on or after `since`.

    `since` is the earlier of the old and the new opening dates: a statement
    before both never counted the opening row and still will not, so it is
    left alone, and anything from there on did or will. A reconciliation that
    was undone is gone -- its row is in the batch undo deleted -- so only one
    that still stands refuses.
    """
    count, latest = session.execute(
        select(func.count(Reconciliation.id), func.max(Reconciliation.statement_date)).where(
            Reconciliation.account_id == account.id,
            Reconciliation.statement_date >= since,
        )
    ).one()
    if count:
        raise Conflict(
            OPENING_ALREADY_PROVED.format(
                latest=latest.isoformat(),
                which="that reconciliation" if count == 1 else f"those {count} reconciliations",
            )
        )


def _refuse_unless_written_as_opening(session: Session, row: Transaction) -> None:
    """Conflict unless `row` is the opening balance the app itself wrote.

    `_opening_row` finds the earliest row with the opening-balance payee, and
    a person can put that payee on any row. `set_opening` edits past the lock,
    so it must only ever reach the row `_write_opening_balance` (or the demo
    seed, by hand) wrote. No column says so, and none is added: the audit log
    already does. That row's first entry is an insert that was **born
    reconciled** with the system payee on it -- an imported or typed row
    starts uncleared, and one locked later, by hand or by a statement, shows
    up as an update. (A row typed in the register as locked from the start,
    against that payee, passes: that is a person saying it is an opening
    balance, in as many words.) The log carries every insert from the first
    commit, as `households.transactions_logged` relies on, so a row with no
    insert in it is refused rather than guessed at.

    Two more ways the born-locked row stops being safe to edit here:

    - it has been through a `reconcile` batch since -- unlocked by hand, then
      ticked against a statement -- so its lock is now a statement's proof;
    - it is a transfer leg, and `transactions.delete(..., allow_locked=True)`
      would take the other leg, in another account, with it.
    """
    when = row.date.isoformat()
    if row.transfer_transaction_id or row.transfer_account_id:
        raise Conflict(OPENING_IS_A_TRANSFER.format(date=when))

    first = session.execute(
        select(Change.op, Change.after)
        .where(Change.table_name == Transaction.__tablename__, Change.row_id == row.id)
        .order_by(Change.seq)
        .limit(1)
    ).first()
    born_locked = (
        first is not None
        and first.op is ChangeOp.insert
        and (first.after or {}).get("cleared") == ClearedState.reconciled.value
        and (first.after or {}).get("payee_id") == row.payee_id
    )
    if not born_locked:
        raise Conflict(OPENING_NOT_WRITTEN_AS_ONE.format(date=when))

    relocked = session.execute(
        select(Change.seq)
        .join(Batch, Batch.id == Change.batch_id)
        .where(
            Change.table_name == Transaction.__tablename__,
            Change.row_id == row.id,
            Batch.kind == BatchKind.reconciled,
            # An undone reconciliation took its lock back with it.
            Batch.status != BatchStatus.undone,
        )
        .limit(1)
    ).first()
    if relocked is not None:
        raise Conflict(OPENING_RELOCKED.format(date=when))


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
