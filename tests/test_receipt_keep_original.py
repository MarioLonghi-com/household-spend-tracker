"""Whether the file a camera wrote is kept, and who decides. Issue #62.

The review item asked whether converting receipts to greyscale or black and
white buys considerable storage. It was measured before anything was built and
the answer is **no**: greyscale saves 2.3% across a phone photo, a flat scan
and a receipt with a colour logo, because AVIF already subsamples chroma and a
receipt's chroma planes are near-constant and near-free. A global 1-bit
threshold saves 78% and is not a photograph any more; the local threshold that
would survive real lighting measured *larger* than colour, because the speckle
it produces is exactly what a frequency-domain codec cannot compress. So no
conversion was built, and the measurement is recorded on #62 rather than the
question being re-opened with intuitions.

What *does* pay is the other half of the item: the original upload is 2.3 MB
against 33 KB for the two derivatives -- a factor of seventy -- and whether to
keep it was a process environment variable rather than something the household
whose receipts they are could see, change, or have audited.

Two households in the fixture, one with the toggle on and one off, and the
assertions are on **which blob roles exist** rather than on a 200.
"""

from __future__ import annotations

import pathlib

import pytest

from app.models import BlobRole, Household
from app.services import receipts

from .receipt_fixtures import as_bytes, receipt_image

PDF_FIXTURE = "tests/statement_files/card_bill.pdf"


@pytest.fixture()
def photo() -> bytes:
    return as_bytes(receipt_image((1200, 1600)), "JPEG", quality=90)


def _roles(prepared) -> set[BlobRole]:
    return {blob.role for blob in prepared.blobs}


# --------------------------------------------------------------------------- #
# What the flag does to the bytes
# --------------------------------------------------------------------------- #


def test_off_the_original_is_not_stored_and_on_it_is(photo):
    """The value, not the mechanism: which blobs came out."""
    without = receipts.prepare(photo, keep_original_bytes=False)
    with_it = receipts.prepare(photo, keep_original_bytes=True)

    assert _roles(without) == {BlobRole.display, BlobRole.thumb}
    assert _roles(with_it) == {BlobRole.original, BlobRole.display, BlobRole.thumb}

    kept = next(b for b in with_it.blobs if b.role is BlobRole.original)
    assert kept.data == photo  # byte for byte, which is the entire point


def test_the_original_is_the_whole_storage_question():
    """The measurement that decided this, asserted rather than asserted-about.

    At phone resolution the drawn fixture comes out at roughly **23x**. A real
    phone photograph measures ~70x, and is not in the repository for the reason
    `receipt_fixtures` gives -- a JPEG of a real receipt carries a real shop, a
    real card's last four and, before stripping, real coordinates. The drawn
    one is noise-free line art, which is the conservative direction: noise is
    exactly what the downscale throws away, so a real photo can only do better.

    Ten is asserted rather than twenty-three because the point is the order of
    magnitude. If this ever stops being a large multiple the toggle has stopped
    being worth having, and the reasoning on #62 needs redoing.
    """
    raw = as_bytes(receipt_image((3000, 4000)), "JPEG", quality=92)
    prepared = receipts.prepare(raw, keep_original_bytes=False)
    derivatives = sum(len(blob.data) for blob in prepared.blobs)
    assert len(raw) > derivatives * 10


def test_a_pdf_keeps_its_original_even_with_the_toggle_off():
    """`keep_original_always` outranks both the household and the operator.

    A PDF bill is frequently the only copy of a document whose terms are on
    pages 2 and 3, and a page-1 raster throws those away.
    """
    raw = pathlib.Path(PDF_FIXTURE).read_bytes()
    prepared = receipts.prepare(raw, keep_original_bytes=False)
    assert BlobRole.original in _roles(prepared)


# --------------------------------------------------------------------------- #
# Who decides
# --------------------------------------------------------------------------- #


def test_the_household_setting_is_what_the_resolver_reads(session, household):
    assert household.receipts_keep_original is False
    assert receipts.keep_original(household) is False

    household.receipts_keep_original = True
    assert receipts.keep_original(household) is True


def test_the_environment_is_a_floor_and_not_a_ceiling(monkeypatch, household):
    """Household OR environment, never AND.

    A self-hoster who has decided their disk is big enough should not have to
    visit every household; but their decision must not be able to *remove* an
    original a household asked to keep.
    """
    household.receipts_keep_original = False
    monkeypatch.setenv("SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL", "1")
    assert receipts.keep_original(household) is True

    monkeypatch.setenv("SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL", "0")
    assert receipts.keep_original(household) is False
    household.receipts_keep_original = True
    assert receipts.keep_original(household) is True


def test_two_households_get_two_answers_from_one_instance(session, owner, household):
    """The reason this is not an environment variable, stated as a test."""
    from app.audit.batch import batch
    from app.models import BatchKind, HouseholdMember, utcnow

    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        second = Household(name="Flat", base_currency="GBP", receipts_keep_original=True)
        session.add(second)
        session.flush()
        session.add(
            HouseholdMember(
                household_id=second.id, user_id=owner.id,
                added_by_id=owner.id, added_at=utcnow(),
            )
        )

    assert receipts.keep_original(household) is False
    assert receipts.keep_original(second) is True


def test_with_no_household_at_all_the_environment_alone_answers(monkeypatch):
    """A benchmark, a test, a script: no session, no household, still works."""
    monkeypatch.delenv("SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL", raising=False)
    assert receipts.keep_original(None) is False
    monkeypatch.setenv("SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL", "yes")
    assert receipts.keep_original(None) is True


# --------------------------------------------------------------------------- #
# Over HTTP, where the setting has to actually reach the upload
# --------------------------------------------------------------------------- #


def _ledger(client) -> dict:
    """Two households, and they are given different answers on purpose."""
    from tests.conftest import HEADERS, _setup_owner

    _setup_owner(client)
    ours = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    theirs = client.post("/api/households", json={"name": "Theirs"}, headers=HEADERS).json()
    return {"ours": ours, "theirs": theirs}


def _upload(client, household_id, raw, *, name="IMG_0042.jpeg"):
    from tests.conftest import HEADERS

    return client.post(
        f"/api/households/{household_id}/receipts",
        files={"file": (name, raw, "application/octet-stream")},
        headers=HEADERS,
    )


def _has_original(client, household_id, receipt_id) -> bool:
    """Ask for the original over the wire. 200 means it is there, 404 means not."""
    from tests.conftest import HEADERS

    answer = client.get(
        f"/api/receipts/{receipt_id}/original", headers=HEADERS
    )
    return answer.status_code == 200


def test_one_instance_two_households_two_answers_over_http(client, photo):
    """The end-to-end shape of the fix, and the reason it is not an env var.

    Same server, same process, same upload -- and the household that asked for
    its originals gets them while the one that did not is not paying for them.
    """
    from tests.conftest import HEADERS

    made = _ledger(client)
    turned_on = client.patch(
        f"/api/households/{made['theirs']['id']}",
        json={"receipts_keep_original": True},
        headers=HEADERS,
    )
    assert turned_on.status_code == 200, turned_on.text
    assert turned_on.json()["receipts_keep_original"] is True

    # Two *different* photographs, which is the realistic case and the one
    # this test is about. The same photograph in both is a different question
    # and has its own test below.
    plain = _upload(client, made["ours"]["id"], photo)
    assert plain.status_code == 201, plain.text
    kept = _upload(
        client,
        made["theirs"]["id"],
        as_bytes(receipt_image((1100, 1500)), "JPEG", quality=90),
    )
    assert kept.status_code == 201, kept.text

    assert _has_original(client, made["ours"]["id"], plain.json()["receipt"]["id"]) is False
    assert _has_original(client, made["theirs"]["id"], kept.json()["receipt"]["id"]) is True


def test_a_shared_blob_means_a_household_can_see_an_original_it_did_not_ask_for(
    client, photo
):
    """A property of the content-addressed store, pinned rather than pretended away.

    `receipt_blobs` is keyed `(sha256, role)` and is **shared between
    households** -- two households uploading the same PDF store it once, which
    is the whole design. So if household A keeps originals and household B does
    not, and both upload the *same file*, B's receipt can serve an original too:
    the blob is already there and nothing would be saved by refusing to hand it
    back.

    This is not a leak. The only bytes B can reach are bytes B uploaded itself;
    it cannot discover that A exists, or that A has the same receipt. And it is
    not a storage cost either -- the copy is one copy however many receipts
    point at it. The alternative, deleting or hiding a blob another household
    is relying on, would be worse in both directions.

    Recorded here so the next person to read `receipts_with_original` knows why
    it can be larger than the number of uploads made while the toggle was on.
    """
    from tests.conftest import HEADERS

    made = _ledger(client)
    client.patch(
        f"/api/households/{made['theirs']['id']}",
        json={"receipts_keep_original": True},
        headers=HEADERS,
    )
    kept = _upload(client, made["theirs"]["id"], photo)
    assert kept.status_code == 201, kept.text

    # Same bytes, into the household that asked not to keep originals.
    plain = _upload(client, made["ours"]["id"], photo)
    assert plain.status_code == 201, plain.text
    assert _has_original(client, made["ours"]["id"], plain.json()["receipt"]["id"]) is True


def test_the_screen_is_told_how_many_originals_turning_it_off_would_keep(client, photo):
    """The number the warning needs. Nothing is deleted by flipping the switch."""
    from tests.conftest import HEADERS

    made = _ledger(client)
    client.patch(
        f"/api/households/{made['ours']['id']}",
        json={"receipts_keep_original": True},
        headers=HEADERS,
    )
    for _ in range(2):
        # Two different images, because the same bytes twice is a duplicate.
        raw = as_bytes(receipt_image((900 + _ * 30, 1200)), "JPEG", quality=90)
        assert _upload(client, made["ours"]["id"], raw).status_code == 201

    back_off = client.patch(
        f"/api/households/{made['ours']['id']}",
        json={"receipts_keep_original": False},
        headers=HEADERS,
    )
    assert back_off.status_code == 200, back_off.text
    assert back_off.json()["receipts_keep_original"] is False
    # Still two. Turning it off decides what happens to the *next* upload.
    assert back_off.json()["receipts_with_original"] == 2


def test_the_screen_is_told_when_the_operator_has_forced_it_on(client, monkeypatch):
    from tests.conftest import HEADERS

    made = _ledger(client)
    monkeypatch.setenv("SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL", "1")
    seen = client.get(f"/api/households/{made['ours']['id']}", headers=HEADERS).json()
    assert seen["receipts_keep_original_forced"] is True
    assert seen["receipts_keep_original"] is True
