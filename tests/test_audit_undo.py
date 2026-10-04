"""Undo: can a batch actually be put back?

This is the feature the whole audit design exists for -- "undo that import" is
the question a monthly mass import needs answered. So these assert balances and
field values, never that a call returned.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import Conflict
from app.models import (
    Account,
    AccountType,
    Batch,
    BatchKind,
    BatchStatus,
    ClearedState,
    Transaction,
)

JAN = date(2026, 1, 15)


def _spend(session, account, amount: int, *, memo: str = "Groceries") -> Transaction:
    txn = Transaction(
        household_id=account.household_id,
        account_id=account.id,
        date=JAN,
        amount=-abs(amount),
        memo=memo,
    )
    session.add(txn)
    return txn


def _balance(session, account) -> int:
    return session.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == account.id
        )
    ).scalar_one()


def test_undoing_an_import_returns_the_balance_exactly(session, household, owner, accounts):
    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        _spend(session, checking, 5_000, memo="opening spend")
    before = _balance(session, checking)

    with batch(
        session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id
    ) as imported:
        for amount in (1_250, 4_400, 9_900):
            _spend(session, checking, amount)
    assert _balance(session, checking) == before - 15_550

    undo_batch(session, imported.id, actor_id=owner.id)
    session.commit()

    assert _balance(session, checking) == before
    assert session.get(Batch, imported.id).status is BatchStatus.undone
    assert session.get(Batch, imported.id).undone_by_id is not None


def test_undoing_an_update_restores_every_field(session, household, owner, accounts):
    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn = _spend(session, checking, 2_000, memo="original memo")
    txn_id = txn.id

    with batch(
        session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id
    ) as edit:
        txn.memo = "edited memo"
        txn.amount = -9_999
        txn.cleared = ClearedState.reconciled

    undo_batch(session, edit.id, actor_id=owner.id)
    session.commit()

    restored = session.get(Transaction, txn_id)
    assert restored.memo == "original memo"
    assert restored.amount == -2_000
    assert restored.cleared is ClearedState.uncleared


def test_undoing_a_delete_brings_the_row_back_with_its_id(session, household, owner, accounts):
    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn = _spend(session, checking, 3_300, memo="deleted later")
    txn_id = txn.id

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as removal:
        session.delete(txn)
    assert session.get(Transaction, txn_id) is None

    undo_batch(session, removal.id, actor_id=owner.id)
    session.commit()

    back = session.get(Transaction, txn_id)
    assert back is not None
    assert back.memo == "deleted later"
    assert back.amount == -3_300


def test_undoing_a_parent_and_its_children_together(session, household, owner):
    """Deferred foreign keys are what make this work across flushes."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id) as creation:
        account = Account(
            household_id=household.id, name="Joint", type=AccountType.checking, currency="EUR"
        )
        session.add(account)
        session.flush()
        for amount in (1_000, 2_000):
            _spend(session, account, amount)

    undo_batch(session, creation.id, actor_id=owner.id)
    session.commit()

    assert session.execute(
        select(func.count()).select_from(Account).where(Account.name == "Joint")
    ).scalar_one() == 0
    assert session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account.id)
    ).scalar_one() == 0


def test_undoing_an_undo_is_a_redo(session, household, owner, accounts):
    checking = accounts["checking"]
    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id) as first:
        _spend(session, checking, 7_500)
    with_spend = _balance(session, checking)

    undo = undo_batch(session, first.id, actor_id=owner.id)
    session.commit()
    assert _balance(session, checking) == 0

    undo_batch(session, undo.id, actor_id=owner.id)
    session.commit()
    assert _balance(session, checking) == with_spend


def test_a_batch_whose_rows_moved_on_refuses_to_undo(session, household, owner, accounts):
    checking = accounts["checking"]
    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id) as first:
        txn = _spend(session, checking, 4_000, memo="as imported")

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn.memo = "corrected by hand afterwards"

    with pytest.raises(Conflict, match="changed again afterwards"):
        undo_batch(session, first.id, actor_id=owner.id)


def test_editing_a_user_can_still_be_undone(session, household, owner):
    """Only the columns the log captured are restored.

    An earlier version refused to undo *any* batch touching a table that has
    redacted columns, which made ordinary administration -- renaming someone,
    issuing an invitation -- permanently irreversible. An update never needs the
    secrets: they are simply not among the columns it puts back.
    """
    with batch(session, kind=BatchKind.admin, actor_id=owner.id) as rename:
        owner.display_name = "Renamed"
    original_hash = owner.password_hash

    undo_batch(session, rename.id, actor_id=owner.id)
    session.commit()

    session.refresh(owner)
    assert owner.display_name == "Jane"
    assert owner.password_hash == original_hash, "the secret was never in the log to restore"


def test_deleting_a_row_that_holds_secrets_cannot_be_undone(session, household, owner):
    """Putting it back would need the columns the log deliberately never
    captured, and would write nulls into them."""
    from app.models import RecoveryCode

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        code = RecoveryCode(user_id=owner.id, code_hash="argon2-of-something")
        session.add(code)

    with batch(session, kind=BatchKind.admin, actor_id=owner.id) as removal:
        session.delete(code)

    with pytest.raises(Conflict, match="holds secrets"):
        undo_batch(session, removal.id, actor_id=owner.id)


def test_an_undone_batch_cannot_be_undone_twice(session, household, owner, accounts):
    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id) as first:
        _spend(session, accounts["checking"], 1_100)

    undo_batch(session, first.id, actor_id=owner.id)
    session.commit()

    with pytest.raises(Conflict, match="only an applied batch"):
        undo_batch(session, first.id, actor_id=owner.id)


def test_undoing_a_deleted_transfer_brings_both_legs_back(session, household, owner, accounts):
    """The shape that broke: a batch that updates a row and then deletes it.

    Deleting a transfer clears both legs' links and then removes both rows, so
    the replay meets the delete before the update of the same row -- and
    session.get() cannot see a row it has only just re-added.
    """
    from app.services import transactions as txn_service

    checking, card = accounts["checking"], accounts["card"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        out_leg, in_leg = txn_service.create_transfer(
            session, source=checking, destination=card, date=JAN, amount=15_000
        )
    out_id, in_id = out_leg.id, in_leg.id

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as removal:
        txn_service.delete(session, out_leg)
    assert _balance(session, checking) == 0 and _balance(session, card) == 0

    undo_batch(session, removal.id, actor_id=owner.id)
    session.commit()

    assert _balance(session, checking) == -15_000
    assert _balance(session, card) == 15_000
    restored_out = session.get(Transaction, out_id)
    restored_in = session.get(Transaction, in_id)
    assert restored_out is not None and restored_in is not None
    assert restored_out.transfer_transaction_id == in_id, "and the pair is linked again"
    assert restored_in.transfer_transaction_id == out_id


def test_undoing_the_later_batch_then_the_earlier_one_works(session, household, owner, accounts):
    """"Undo that batch first" has to be advice you can actually follow.

    The undo's own change rows become the newest for those rows, so counting
    them as blockers left the earlier batch refused forever.
    """
    from app.services import transactions as txn_service

    checking = accounts["checking"]
    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id) as first:
        txn = txn_service.create(
            session, account=checking, date=JAN, amount=-4_000, memo="as imported"
        )
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as second:
        txn_service.update(session, txn, memo="corrected by hand")

    with pytest.raises(Conflict, match="changed again afterwards"):
        undo_batch(session, first.id, actor_id=owner.id)

    undo_batch(session, second.id, actor_id=owner.id)
    session.commit()

    undo_batch(session, first.id, actor_id=owner.id)
    session.commit()
    assert _balance(session, checking) == 0


def test_undoing_a_payee_takes_its_links_through_the_log(session, owner, household, accounts):
    """`transactions.payee_id` is ON DELETE SET NULL, and SQLite obeys it itself.

    SQLAlchemy's own nullification also runs inside the flush -- both after
    ``before_flush``. So undoing the batch that created a payee used to clear it
    off every transaction that referenced it with no change row written: the
    undo reported success having destroyed the attributions, and undoing the
    undo brought the payee back but not the links.
    """
    from app.models import Change
    from app.services import payees as payee_service
    from app.services import transactions as txn_service

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as made:
        payee = payee_service.get_or_create(session, household.id, "Repsol")
    payee_id = payee.id

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-4_500, payee=payee
        )
    txn_id = txn.id
    session.commit()

    undone = undo_batch(session, made.id, actor_id=owner.id)
    session.commit()

    assert session.get(Transaction, txn_id).payee_id is None
    ops = [
        row.op.value
        for row in session.execute(
            select(Change).where(Change.table_name == "transactions", Change.row_id == txn_id)
        ).scalars()
    ]
    assert "update" in ops, "the payee link was cleared with nothing recorded in the audit log"

    # Which makes it reversible by the ordinary replay, like everything else.
    undo_batch(session, undone.id, actor_id=owner.id)
    session.commit()
    assert session.get(Transaction, txn_id).payee_id == payee_id


def test_undo_refuses_to_take_rows_a_later_batch_created(session, owner, household):
    """`_refuse_if_superseded` only looks at rows the batch itself touched.

    Dependants were invisible to it, so undoing the batch that created an
    account also deleted every transaction a later batch added to it, and
    nothing stood in the way. The deletes were logged and a redo puts them back,
    so this was destructive-without-warning rather than unrecoverable -- but
    "undo only the most recent batch that touched a row" is meant to mean the
    user is told, not that they find out afterwards.
    """
    from app.services import accounts as account_service
    from app.services import transactions as txn_service

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id) as made:
        account = account_service.create_account(
            session, household=household, name="Later", type=AccountType.checking
        )

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as spent:
        for amount in (-1_000, -2_000, -3_000):
            txn_service.create(session, account=account, date=JAN, amount=amount)
    session.commit()

    before = session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account.id)
    ).scalar_one()
    assert before == 3

    with pytest.raises(Conflict, match="later batch"):
        undo_batch(session, made.id, actor_id=owner.id)
    session.rollback()

    # Refused, not half-applied.
    assert session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account.id)
    ).scalar_one() == 3

    # And the advice works: undo that batch first, then this one.
    undo_batch(session, spent.id, actor_id=owner.id)
    session.commit()
    undo_batch(session, made.id, actor_id=owner.id)
    session.commit()
    assert session.get(Account, account.id) is None


def test_a_json_column_comes_back_as_json_and_not_as_its_repr(session, household, owner):
    """`receipts.exif` is the first JSON column on an audited table.

    `_jsonable` used to fall through to `str()` for anything it did not
    recognise, so a dict went into the change image as a Python repr --
    `"{'Make': 'Fictional'}"` -- and undo assigned that string straight back
    into the column. Nothing failed at the time: the row was restored, the
    batch said `undone`, and the column had quietly stopped being a dict until
    the next read tried to use it.
    """
    from app.models import BlobRole, Receipt, ReceiptBlob

    block = {"Make": "Fictional", "Model": "Handset 9", "ISOSpeedRatings": 400}
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        # The bytes the receipt points at: undo refuses to restore a receipt
        # whose blob is gone, which is not what this test is about.
        session.add(
            ReceiptBlob(
                sha256="d" * 64, role=BlobRole.display, data=b"x", media_type="image/jpeg",
                byte_size=1,
            )
        )
        receipt = Receipt(
            household_id=household.id,
            content_sha256="d" * 64,
            blob_sha256="d" * 64,
            media_type="image/jpeg",
            byte_size=10,
            exif=block,
        )
        session.add(receipt)
    session.commit()
    receipt_id = receipt.id

    with batch(
        session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
    ) as removing:
        session.delete(receipt)
    session.commit()

    undo_batch(session, removing.id, actor_id=owner.id)
    session.commit()

    back = session.get(Receipt, receipt_id)
    assert back is not None
    assert back.exif == block, f"came back as {type(back.exif).__name__}: {back.exif!r}"


def test_only_the_owner_can_undo_a_one_time_import_or_redo_it(session, household, owner, member):
    """#216: the import is owner-only at every door, and one undo takes a
    household's whole migrated history back out -- through a route that asked
    only for membership. The redo is the import again, so it is the owner's
    too; an ordinary statement import stays undoable by any member."""
    from app.errors import Forbidden
    from app.models import Batch, BatchStatus
    from tests.test_ledger_performance import _one_time_import_into_a_new_account

    made, _ = _one_time_import_into_a_new_account(session, owner, household, 5, tag="owned")
    rows = session.execute(
        select(func.count()).where(Transaction.household_id == household.id)
    ).scalar_one()
    assert rows == 5

    with pytest.raises(Forbidden, match="only the owner can undo a one-time import"):
        undo_batch(session, made, actor_id=member.id)
    session.rollback()
    assert session.get(Batch, made).status is BatchStatus.applied
    assert session.execute(
        select(func.count()).where(Transaction.household_id == household.id)
    ).scalar_one() == 5

    undone = undo_batch(session, made, actor_id=owner.id)
    session.commit()
    assert session.execute(
        select(func.count()).where(Transaction.household_id == household.id)
    ).scalar_one() == 0

    with pytest.raises(Forbidden):
        undo_batch(session, undone.id, actor_id=member.id)
    session.rollback()
    undo_batch(session, undone.id, actor_id=owner.id)
    session.commit()
    assert session.execute(
        select(func.count()).where(Transaction.household_id == household.id)
    ).scalar_one() == 5


def test_a_member_still_undoes_an_ordinary_edit(session, household, owner, member, accounts):
    """The owner rule is about the one-time import, not about imports or undo."""
    from app.services import transactions as txn_service

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as made:
        txn_service.create(session, account=accounts["checking"], date=JAN, amount=-700, category=None)
    session.commit()
    undo_batch(session, made.id, actor_id=member.id)
    session.commit()
    assert _balance(session, accounts["checking"]) == 0
