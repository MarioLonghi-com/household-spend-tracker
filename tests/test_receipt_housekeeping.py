"""The sweep, the grace period, and what a split does to its evidence.

Both halves are about the same thing: `receipt_blobs` is out of the audit log
and `receipts` is in it, and every bug in this area lives where the two
postures meet.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.models import BatchKind, BlobRole, Receipt, ReceiptBlob, Transaction, utcnow
from app.services import receipts as receipt_service
from app.services import transactions as txn_service

JAN = date(2026, 1, 15)
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture()
def write(session, owner, household):
    def _open(kind=BatchKind.manual):
        return batch(session, kind=kind, actor_id=owner.id, household_id=household.id)

    return _open


def _blob(session, sha: str, *, orphaned_at=None) -> ReceiptBlob:
    row = ReceiptBlob(
        sha256=sha,
        role=BlobRole.display,
        data=b"pretend-bytes-" + sha[:4].encode(),
        media_type="image/avif",
        byte_size=18,
        orphaned_at=orphaned_at,
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
        byte_size=18,
    )
    session.add(row)
    return row


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


def test_the_sweep_takes_the_old_orphan_and_leaves_the_fresh_one(session):
    """Test 16, and it asserts a count that changed rather than that it ran."""
    now = utcnow()
    _blob(session, SHA_A, orphaned_at=now - timedelta(hours=25))
    _blob(session, SHA_B, orphaned_at=now - timedelta(hours=1))
    session.commit()

    gone = receipt_service.sweep_orphan_blobs(session, now=now)
    session.commit()

    assert gone == 1
    left = set(session.execute(select(ReceiptBlob.sha256)).scalars())
    assert left == {SHA_B}, "the one orphaned an hour ago is still inside the window"


def test_a_blob_nothing_references_starts_its_clock_rather_than_going_at_once(session):
    now = utcnow()
    _blob(session, SHA_A)
    session.commit()

    assert receipt_service.sweep_orphan_blobs(session, now=now) == 0
    session.commit()

    marked = session.get(ReceiptBlob, (SHA_A, BlobRole.display))
    assert marked is not None and marked.orphaned_at is not None, (
        "the window runs from when it was orphaned, not from when it was made"
    )

    assert receipt_service.sweep_orphan_blobs(session, now=now + timedelta(hours=25)) == 1
    session.commit()
    assert session.get(ReceiptBlob, (SHA_A, BlobRole.display)) is None


def test_a_blob_that_is_referenced_again_stops_its_clock(session, household, write):
    """A receipt re-attached from the inbox inside the window must not be swept
    out from under, and the clock has to be *cleared* rather than merely
    ignored -- otherwise the next sweep past the window takes it anyway."""
    now = utcnow()
    _blob(session, SHA_A, orphaned_at=now - timedelta(hours=23))
    with write():
        _receipt(session, household.id, SHA_A, None)
    session.commit()

    assert receipt_service.sweep_orphan_blobs(session, now=now) == 0
    session.commit()
    assert session.get(ReceiptBlob, (SHA_A, BlobRole.display)).orphaned_at is None

    assert receipt_service.sweep_orphan_blobs(session, now=now + timedelta(days=5)) == 0
    assert session.get(ReceiptBlob, (SHA_A, BlobRole.display)) is not None


def test_two_receipts_sharing_one_hash_keep_the_bytes_between_them(
    session, household, accounts, write
):
    """A reference count, not a cascade. Deleting one must not take the other's
    picture with it -- which is exactly what a foreign-key cascade would do."""
    with write():
        one = txn_service.create(session, account=accounts["checking"], date=JAN, amount=-100)
        two = txn_service.create(session, account=accounts["card"], date=JAN, amount=-200)
        _blob(session, SHA_A)
        first = _receipt(session, household.id, SHA_A, one.id)
        _receipt(session, household.id, SHA_A, two.id)
    session.commit()

    with write():
        session.delete(first)
    session.commit()

    assert receipt_service.sweep_orphan_blobs(session, now=utcnow() + timedelta(days=2)) == 0
    assert session.get(ReceiptBlob, (SHA_A, BlobRole.display)) is not None, (
        "the second receipt still points at these bytes"
    )


def test_undoing_a_delete_inside_the_window_finds_its_bytes(
    session, household, accounts, write, owner
):
    """The one place the two tables' different audit postures can bite.

    `undo` replays change images and `receipt_blobs` has none, so the grace
    period is the only thing that makes this correct rather than a restored row
    pointing at nothing -- which is worse than not undoing, because it looks
    like it worked.
    """
    with write():
        txn = txn_service.create(session, account=accounts["checking"], date=JAN, amount=-100)
        _blob(session, SHA_A)
        receipt = _receipt(session, household.id, SHA_A, txn.id)
    session.commit()
    receipt_id = receipt.id

    with write() as removing:
        session.delete(receipt)
    session.commit()

    # A sweep runs in between, as one does every six hours.
    receipt_service.sweep_orphan_blobs(session)
    session.commit()

    undo_batch(session, removing.id, actor_id=owner.id)
    session.commit()

    back = session.get(Receipt, receipt_id)
    assert back is not None
    bytes_back = receipt_service.blob_for(session, back.content_sha256, BlobRole.display)
    assert bytes_back is not None, "the receipt came back pointing at nothing"
    assert bytes_back.data.startswith(b"pretend-bytes-")

    # Undo replays change images and knows nothing about blobs, so the clock
    # is still ticking on this one until the next sweep notices somebody is
    # pointing at it again. That next sweep is what has to clear it -- and it
    # has to clear it rather than merely skip it, or a sweep a week later takes
    # bytes that are in use.
    receipt_service.sweep_orphan_blobs(session, now=utcnow() + timedelta(days=7))
    session.commit()
    still_there = receipt_service.blob_for(session, back.content_sha256, BlobRole.display)
    assert still_there is not None, "a later sweep took bytes a live receipt points at"
    assert still_there.orphaned_at is None


def test_undoing_a_delete_after_the_bytes_were_swept_is_refused(
    session, household, accounts, write, owner
):
    """Past the grace period the bytes are gone, and undo -- which knows nothing
    about `receipt_blobs` -- used to put back a receipt pointing at nothing."""
    from app.errors import Conflict

    with write():
        txn = txn_service.create(session, account=accounts["checking"], date=JAN, amount=-100)
        _blob(session, SHA_A)
        receipt = _receipt(session, household.id, SHA_A, txn.id)
        # A second receipt on other bytes that are still there, so the refusal
        # has to be about the one that is gone rather than any receipt at all.
        _blob(session, SHA_B)
        other = _receipt(session, household.id, SHA_B, txn.id)
    session.commit()
    receipt_id = receipt.id

    with write() as removing:
        session.delete(receipt)
    session.commit()
    with write() as removing_other:
        session.delete(other)
    session.commit()

    # The first sweep starts the clock; one twenty-five hours on takes the bytes.
    receipt_service.sweep_orphan_blobs(session)
    assert receipt_service.sweep_orphan_blobs(session, now=utcnow() + timedelta(hours=25)) == 2
    session.commit()

    # Put SHA_B's bytes back as if re-uploaded: that receipt's undo may proceed.
    _blob(session, SHA_B)
    session.commit()
    undo_batch(session, removing_other.id, actor_id=owner.id)
    session.commit()

    with pytest.raises(Conflict, match="cleared out"):
        undo_batch(session, removing.id, actor_id=owner.id)
    session.rollback()
    assert session.get(Receipt, receipt_id) is None, "a receipt came back with no bytes"


def test_the_housekeeping_timer_actually_calls_it(engine, session, household, write):
    """A retention window with no caller is not a retention window.

    `ratelimit.prune()` documented thirty days and had no caller anywhere in
    the application for the life of the build; this is the test that says the
    same thing cannot be true here.
    """
    from app.auth import housekeeping

    _blob(session, SHA_A, orphaned_at=utcnow() - timedelta(days=3))
    session.commit()

    removed = housekeeping.sweep(engine)

    assert removed["receipt_blobs"] == 1
    assert session.get(ReceiptBlob, (SHA_A, BlobRole.display)) is None


# --------------------------------------------------------------------------- #
# A split, and its evidence
# --------------------------------------------------------------------------- #


def test_splitting_copies_the_receipt_onto_every_part(session, household, accounts, write):
    """Test 15b. Three rows, one blob, and every part shows the marker.

    Without this, splitting a supermarket receipt into groceries, household and
    wine would drop the evidence into the inbox every single time -- which is
    how people stop splitting.
    """
    with write():
        txn = txn_service.create(session, account=accounts["checking"], date=JAN, amount=-9_000)
        _blob(session, SHA_A)
        _receipt(session, household.id, SHA_A, txn.id)
    session.commit()

    with write(kind=BatchKind.split):
        parts = txn_service.split(
            session,
            txn,
            [
                txn_service.SplitPart(amount=-4_000),
                txn_service.SplitPart(amount=-3_000),
                txn_service.SplitPart(amount=-2_000),
            ],
        )
    session.commit()

    rows = session.execute(select(Receipt)).scalars().all()
    assert len(rows) == 3, "one receipt row per part"
    assert {one.transaction_id for one in rows} == {one.id for one in parts}
    assert session.execute(select(func.count()).select_from(ReceiptBlob)).scalar_one() == 1, (
        "and one image between them -- the store is content-addressed"
    )

    marked = receipt_service.receipted_ids(session, household.id)
    assert marked == {one.id for one in parts}, "every part shows the paperclip"


def test_undoing_that_split_puts_the_receipt_back_on_one_row(
    session, household, accounts, write, owner
):
    """Test 15c. Back to one receipt row, and the bytes are still readable."""
    with write():
        txn = txn_service.create(session, account=accounts["checking"], date=JAN, amount=-9_000)
        _blob(session, SHA_A)
        _receipt(session, household.id, SHA_A, txn.id)
    session.commit()
    original_id = txn.id

    with write(kind=BatchKind.split) as splitting:
        txn_service.split(
            session,
            txn,
            [txn_service.SplitPart(amount=-5_000), txn_service.SplitPart(amount=-4_000)],
        )
    session.commit()

    undo_batch(session, splitting.id, actor_id=owner.id)
    session.commit()

    rows = session.execute(select(Receipt)).scalars().all()
    assert len(rows) == 1, f"the copies should be gone, found {len(rows)}"
    assert rows[0].transaction_id == original_id, "attached to the row that came back"
    assert session.get(Transaction, original_id) is not None
    assert receipt_service.blob_for(session, SHA_A, BlobRole.display) is not None
