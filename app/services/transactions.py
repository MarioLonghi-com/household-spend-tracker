"""The register: creating, editing and deleting transactions, and transfers.

Every balance is a sum over this table, so it is the thing to protect. Two rules
carry most of the weight:

- a locked row (stored as `reconciled`) refuses every change, because "I have
  checked this against the bank" is a statement someone made and an edit would
  quietly unmake it;
- a transfer is one event in two rows, and the two must never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal

from sqlalchemy import and_, false, or_, select
from sqlalchemy.orm import Session, aliased

from ..errors import Conflict, CurrencyMismatch, NotFound, ValidationError
from ..models import (
    Account,
    Category,
    ClearedState,
    LinkSource,
    Payee,
    RegisterSource,
    ReimbursementState,
    ReimbursementView,
    Transaction,
    new_id,
)
from ..money import format_amount, magnitude_span, minor_factor
from . import categories as category_service
from . import payees as payee_service

#: Distinguishes "leave this alone" from "set this to nothing". One object,
#: defined once -- the previous build defined a second one in its router, so
#: "leave it alone" never matched and the sentinel was written into the column.
UNSET = object()


#: One sentence, because three doors refuse the same thing: the PATCH, the
#: register's bulk edit and the agent's re-categorisation.
TRANSFER_HAS_NO_CATEGORY = "a transfer has no category"


def is_transfer_leg(txn: Transaction) -> bool:
    """Either link column makes a row one side of a transfer.

    Both, not just ``transfer_account_id``: the pair is written in two steps
    and the id is ``ON DELETE SET NULL``, so a row carrying only one of the two
    is still a leg rather than spending.
    """
    return bool(txn.transfer_account_id or txn.transfer_transaction_id)


def _assert_editable(txn: Transaction) -> None:
    if txn.cleared is ClearedState.reconciled:
        raise Conflict("that transaction is locked; set it back to cleared before editing it")


def get_for_household(session: Session, transaction_id: str, household_id: str) -> Transaction:
    txn = session.execute(
        select(Transaction).where(
            Transaction.id == transaction_id, Transaction.household_id == household_id
        )
    ).scalar_one_or_none()
    if txn is None:
        raise NotFound("no such transaction")
    return txn


def create(
    session: Session,
    *,
    account: Account,
    date: Date,
    amount: int,
    payee: Payee | None = None,
    category: Category | None | object = UNSET,
    memo: str | None = None,
    cleared: ClearedState | str = ClearedState.uncleared,
    import_id: str | None = None,
    import_payee_original: str | None = None,
    import_source: str | None = None,
    import_alt_ids: list[str] | None = None,
    import_id_checked: bool = False,
    flush: bool = True,
) -> Transaction:
    """A new row in the register.

    Two switches exist for the importer alone, which writes hundreds of rows in
    one act and was paying a statement per row for each of them (issue #101):

    - ``import_id_checked`` skips the clash query. The importer has already
      read every `import_id` the account carries and keeps that set current as
      it writes, and `uq_transactions_account_import` refuses a clash at flush
      whatever this function does -- so the query only repeated a check that
      had been made, once per row.
    - ``flush=False`` leaves the row pending, for the caller to flush many at
      once. The id is assigned at construction, so the returned row can be
      pointed at before it is written.
    """
    if payee is not None and payee.household_id != account.household_id:
        raise ValidationError("that payee belongs to a different household")

    # UNSET, not None: "no category was given, work one out from the payee" and
    # "deliberately leave this uncategorised" are different requests, and only
    # the first should consult the payee's rule.
    if category is UNSET:
        category = category_service.decide(session, payee)
    if category is not None and category.household_id != account.household_id:
        raise ValidationError("that category belongs to a different household")

    if import_id is not None and not import_id_checked:
        clash = session.execute(
            select(Transaction).where(
                Transaction.account_id == account.id, Transaction.import_id == import_id
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise Conflict("that statement line is already in this account")

    txn = Transaction(
        household_id=account.household_id,
        account_id=account.id,
        date=date,
        amount=amount,
        payee_id=payee.id if payee else None,
        category_id=category.id if category else None,
        memo=memo,
        cleared=ClearedState(cleared),
        import_id=import_id,
        import_payee_original=import_payee_original,
        import_source=import_source,
        import_alt_ids=import_alt_ids or None,
    )
    session.add(txn)
    if flush:
        session.flush()
    return txn


def update(
    session: Session,
    txn: Transaction,
    *,
    date: Date | None = None,
    amount: int | None = None,
    payee: Payee | None | object = UNSET,
    category: Category | None | object = UNSET,
    memo: str | None | object = UNSET,
    cleared: ClearedState | str | None = None,
    allow_locked: bool = False,
    flush: bool = True,
) -> Transaction:
    """Edit one row. ``flush=False`` leaves the change pending, for a caller
    changing many rows in one act to flush once (#236), as `create()` allows.
    """
    # Downgrading `cleared` is the one edit a locked row must accept: it is the
    # act of unlocking. Blocking it made every reconciled row permanently
    # uncorrectable -- and opening balances are born reconciled, so each
    # account's anchor figure was write-once and a typo in it was forever. The
    # register even told the user to "set it back to cleared to edit it", which
    # the service refused.
    only_unlocking = (
        cleared is not None
        and ClearedState(cleared) is not ClearedState.reconciled
        and date is None
        and amount is None
        and memo is UNSET
        and payee is UNSET
        and category is UNSET
    )
    if not allow_locked and not only_unlocking:
        _assert_editable(txn)

    if date is not None:
        txn.date = date
    if memo is not UNSET:
        txn.memo = memo
    if payee is not UNSET:
        if payee is not None and payee.household_id != txn.household_id:
            raise ValidationError("that payee belongs to a different household")
        txn.payee_id = payee.id if payee else None
    if category is not UNSET:
        if category is not None and category.household_id != txn.household_id:
            raise ValidationError("that category belongs to a different household")
        # A transfer is not spending, which is why linking clears the category
        # (`transfers.link`). Letting one be set again afterwards made the leg
        # count as spending in every report, and broke "a categorised row is not
        # a transfer" (#124, #125). Emptying one is still allowed: that is only
        # ever a leg being put right.
        if category is not None and is_transfer_leg(txn):
            raise ValidationError(TRANSFER_HAS_NO_CATEGORY)
        txn.category_id = category.id if category else None
    if cleared is not None:
        txn.cleared = ClearedState(cleared)
    if amount is not None and amount != txn.amount:
        # A work expense is money out and its payment is money in; the report
        # and Income v Expense both lean on that. An edit that flips the sign
        # of either would leave a link neither rule allows, so it is refused
        # rather than silently producing one.
        flips = (amount > 0) != (txn.amount > 0) or amount == 0
        if flips and txn.reimbursement is not None:
            raise ValidationError(SIGN_CHANGE_ON_WORK_EXPENSE)
        if flips and is_payment(session, txn):
            raise ValidationError(SIGN_CHANGE_ON_PAYMENT)
        txn.amount = amount
        _mirror(session, txn, amount=amount)
    if date is not None:
        # The two legs are one movement of money. Mirroring the amount but not
        # the date left a transfer half-landed: for seven weeks every as-of
        # balance in the household showed money that had left one account and
        # not yet arrived in the other.
        _mirror(session, txn, date=date)

    if flush:
        session.flush()
    return txn


# --------------------------------------------------------------------------- #
# Reimbursement: a work expense, and the payment that paid it back
# --------------------------------------------------------------------------- #


#: The sentences the rules refuse with. Named once, because the panel, the
#: bulk edit and the link-many route all say them, and a test reads them.
NOT_MONEY_OUT = (
    "only money leaving an account can be a work expense; this row is money coming in"
)
TRANSFER_IS_NOT_A_WORK_EXPENSE = (
    "this is one leg of a transfer -- money moving between your own accounts. "
    "Flag the purchase it paid for instead."
)
WRITTEN_OFF_CANNOT_BE_PAID = (
    "this was written off. Set it back to expected before recording a payment for it."
)
PAID_CANNOT_BE_WRITTEN_OFF = (
    "this has been paid back. Take the payment off it before writing it off."
)
SIGN_CHANGE_ON_WORK_EXPENSE = (
    "this is a work expense, so it has to stay money going out. "
    "Set it to not a work expense first if the amount really changed direction."
)
SIGN_CHANGE_ON_PAYMENT = (
    "this row paid work expenses back, so it has to stay money coming in. "
    "Take the expenses off it first if the amount really changed direction."
)
SPLIT_A_PAYMENT = (
    "this payment repaid work expenses. Take them off it before splitting it, "
    "then link each one to the part that paid it."
)
NOT_A_WORK_EXPENSE_CANNOT_BE_PAID = (
    "a row that is not a work expense cannot have been paid back by anything"
)


def set_reimbursement(
    session: Session,
    txn: Transaction,
    *,
    state: ReimbursementState | None | object = UNSET,
    settled_by: Transaction | None | object = UNSET,
    flush: bool = True,
    payments: set[str] | None = None,
) -> Transaction:
    """Flag a row as a work expense, or say which payment paid it back.

    The only writer of the two columns. Deliberately **not** part of
    `update()`, and deliberately does **not** call `_assert_editable`: a
    reconciled row is one the bank and I agree about, and whether an employer
    will pay it back is not a fact about the money -- it is usually decided
    long after the statement was ticked off. A separate function also keeps
    `update()`'s `only_unlocking` conjunction untouched, which a new parameter
    there would silently break.

    `UNSET` rather than None for both, as everywhere in this module: "leave it
    alone" and "empty it" are different requests.

    Two states are impossible and this is where they are made so: a link on a
    row that is not a work expense (clearing the state clears the link), and a
    link on a written-off row (refused, 409, whichever of the two arrives
    second -- writing off a repaid row is refused too, unless the same call
    takes the payment off). Linking a row nobody had flagged
    flags it `expected` -- finding the payment first and working back is the
    common way round.

    There is no date rule. A payment may come before the expense (an advance).

    ``flush=False`` and ``payments`` are for a caller changing many rows in
    one act (#236): the first leaves the change pending, the second is the
    ids among them that repaid something, read in one query, so the claim
    check does not ask once per row. Pass ``flush=False`` only where no row
    written in the same loop can change what ``is_payment`` answers -- a link
    it has not flushed is invisible to that query.
    """
    if state is not UNSET and state is not None:
        state = ReimbursementState(state)
        _assert_claimable(session, txn, payments=payments)
    linking = settled_by is not UNSET and settled_by is not None
    unlinking = settled_by is None
    final = txn.reimbursement if state is UNSET else state

    if linking:
        _assert_settleable(txn, settled_by)
        if final is ReimbursementState.written_off:
            raise Conflict(WRITTEN_OFF_CANNOT_BE_PAID)
        if state is None:
            raise ValidationError(NOT_A_WORK_EXPENSE_CANNOT_BE_PAID)
        if final is None:
            # Linking a row nobody had flagged is the common way round: the
            # payment is found first and worked back from. It is the flag.
            _assert_claimable(session, txn, payments=payments)
            state = ReimbursementState.expected
    elif (
        final is ReimbursementState.written_off
        and txn.reimbursed_by_id is not None
        and not unlinking
    ):
        raise Conflict(PAID_CANNOT_BE_WRITTEN_OFF)

    if state is not UNSET:
        txn.reimbursement = state
        if state is None:
            # Clearing the flag clears the link with it: a row pointing at a
            # payment while claiming not to be a work expense is one of the two
            # impossible states, and this is where it is made so.
            txn.reimbursed_by_id = None
    if linking:
        txn.reimbursed_by_id = settled_by.id
    elif unlinking:
        txn.reimbursed_by_id = None

    if flush:
        session.flush()
    return txn


def _assert_claimable(session: Session, txn: Transaction, *, payments: set[str] | None = None) -> None:
    """Only money leaving an account, and never a transfer leg (spec §5.1).

    Nor a row that already paid something back. That is money coming in, so
    the sign test usually refuses it first -- but an edit can turn a payment's
    amount negative afterwards, and a row that is both a claim and its
    repayment makes every figure in the report ambiguous.
    """
    if is_transfer_leg(txn):
        raise ValidationError(TRANSFER_IS_NOT_A_WORK_EXPENSE)
    if txn.amount >= 0:
        raise ValidationError(NOT_MONEY_OUT)
    if (txn.id in payments) if payments is not None else is_payment(session, txn):
        raise ValidationError(
            "this row paid work expenses back, so it cannot be one itself"
        )


def _assert_settleable(txn: Transaction, settlement: Transaction) -> None:
    """Money arriving, in this household, not a transfer, not itself, and not
    itself a work expense (spec §5.2).

    Another household's row is NotFound, not a refusal: the caller has not
    shown they can see it, so they do not learn that it exists.
    """
    if settlement.household_id != txn.household_id:
        raise NotFound("no such transaction")
    if settlement.id == txn.id:
        raise ValidationError("a transaction cannot reimburse itself")
    if is_transfer_leg(settlement):
        raise ValidationError("a transfer between your own accounts is not a reimbursement")
    # Before the sign: a flagged row is money going out, so it would otherwise
    # be refused as "not money arriving", which is true and misses the point.
    if settlement.reimbursement is not None:
        raise ValidationError(
            "that row is itself a work expense, so it cannot also be what paid one back"
        )
    if settlement.amount <= 0:
        raise ValidationError("a reimbursement is money arriving, so pick money coming in")


def claimable(session: Session, txn: Transaction, *, payments: set[str] | None = None) -> bool:
    """Whether this row could be flagged, without the sentence -- for a bulk
    edit that skips what it cannot flag rather than refusing the selection."""
    try:
        _assert_claimable(session, txn, payments=payments)
    except ValidationError:
        return False
    return True


def named(session: Session, txn: Transaction) -> str:
    """A row as a person would pick it out of the register, for a refusal
    that has to say *which* of many rows it was about."""
    account = session.get(Account, txn.account_id)
    currency = account.currency if account else "EUR"
    where = f" in {account.name}" if account else ""
    return f"{format_amount(txn.amount, currency)}{where} on {txn.date.isoformat()}"


def link_reimbursements(
    session: Session, expenses: list[Transaction], settlement: Transaction
) -> list[Transaction]:
    """Point many expenses at one payment, all or nothing.

    Every row is checked before any is written, so one refusal leaves the
    whole selection as it was -- and the refusal names the row, because "this
    is money coming in" about one of twelve rows is a puzzle, not an answer.
    The caller's batch makes it one act and one undo either way.

    Which of them repaid something is read once for all of them, and the
    rows are flushed together: two `is_payment` queries and a flush per
    expense were 802 statements for 200 (#236).
    """
    payments = payments_among(session, [txn.id for txn in expenses])
    for txn in expenses:
        try:
            _assert_settleable(txn, settlement)
            _assert_claimable(session, txn, payments=payments)
            if txn.reimbursement is ReimbursementState.written_off:
                raise Conflict(WRITTEN_OFF_CANNOT_BE_PAID)
        except NotFound:
            raise
        except (ValidationError, Conflict) as refused:
            raise type(refused)(f"{named(session, txn)}: {refused}") from None
    for txn in expenses:
        set_reimbursement(session, txn, settled_by=settlement, flush=False, payments=payments)
    session.flush()
    return expenses


def payments_among(session: Session, ids: list[str]) -> set[str]:
    """Which of these rows some row names as what paid it back. One query."""
    if not ids:
        return set()
    return set(
        session.execute(
            select(Transaction.reimbursed_by_id)
            .where(Transaction.reimbursed_by_id.in_(ids))
            .distinct()
        ).scalars()
    )


def is_payment(session: Session, txn: Transaction) -> bool:
    """Whether some row names this one as what paid it back."""
    return (
        session.execute(
            select(Transaction.id).where(Transaction.reimbursed_by_id == txn.id).limit(1)
        ).first()
        is not None
    )


def delete(session: Session, txn: Transaction, *, allow_locked: bool = False) -> None:
    """Hard delete. The audit log's before-image is how it comes back."""
    if not allow_locked:
        _assert_editable(txn)

    other = _counterpart(session, txn)
    if other is not None:
        if not allow_locked:
            _assert_editable(other)
        # Half a transfer is money that appeared from nowhere on the other side.
        # The links are left alone: the foreign key is ON DELETE SET NULL, so
        # clearing them first only added an update to the batch that undo then
        # had to replay after re-inserting the rows.
        session.delete(other)
    session.delete(txn)
    session.flush()


def _counterpart(session: Session, txn: Transaction) -> Transaction | None:
    if not txn.transfer_transaction_id:
        return None
    return session.get(Transaction, txn.transfer_transaction_id)


def _mirror(session: Session, txn: Transaction, *, amount: int | None = None, date: Date | None = None) -> None:
    """Keep both legs of a transfer in step.

    The amount is equal and opposite; the date is simply the same. `cleared` is
    deliberately *not* mirrored -- real banks clear the two sides on different
    days, and forcing them together would misreport the cleared balance rather
    than fix it.
    """
    other = _counterpart(session, txn)
    if other is None:
        return
    if date is not None:
        _assert_editable(other)
        other.date = date
        return
    # The lock is on the row, not on the way in. Editing this leg rewrites the
    # other one, so a reconciled counterpart would otherwise have its amount
    # changed by going around it.
    _assert_editable(other)
    source = session.get(Account, txn.account_id)
    destination = session.get(Account, other.account_id)
    if source and destination and source.currency != destination.currency:
        raise ValidationError(
            "edit each leg of a cross-currency transfer on its own; we will not re-derive a rate"
        )
    other.amount = -txn.amount


def create_transfer(
    session: Session,
    *,
    source: Account,
    destination: Account,
    date: Date,
    amount: int,
    to_amount: int | None = None,
    memo: str | None = None,
    cleared: ClearedState | str = ClearedState.uncleared,
) -> tuple[Transaction, Transaction]:
    """Move money between two accounts as one linked, mirrored pair.

    ``amount`` is the positive magnitude leaving ``source``. Across currencies
    both legs stay exact in their own, and the implied rate is stored on the
    pair -- so the difference is a reporting question rather than a silent loss.
    """
    if source.id == destination.id:
        raise ValidationError("an account cannot transfer to itself", code="transfer.same_account")
    if source.household_id != destination.household_id:
        raise ValidationError("those accounts are in different households")
    if amount <= 0:
        raise ValidationError("a transfer amount must be positive")

    rate: str | None = None
    if source.currency != destination.currency:
        if to_amount is None:
            raise CurrencyMismatch(
                f"a {source.currency} to {destination.currency} transfer needs the amount that "
                "arrives; we never invent a rate",
                code="transfer.needs_amount_arriving",
                params={"from_currency": source.currency, "to_currency": destination.currency},
            )
        if to_amount <= 0:
            raise ValidationError("the amount arriving must be positive")
        rate = str(
            (Decimal(to_amount) / minor_factor(destination.currency))
            / (Decimal(amount) / minor_factor(source.currency))
        )
    else:
        if to_amount is not None and to_amount != amount:
            raise ValidationError("both sides of a same-currency transfer must match")
        to_amount = amount

    state = ClearedState(cleared)
    out_leg = Transaction(
        household_id=source.household_id,
        account_id=source.id,
        date=date,
        amount=-amount,
        payee_id=payee_service.transfer_payee(session, source.household_id, destination).id,
        memo=memo,
        cleared=state,
        transfer_account_id=destination.id,
        transfer_fx_rate=rate,
        link_source=LinkSource.person,
    )
    in_leg = Transaction(
        household_id=destination.household_id,
        account_id=destination.id,
        date=date,
        amount=to_amount,
        payee_id=payee_service.transfer_payee(session, destination.household_id, source).id,
        memo=memo,
        cleared=state,
        transfer_account_id=source.id,
        transfer_fx_rate=rate,
        link_source=LinkSource.person,
    )
    session.add_all([out_leg, in_leg])
    # Both rows must exist before either can point at the other. That is the
    # whole reason the previous build left this column unconstrained; done in
    # two steps it is an ordinary foreign key.
    session.flush()
    out_leg.transfer_transaction_id = in_leg.id
    in_leg.transfer_transaction_id = out_leg.id
    session.flush()
    return out_leg, in_leg


def duplicate(session: Session, txn: Transaction, *, date: Date | None = None) -> Transaction:
    """Copy a row as the basis for a new one.

    The cheapest stand-in for scheduled transactions, which iteration 1 does not
    have. A transfer is not duplicated -- copying one leg would invent money.
    """
    if txn.transfer_transaction_id:
        raise ValidationError(
            "make a new transfer from the register, not a copy of one leg of this one"
        )

    copy = Transaction(
        household_id=txn.household_id,
        account_id=txn.account_id,
        date=date or txn.date,
        amount=txn.amount,
        payee_id=txn.payee_id,
        memo=txn.memo,
        cleared=ClearedState.uncleared,
        # A duplicated work expense is a work expense -- that is the point of
        # duplicating a monthly one. The payment is not copied: it paid the
        # original, not this.
        reimbursement=txn.reimbursement,
    )
    session.add(copy)
    session.flush()
    return copy


def running_balance(rows: list[Transaction]) -> list[int]:
    """A running total down an ordered list.

    Only meaningful for one account in date order, which is the caller's job to
    guarantee -- across accounts or under another sort it is a column of noise.
    """
    total = 0
    out = []
    for txn in rows:
        total += txn.amount
        out.append(total)
    return out


#: How many pieces one transaction may become. Two because one is not a split;
#: five because past that you are not classifying a purchase, you are keeping a
#: second ledger inside a row, and the answer to that is separate transactions.
MIN_PARTS = 2
MAX_PARTS = 5


@dataclass(slots=True)
class SplitPart:
    """One piece of a transaction being divided."""

    amount: int
    category: Category | None = None
    memo: str | None = None


def split(
    session: Session,
    txn: Transaction,
    parts: list[SplitPart],
    *,
    split_id: str | None = None,
) -> list[Transaction]:
    """Divide one transaction into several, in one act.

    **The original is replaced, not kept as a parent.** The previous build hung
    children off a parent row that was itself a transaction, which meant every
    balance, every register query and every future report had to remember to
    exclude it or count the money twice -- and the ones that forgot were bugs
    nobody saw, because the totals still looked plausible. Here the parts *are*
    the transactions: no sum in the system needs to know a split happened.

    What the row used to be is not lost. The whole thing is one batch, so the
    audit log holds the original's before-image, the History screen shows it as
    one act, and undo puts the original back and removes the parts together.

    The parts must add up to what they replace. A split that does not is not a
    split -- it is an edit and a new transaction wearing one name, and it would
    move the account's balance while looking like a reclassification.
    """
    _assert_editable(txn)

    if txn.transfer_account_id is not None:
        raise ValidationError(
            "this is one leg of a transfer, which is a single movement of money "
            "recorded twice. Split the transfer's other side too, or undo it first."
        )
    # Splitting a payment deletes it, and deleting it releases every expense it
    # repaid back to outstanding -- logged, but not what anybody splitting a
    # payment meant. Which part repaid which expense is a person's call.
    if is_payment(session, txn):
        raise ValidationError(SPLIT_A_PAYMENT)
    if not MIN_PARTS <= len(parts) <= MAX_PARTS:
        raise ValidationError(f"a split is between {MIN_PARTS} and {MAX_PARTS} parts")
    if any(part.amount == 0 for part in parts):
        raise ValidationError("a part of a split cannot be zero")

    # Every part carries the flag, so a part of a work expense that is money
    # coming in would be a work expense that is not money out.
    if txn.reimbursement is not None and any(part.amount > 0 for part in parts):
        raise ValidationError(
            "every part of a work expense has to be money going out, because each "
            "part stays a work expense"
        )

    total = sum(part.amount for part in parts)
    if total != txn.amount:
        account = session.get(Account, txn.account_id)
        raise Conflict(
            f"the parts come to {total} and the transaction is {txn.amount}. "
            "A split has to add up, or it moves the balance.",
            code="split.does_not_add_up",
            params={
                "total": total,
                "amount": txn.amount,
                "currency": account.currency if account else None,
            },
        )
    for part in parts:
        if part.category is not None and part.category.household_id != txn.household_id:
            raise ValidationError("that category belongs to a different household")

    group = split_id or new_id()
    # Read off before the original goes, so the parts carry what it carried.
    account = session.get(Account, txn.account_id)
    shared = {
        "date": txn.date,
        "payee_id": txn.payee_id,
        "cleared": txn.cleared,
        "import_id": None,
        "import_payee_original": txn.import_payee_original,
        "import_source": txn.import_source,
        # Splitting a work expense does not stop it being one, and splitting a
        # repaid one does not unrepay it. Without these the parts would be
        # ordinary rows and the outstanding figure would fall by the whole
        # amount with nothing to say why. This is also how a partial
        # reimbursement is recorded: split, then write off or relink a part.
        "reimbursement": txn.reimbursement,
        "reimbursed_by_id": txn.reimbursed_by_id,
    }
    original_memo = txn.memo

    # Read before the original goes: `delete` below detaches whatever is still
    # attached, and by then there is nothing left to copy.
    evidence = list(txn.receipts)

    made: list[Transaction] = []
    for part in parts:
        row = Transaction(
            household_id=txn.household_id,
            account_id=txn.account_id,
            amount=part.amount,
            category_id=part.category.id if part.category else None,
            # The original's memo carries to any part that was not given one,
            # so splitting does not quietly discard what the row said.
            memo=part.memo if part.memo is not None else original_memo,
            split_id=group,
            **shared,
        )
        session.add(row)
        made.append(row)
    session.flush()

    _carry_receipts(session, evidence, made)

    # Last, so the parts exist before the thing they replace is gone: the
    # before-image is recorded either way, but the register is never briefly
    # missing the money.
    delete(session, txn)
    session.flush()

    if account is not None:
        session.refresh(account)
    return made


def _carry_receipts(
    session: Session, evidence: list, made: list[Transaction]
) -> None:
    """Put the receipt on every part, rather than dropping it to the inbox.

    A split replaces the row it divides, so without this the receipt's
    `transaction_id` goes null and the evidence lands in a pile of unmatched
    images. Honest, and bad: splitting a supermarket receipt into groceries,
    household and wine would create inbox work every single time, which is how
    people stop splitting -- or stop attaching.

    Three things make this cheap rather than clever:

    - **The bytes are not copied.** The store is content-addressed, so five
      receipt rows referencing one hash is five rows of about two hundred bytes
      and one image.
    - **The unique constraint permits it, and is the reason it is safe.**
      Uniqueness is scoped to `(transaction_id, content_sha256)` and the parts
      are different transactions. Under a household-wide rule this would have
      been *impossible*, which is the strongest argument for the scope that was
      chosen and one neither spec noticed at the time.
    - **It is one batch, so it is one undo.** The split and the copies are the
      same act; undoing removes the copies and restores the original's link,
      with no special case in `audit/undo.py`.

    The honest cost, stated because somebody will notice: detaching the receipt
    from one part does not detach it from the others. That is correct -- they
    are independent attachments now -- and the panel says "also on N other
    transactions" rather than pretending otherwise.
    """
    if not evidence or not made:
        return
    from . import receipts as receipt_service

    for receipt in evidence:
        for part in made:
            receipt_service.copy_onto(session, receipt, part.id)
        # The original goes, exactly as the transaction it was attached to
        # goes. A split *replaces* the row it divides, and replacing its
        # evidence with one row per part is the same act.
        #
        # Deliberately not "move the original onto part one and copy it onto
        # the rest". That reads cheaper and is a trap: undo would then replay
        # a *column* change on a row whose loaded `transaction` relationship
        # still names the part being deleted in the same flush, and the unit of
        # work believes the relationship. The receipt came back attached to
        # nothing, and the undo reported success. Inserts and a delete have no
        # such ambiguity -- there is no half-updated object for the flush to
        # disagree with.
        session.delete(receipt)
    session.flush()


def parts_of(session: Session, split_id: str) -> list[Transaction]:
    """Every piece of one split, oldest first."""
    return list(
        session.execute(
            select(Transaction)
            .where(Transaction.split_id == split_id)
            .order_by(Transaction.created_at, Transaction.id)
        ).scalars()
    )


# --------------------------------------------------------------------------- #
# One filter language
# --------------------------------------------------------------------------- #


def account_currencies(session: Session, household_id: str) -> dict[str, str]:
    """``account id -> its currency``, for a whole register page in one query.

    The same shape as the payee and category lookups the register already does:
    fetch the small table whole and resolve against it in Python, rather than
    joining a row's currency onto twenty thousand rows.

    Every account, including the closed ones. A closed account still has
    transactions in the register, and they are still in the currency it was
    held in -- which is exactly what the client could not know, because the
    accounts list it draws from leaves closed accounts out.
    """
    rows = session.execute(
        select(Account.id, Account.currency).where(Account.household_id == household_id)
    ).all()
    return {row[0]: row[1] for row in rows}


#: One span of minor units, and the accounts it applies to: the amount lookup
#: resolved against a household's currencies. See `amount_lookup`.
AmountSpan = tuple[list[str], int, int]


def amount_lookup(currencies: dict[str, str], typed: str) -> list[AmountSpan]:
    """What a typed amount means in each of the household's currencies (#123).

    ``currencies`` is ``account_currencies`` -- account id to currency. The
    register's amount box searches every money column, Out and In in every
    currency, so the one figure is read once per currency: 45.20 is 4520
    minor units of euro and no amount of yen at all. The spans come back
    grouped by account so `filtered` can say "this account, this span" without
    joining a currency onto every row.

    An empty list means the text is not an amount anywhere, and `filtered`
    answers that with no rows rather than with every row.
    """
    by_currency: dict[str, list[str]] = {}
    for account, currency in currencies.items():
        by_currency.setdefault(currency, []).append(account)
    spans: list[AmountSpan] = []
    for currency, accounts in sorted(by_currency.items()):
        span = magnitude_span(typed, currency)
        if span is not None:
            spans.append((sorted(accounts), span[0], span[1]))
    return spans


def _source_is(source: RegisterSource):
    """The Source column's letter, as a condition. Same precedence as the letter."""
    transfer = Transaction.transfer_account_id.is_not(None)
    split = Transaction.split_id.is_not(None)
    imported = Transaction.import_id.is_not(None)
    if source is RegisterSource.transfer:
        return transfer
    if source is RegisterSource.split:
        return and_(~transfer, split)
    if source is RegisterSource.imported:
        return and_(~transfer, ~split, imported)
    return and_(~transfer, ~split, ~imported)


def _reimbursement_is(household_id: str, view: ReimbursementView):
    """The register's Work expenses filter, as a condition.

    `work` and `paid` include the payments, not only the flagged rows: the
    register shows each payment with the expenses it repaid, and a filter that
    left the money coming in out would show a claim with no answer to it.
    """
    expense = aliased(Transaction)
    expected = Transaction.reimbursement == ReimbursementState.expected

    def paid_for(*conditions):
        return Transaction.id.in_(
            select(expense.reimbursed_by_id).where(
                expense.household_id == household_id,
                expense.reimbursed_by_id.is_not(None),
                *conditions,
            )
        )

    if view is ReimbursementView.work:
        return or_(Transaction.reimbursement.is_not(None), paid_for())
    if view is ReimbursementView.owed:
        return and_(expected, Transaction.reimbursed_by_id.is_(None))
    if view is ReimbursementView.paid:
        return or_(
            and_(expected, Transaction.reimbursed_by_id.is_not(None)),
            paid_for(expense.reimbursement == ReimbursementState.expected),
        )
    return Transaction.reimbursement == ReimbursementState.written_off


def filtered(
    household_id: str,
    *,
    account_id: str | None = None,
    account_ids: list[str] | None = None,
    since: Date | None = None,
    until: Date | None = None,
    search: str | None = None,
    cleared: ClearedState | None = None,
    uncategorised: bool = False,
    amount: list[AmountSpan] | None = None,
    source: RegisterSource | None = None,
    reimbursement: ReimbursementView | None = None,
    category_id: str | None = None,
    payee_id: str | None = None,
    category_ids: list[str] | None = None,
    categorised: bool = False,
):
    """A ``select(Transaction)`` narrowed by the household's filter vocabulary.

    Extracted from ``routers/transactions.register`` so that the register and
    the agent aggregates narrow rows with **the same code**, not merely the
    same intentions. An agent that learned this vocabulary for one endpoint
    knows it for all of them, and a change to what ``search`` means cannot
    reach one and miss the other.

    ``cleared`` is new and available to both. Everything else behaves exactly
    as it did inline, which is what lets the register's existing tests stand
    unchanged as the proof of that.

    ``account_ids`` is the plural of ``account_id`` and not a replacement for
    it: the register's account filter is a grouped picker now, so "the three
    Spanish accounts" is one filter rather than three requests, while every
    caller that narrows to exactly one account keeps saying so in the singular.
    An empty list is *not* "no accounts": there is no way to spell that in a
    repeated query parameter, and a filter that quietly means "everything"
    would be a register showing every row while its picker says none are
    ticked. So an empty list narrows nothing here, and the client never sends
    one -- it says so on screen instead. See ``Register.tsx``.

    ``category_id`` and ``payee_id`` narrow to rows carrying exactly that id
    (#134). The agent's register read asks them -- "every row for this payee"
    is how a mis-categorisation is checked -- and they live here rather than
    there so the register can take them up without a second meaning.

    ``category_ids`` and ``categorised`` are the register's category picker
    (#188), and together with ``uncategorised`` they are **one** filter whose
    parts add up rather than narrow: "Needs a category" plus Groceries is the
    rows that need a category *or* are in Groceries, the way ticking a second
    account brings its rows in too. ``categorised`` is every row that has a
    category, which is "every category ticked, Needs a category not" said
    without listing every id in a URL. The singular ``category_id`` stays an
    *and*, because it is the agent's "this category, within whatever else I
    asked" and changing that would be changing the agent's answers.
    """
    stmt = select(Transaction).where(Transaction.household_id == household_id)
    if account_id:
        stmt = stmt.where(Transaction.account_id == account_id)
    if account_ids:
        stmt = stmt.where(Transaction.account_id.in_(account_ids))
    if since:
        stmt = stmt.where(Transaction.date >= since)
    if until:
        stmt = stmt.where(Transaction.date <= until)
    if cleared is not None:
        stmt = stmt.where(Transaction.cleared == cleared)
    slices = []
    if uncategorised:
        # A boolean rather than ``category_id="__none"``: "no category" is not
        # a category id, and overloading one parameter with a sentinel is how
        # ``__none`` leaks into an API and then into every client that has to
        # spell it.
        #
        # Transfers are excluded because they have no category *by design* --
        # see ``transfer()`` -- so without this they would sit in the backlog
        # permanently and it would never reach zero. The backlog is a worklist,
        # and a worklist with rows nobody can action is not one.
        #
        # Collected rather than applied, so the picker's other ticks can join
        # it as alternatives -- see the docstring.
        slices.append(
            and_(
                Transaction.category_id.is_(None),
                Transaction.transfer_account_id.is_(None),
            )
        )
    if category_ids:
        slices.append(Transaction.category_id.in_(category_ids))
    if categorised:
        slices.append(Transaction.category_id.is_not(None))
    if slices:
        stmt = stmt.where(or_(*slices))
    if category_id:
        stmt = stmt.where(Transaction.category_id == category_id)
    if payee_id:
        stmt = stmt.where(Transaction.payee_id == payee_id)
    if source is not None:
        stmt = stmt.where(_source_is(source))
    if reimbursement is not None:
        stmt = stmt.where(_reimbursement_is(household_id, reimbursement))
    if amount is not None:
        # Out and In are two views of one signed column, so "in any money
        # column" is the magnitude: the span, and the same span negated.
        stmt = stmt.where(
            or_(
                *(
                    and_(
                        Transaction.account_id.in_(accounts),
                        Transaction.amount.between(low, high)
                        | Transaction.amount.between(-high, -low),
                    )
                    for accounts, low, high in amount
                )
            )
            if amount
            else false()
        )
    if search:
        # `autoescape`: what was typed is text, not a pattern. Without it "%"
        # and "_" matched every row, and "100%" could not find "100% cotton"
        # as itself (issue #239).
        term = search.strip()
        payee_ids = select(Payee.id).where(
            Payee.household_id == household_id, Payee.name.icontains(term, autoescape=True)
        )
        stmt = stmt.where(
            Transaction.memo.icontains(term, autoescape=True)
            | Transaction.import_payee_original.icontains(term, autoescape=True)
            | Transaction.payee_id.in_(payee_ids)
        )
    return stmt
