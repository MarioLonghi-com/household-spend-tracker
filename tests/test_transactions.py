"""The register, and the mirrored transfer pair.

Every assertion here is about a value the ledger holds afterwards, not about a
call returning. The previous build had four bugs hiding behind tests that
checked a mechanism fired.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import Conflict, CurrencyMismatch, ValidationError
from app.models import BatchKind, ClearedState, Payee, Transaction
from app.services import accounts as account_service
from app.services import payees as payee_service
from app.services import transactions as txn_service

JAN = date(2026, 1, 15)
FEB = date(2026, 2, 20)


@pytest.fixture()
def write(session, owner, household):
    """Open a batch for a test's own writes, the way a route does."""

    def _open(kind=BatchKind.manual):
        return batch(session, kind=kind, actor_id=owner.id, household_id=household.id)

    return _open


def _balance(session, account) -> int:
    return account_service.balances(session, account.id)["balance"]


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #


def test_a_transaction_moves_the_balance(session, accounts, write):
    checking = accounts["checking"]
    with write():
        txn_service.create(session, account=checking, date=JAN, amount=-4_250, memo="Coffee")
    assert _balance(session, checking) == -4_250


def test_a_payee_is_found_however_it_is_spelled(session, household, write):
    with write():
        first = payee_service.get_or_create(session, household.id, "MERCADONA")
        again = payee_service.get_or_create(session, household.id, "  mercadona ")
    assert first.id == again.id, "casing and spacing must not fragment the payee list"
    assert first.name == "MERCADONA", "the first spelling is kept for display"


def test_a_reconciled_row_is_locked(session, accounts, write):
    with write():
        txn = txn_service.create(
            session,
            account=accounts["checking"],
            date=JAN,
            amount=-1_000,
            cleared=ClearedState.reconciled,
        )
    with write(), pytest.raises(Conflict, match="locked"):
        txn_service.update(session, txn, amount=-2_000)


def test_a_locked_row_can_still_be_edited_deliberately(session, accounts, write):
    with write():
        txn = txn_service.create(
            session,
            account=accounts["checking"],
            date=JAN,
            amount=-1_000,
            cleared=ClearedState.reconciled,
        )
    with write():
        txn_service.update(session, txn, memo="corrected", allow_locked=True)
    assert txn.memo == "corrected"


def test_clearing_a_memo_differs_from_leaving_it_alone(session, accounts, write):
    """One sentinel object, defined once.

    The previous build defined a second one in its router, so "leave this field
    alone" never matched and the sentinel itself was written into the column.
    """
    with write():
        txn = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-500, memo="original"
        )
    with write():
        txn_service.update(session, txn, amount=-600)
    assert txn.memo == "original", "not passing memo leaves it alone"

    with write():
        txn_service.update(session, txn, memo=None)
    assert txn.memo is None, "passing None clears it"


def test_duplicating_a_row_copies_it_uncleared(session, accounts, write):
    with write():
        original = txn_service.create(
            session,
            account=accounts["checking"],
            date=JAN,
            amount=-3_000,
            memo="Gym",
            cleared=ClearedState.cleared,
        )
    with write():
        copy = txn_service.duplicate(session, original, date=date(2026, 2, 15))

    assert copy.id != original.id
    assert (copy.amount, copy.memo) == (-3_000, "Gym")
    assert copy.date == date(2026, 2, 15)
    assert copy.cleared is ClearedState.uncleared, "a copy has not been seen by the bank"


def test_a_running_balance_accumulates_in_order(session, accounts, write):
    with write():
        for amount in (-1_000, -2_000, 500):
            txn_service.create(session, account=accounts["checking"], date=JAN, amount=amount)
    rows = list(
        session.execute(
            select(Transaction)
            .where(Transaction.account_id == accounts["checking"].id)
            .order_by(Transaction.created_at)
        ).scalars()
    )
    assert txn_service.running_balance(rows) == [-1_000, -3_000, -2_500]


# --------------------------------------------------------------------------- #
# Transfers
# --------------------------------------------------------------------------- #


def test_a_transfer_moves_money_without_creating_any(session, accounts, write):
    checking, card = accounts["checking"], accounts["card"]
    with write():
        txn_service.create(session, account=checking, date=JAN, amount=100_000)
        out_leg, in_leg = txn_service.create_transfer(
            session, source=checking, destination=card, date=JAN, amount=25_000
        )

    assert _balance(session, checking) == 75_000
    assert _balance(session, card) == 25_000
    assert out_leg.amount == -in_leg.amount
    assert out_leg.transfer_transaction_id == in_leg.id
    assert in_leg.transfer_transaction_id == out_leg.id


def test_both_legs_are_a_real_foreign_key_now(session, accounts, write):
    """The previous build left this column unconstrained, reasoning that the
    pair is written in two steps. It is -- and done in two steps, it is an
    ordinary foreign key."""
    from app.models import Transaction as T

    constrained = {
        fk.parent.name for fk in T.__table__.foreign_keys if fk.column.table.name == "transactions"
    }
    assert "transfer_transaction_id" in constrained


def test_editing_one_leg_mirrors_the_other(session, accounts, write):
    checking, card = accounts["checking"], accounts["card"]
    with write():
        out_leg, in_leg = txn_service.create_transfer(
            session, source=checking, destination=card, date=JAN, amount=10_000
        )
    with write():
        txn_service.update(session, out_leg, amount=-12_000)

    assert in_leg.amount == 12_000
    assert _balance(session, checking) + _balance(session, card) == 0


def test_deleting_one_leg_takes_the_other_with_it(session, accounts, write):
    """Half a transfer is money that appeared from nowhere on the other side."""
    checking, card = accounts["checking"], accounts["card"]
    with write():
        out_leg, _ = txn_service.create_transfer(
            session, source=checking, destination=card, date=JAN, amount=8_000
        )
    with write():
        txn_service.delete(session, out_leg)

    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 0
    assert _balance(session, checking) == 0
    assert _balance(session, card) == 0


def test_a_cross_currency_transfer_needs_the_amount_that_arrives(session, accounts, write):
    with write(), pytest.raises(CurrencyMismatch, match="never invent a rate"):
        txn_service.create_transfer(
            session,
            source=accounts["checking"],
            destination=accounts["pounds"],
            date=JAN,
            amount=10_000,
        )


def test_a_cross_currency_transfer_keeps_both_sides_exact(session, accounts, write):
    checking, pounds = accounts["checking"], accounts["pounds"]
    with write():
        out_leg, in_leg = txn_service.create_transfer(
            session, source=checking, destination=pounds, date=JAN, amount=10_000, to_amount=8_500
        )

    assert out_leg.amount == -10_000 and in_leg.amount == 8_500
    assert out_leg.transfer_fx_rate == in_leg.transfer_fx_rate == "0.85"
    assert _balance(session, checking) == -10_000
    assert _balance(session, pounds) == 8_500


def test_a_cross_currency_leg_will_not_be_re_derived(session, accounts, write):
    with write():
        out_leg, _ = txn_service.create_transfer(
            session,
            source=accounts["checking"],
            destination=accounts["pounds"],
            date=JAN,
            amount=10_000,
            to_amount=8_500,
        )
    with write(), pytest.raises(ValidationError, match="each leg"):
        txn_service.update(session, out_leg, amount=-11_000)


def test_an_account_cannot_transfer_to_itself(session, accounts, write):
    with write(), pytest.raises(ValidationError, match="cannot transfer to itself"):
        txn_service.create_transfer(
            session,
            source=accounts["checking"],
            destination=accounts["checking"],
            date=JAN,
            amount=1_000,
        )


def test_a_transfer_leg_is_not_duplicated(session, accounts, write):
    with write():
        out_leg, _ = txn_service.create_transfer(
            session,
            source=accounts["checking"],
            destination=accounts["card"],
            date=JAN,
            amount=5_000,
        )
    with write(), pytest.raises(ValidationError, match="not a copy of one leg"):
        txn_service.duplicate(session, out_leg)


def test_a_transfer_names_the_other_account_as_its_payee(session, accounts, write):
    with write():
        out_leg, in_leg = txn_service.create_transfer(
            session,
            source=accounts["checking"],
            destination=accounts["card"],
            date=JAN,
            amount=5_000,
        )
    assert session.get(Payee, out_leg.payee_id).name == "Transfer : Visa"
    assert session.get(Payee, in_leg.payee_id).name == "Transfer : Checking"


# --------------------------------------------------------------------------- #
# Undoing register work
# --------------------------------------------------------------------------- #


def test_undoing_a_transfer_removes_both_legs(session, accounts, owner, household):
    checking, card = accounts["checking"], accounts["card"]
    with batch(
        session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
    ) as made:
        txn_service.create_transfer(
            session, source=checking, destination=card, date=JAN, amount=7_000
        )
    assert _balance(session, card) == 7_000

    undo_batch(session, made.id, actor_id=owner.id)
    session.commit()

    assert _balance(session, checking) == 0
    assert _balance(session, card) == 0
    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 0


def test_undoing_a_bulk_edit_restores_every_row(session, accounts, owner, household):
    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        rows = [
            txn_service.create(session, account=checking, date=JAN, amount=-amount, memo="before")
            for amount in (1_000, 2_000, 3_000)
        ]

    with batch(
        session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id
    ) as edit:
        for row in rows:
            txn_service.update(session, row, memo="after", cleared=ClearedState.cleared)

    undo_batch(session, edit.id, actor_id=owner.id)
    session.commit()

    for row in rows:
        session.refresh(row)
        assert row.memo == "before"
        assert row.cleared is ClearedState.uncleared


def test_a_reconciled_leg_cannot_be_edited_through_its_partner(session, accounts, write):
    """The lock is on the row, not on the way in.

    Editing one leg rewrites the other, so a reconciled counterpart could have
    its amount changed by going around it.
    """
    checking, card = accounts["checking"], accounts["card"]
    with write():
        out_leg, in_leg = txn_service.create_transfer(
            session, source=checking, destination=card, date=JAN, amount=10_000
        )
    with write():
        txn_service.update(session, in_leg, cleared=ClearedState.reconciled)

    with write(), pytest.raises(Conflict, match="locked"):
        txn_service.update(session, out_leg, amount=-25_000)

    session.refresh(in_leg)
    assert in_leg.amount == 10_000, "the reconciled side is untouched"


def test_a_reconciled_leg_cannot_be_deleted_through_its_partner(session, accounts, write):
    checking, card = accounts["checking"], accounts["card"]
    with write():
        out_leg, in_leg = txn_service.create_transfer(
            session, source=checking, destination=card, date=JAN, amount=10_000
        )
    with write():
        txn_service.update(session, in_leg, cleared=ClearedState.reconciled)

    with write(), pytest.raises(Conflict, match="locked"):
        txn_service.delete(session, out_leg)

    assert _balance(session, card) == 10_000


def test_a_reconciled_row_can_be_unreconciled_and_then_corrected(session, accounts, write):
    """Opening balances are born reconciled, so this was every account's anchor.

    `update()` ran its locked-row check before looking at what was being
    changed, so the one edit that lifts the lock was itself blocked: the amount
    could not be corrected, the row could not be unreconciled, and it could not
    be deleted. A typo in an opening balance made that account's balance
    permanently wrong -- while the register told the user to "set it back to
    cleared to edit it", which is exactly what the service refused.
    """
    checking = accounts["checking"]
    with write():
        txn = txn_service.create(
            session,
            account=checking,
            date=JAN,
            amount=123_456,
            cleared=ClearedState.reconciled,
        )

    # Everything else is still refused while it is locked.
    with write():
        with pytest.raises(Conflict, match="locked"):
            txn_service.update(session, txn, amount=12_345)
        with pytest.raises(Conflict, match="locked"):
            txn_service.update(session, txn, memo="nope")

    # Unlocking is allowed, and then the correction lands.
    with write():
        txn_service.update(session, txn, cleared=ClearedState.cleared)
    assert txn.cleared is ClearedState.cleared

    with write():
        txn_service.update(session, txn, amount=12_345)
    session.commit()

    assert txn.amount == 12_345
    assert _balance(session, checking) == 12_345


def test_moving_one_leg_of_a_transfer_moves_both(session, accounts, write):
    """Mirroring the amount but not the date left money on one side only.

    For the gap between the two dates every as-of balance in the household
    showed €50 that had left one account and not arrived in the other.
    """
    checking, card = accounts["checking"], accounts["card"]
    with write():
        out_leg, in_leg = txn_service.create_transfer(
            session, source=checking, destination=card, date=JAN, amount=5_000
        )

    with write():
        txn_service.update(session, out_leg, date=FEB)
    session.commit()

    assert out_leg.date == FEB
    assert in_leg.date == FEB, "the other leg was left behind, so the pair no longer nets to zero"
    assert out_leg.amount == -in_leg.amount
