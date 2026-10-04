"""The two tables, before there is any way to reach them over HTTP.

Commits 1 and 2 of this feature touch no route on purpose. The previous build's
receipts shipped the model and the upload route together, which meant the route
was the only way anything was ever exercised -- and that is how a feature with
no download route at all shipped unnoticed and stayed unnoticed for 413
transactions.

"Two of everything" here means **two households, two transactions, and two
receipts that share one sha256**, because a store that deduplicates is exactly
where a single-fixture test gives a false pass.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.audit.batch import batch
from app.audit.registry import EXPECTED_EXCLUDED, audited_models, excluded_tables
from app.models import BatchKind, BlobRole, Receipt, ReceiptBlob, Transaction
from app.services import transactions as txn_service

JAN = date(2026, 1, 15)

#: Not a real image. These tests are about the store, not about the codec.
BYTES_A = b"pretend-receipt-one"
BYTES_B = b"pretend-receipt-two"
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture()
def write(session, owner, household):
    def _open(kind=BatchKind.manual, household_id=None):
        return batch(
            session,
            kind=kind,
            actor_id=owner.id,
            household_id=household_id or household.id,
        )

    return _open


def _blob(session, sha: str, data: bytes = BYTES_A, role: BlobRole = BlobRole.display) -> ReceiptBlob:
    row = ReceiptBlob(
        sha256=sha,
        role=role,
        data=data,
        media_type="image/avif",
        byte_size=len(data),
        width=100,
        height=100,
    )
    session.add(row)
    session.flush()
    return row


def _receipt(session, household_id: str, sha: str, txn_id: str | None) -> Receipt:
    row = Receipt(
        household_id=household_id,
        transaction_id=txn_id,
        content_sha256=sha,
        blob_sha256=sha,
        media_type="image/jpeg",
        byte_size=len(BYTES_A),
        original_filename="IMG_0001.jpeg",
    )
    session.add(row)
    return row


@pytest.fixture()
def two_transactions(session, accounts, write):
    with write():
        one = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-1_200, memo="Lunch"
        )
        two = txn_service.create(
            session, account=accounts["card"], date=JAN, amount=-3_400, memo="Fuel"
        )
    return one, two


# --------------------------------------------------------------------------- #
# The three shapes of "the same file twice"
# --------------------------------------------------------------------------- #


def test_the_same_bytes_on_the_same_transaction_is_refused(
    session, household, two_transactions, write
):
    """Test 1. And the count afterwards, not just the exception."""
    one, _ = two_transactions
    with write():
        _blob(session, SHA_A)
        _receipt(session, household.id, SHA_A, one.id)

    with pytest.raises(IntegrityError), write():
        _receipt(session, household.id, SHA_A, one.id)
    session.rollback()

    assert session.execute(select(func.count()).select_from(Receipt)).scalar_one() == 1, (
        "the refusal has to leave one row, not two and not none"
    )


def test_the_same_bytes_on_two_transactions_is_two_rows_and_one_blob(
    session, household, two_transactions, write
):
    """Test 2. A card bill covering two rows is a real shape, so it is allowed."""
    one, two = two_transactions
    with write():
        _blob(session, SHA_A)
        _receipt(session, household.id, SHA_A, one.id)
        _receipt(session, household.id, SHA_A, two.id)

    assert session.execute(select(func.count()).select_from(Receipt)).scalar_one() == 2
    assert session.execute(select(func.count()).select_from(ReceiptBlob)).scalar_one() == 1, (
        "content-addressing means the bytes exist once however many rows point at them"
    )


def test_two_households_uploading_the_same_file_store_it_once(
    session, household, other_household, two_transactions, owner, member, write
):
    """Test 3. Neither household can tell the other one has it."""
    one, _ = two_transactions
    with write():
        _blob(session, SHA_A)
        mine = _receipt(session, household.id, SHA_A, one.id)
    with batch(session, kind=BatchKind.manual, actor_id=member.id, household_id=other_household.id):
        theirs = _receipt(session, other_household.id, SHA_A, None)

    assert mine.id != theirs.id
    assert session.execute(select(func.count()).select_from(ReceiptBlob)).scalar_one() == 1
    assert mine.content_sha256 == theirs.content_sha256


def test_the_database_does_not_stop_two_identical_receipts_in_the_inbox(
    session, household, write
):
    """Which is exactly why the service has to, and this is the reason.

    The unique constraint is `(transaction_id, content_sha256)` and SQL holds
    that two NULLs are not equal, so the database takes the same file into the
    inbox twice without complaint. An earlier version of this test asserted
    that as *correct* -- "the inbox is where a photo lands before anybody has
    decided anything" -- and the maintainer uploaded one file twice and got two cards.

    The reason a duplicate is allowed elsewhere is that one bill can be
    evidence for two rows. Neither of two inbox copies is evidence for
    anything, so there is no reading under which the second is meant. The
    refusal lives in `services/receipts.py::existing_attachment` and is proved
    over HTTP in `test_receipt_routes.py`; this test pins the gap it covers, so
    that deleting the service check fails something.
    """
    with write():
        _blob(session, SHA_A)
        _receipt(session, household.id, SHA_A, None)
        _receipt(session, household.id, SHA_A, None)

    inbox = session.execute(
        select(func.count()).select_from(Receipt).where(Receipt.transaction_id.is_(None))
    ).scalar_one()
    assert inbox == 2, "no constraint here -- the guard is one layer up"

    from app.services import receipts as receipt_service

    assert (
        receipt_service.existing_attachment(
            session, household_id=household.id, transaction_id=None, sha256=SHA_A
        )
        is not None
    ), "and the service sees what the schema does not"


# --------------------------------------------------------------------------- #
# The two classifications every new table owes
# --------------------------------------------------------------------------- #


def test_receipts_are_audited_and_the_bytes_are_not():
    assert "receipts" in audited_models(), "attaching a receipt is a deliberate act"
    assert "receipt_blobs" in excluded_tables()
    assert "receipt_blobs" in EXPECTED_EXCLUDED, (
        "and it is named in the registry, so the pin has something to compare against"
    )


def test_a_receipt_write_outside_a_batch_is_refused(session, household):
    from app.errors import NoOpenBatch

    _receipt(session, household.id, SHA_A, None)
    with pytest.raises(NoOpenBatch):
        session.flush()
    session.rollback()


def test_blob_writes_need_no_batch(session):
    """`receipt_blobs` is out of the log, so the hook must ignore it entirely.

    Asserted as a value: the row is there afterwards, with its bytes, and no
    change row was written for it.
    """
    from app.models import Change

    _blob(session, SHA_B, BYTES_B)
    session.commit()

    stored = session.get(ReceiptBlob, (SHA_B, BlobRole.display))
    assert stored is not None and stored.data == BYTES_B
    changes = session.execute(
        select(func.count()).select_from(Change).where(Change.table_name == "receipt_blobs")
    ).scalar_one()
    assert changes == 0, "a content-addressed row has no history worth keeping"


def test_the_store_cannot_hold_the_same_content_twice(session):
    """(sha256, role) is the primary key, so this is a property of the schema."""
    _blob(session, SHA_A, BYTES_A)
    session.commit()
    with pytest.raises(IntegrityError):
        _blob(session, SHA_A, BYTES_B)
    session.rollback()


def test_the_three_roles_are_one_content_hash_at_most_once_each(session):
    for role in (BlobRole.original, BlobRole.display, BlobRole.thumb):
        _blob(session, SHA_A, BYTES_A, role)
    session.commit()
    assert session.execute(select(func.count()).select_from(ReceiptBlob)).scalar_one() == 3


# --------------------------------------------------------------------------- #
# What happens to the link
# --------------------------------------------------------------------------- #


def test_deleting_a_transaction_drops_its_receipt_to_the_inbox(
    session, household, two_transactions, write
):
    """Test 15, at the store level.

    And it must be a *logged* update, not the database's own SET NULL: the
    audit hook turns the nullification into an ordinary change row, which is
    the only reason undoing the delete can put the link back.
    """
    from app.models import Change

    one, _ = two_transactions
    with write():
        _blob(session, SHA_A)
        receipt = _receipt(session, household.id, SHA_A, one.id)

    with write() as removing:
        txn_service.delete(session, one)

    session.refresh(receipt)
    assert receipt.transaction_id is None, "the evidence survives the row it was evidence for"
    logged = session.execute(
        select(func.count())
        .select_from(Change)
        .where(Change.table_name == "receipts", Change.batch_id == removing.id)
    ).scalar_one()
    assert logged == 1, (
        "the detach has to be in the log, or undoing the delete restores a "
        "transaction with its evidence silently missing"
    )


def test_undoing_that_delete_puts_the_receipt_back_on_the_transaction(
    session, household, two_transactions, write, owner
):
    from app.audit.undo import undo_batch

    one, _ = two_transactions
    with write():
        _blob(session, SHA_A)
        receipt = _receipt(session, household.id, SHA_A, one.id)

    with write() as removing:
        txn_service.delete(session, one)
    session.commit()

    undo_batch(session, removing.id, actor_id=owner.id)
    session.commit()

    session.refresh(receipt)
    assert receipt.transaction_id == one.id, "undo restores the attachment, not just the row"
    assert session.get(Transaction, one.id) is not None


def test_deleting_a_household_takes_its_receipts_with_it(
    session, household, two_transactions, write, owner
):
    """Through the ORM, so the hook writes a delete row for each one.

    `receipts.household_id` is RESTRICT at the database level precisely so this
    relationship is the only path.
    """
    one, _ = two_transactions
    with write():
        _blob(session, SHA_A)
        _receipt(session, household.id, SHA_A, one.id)

    with write(kind=BatchKind.admin):
        session.delete(household)
    session.commit()

    assert session.execute(select(func.count()).select_from(Receipt)).scalar_one() == 0
    assert session.execute(select(func.count()).select_from(ReceiptBlob)).scalar_one() == 1, (
        "the bytes are left for the sweep, which is the only thing that counts references"
    )
