"""The six routes, over HTTP.

The one that matters most is the download. The previous build had **no route
that served a receipt back** for the whole life of the feature, and
`data/uploads/` was empty after 413 real transactions -- so the first test here
is that the bytes come back at all.
"""

from __future__ import annotations

import io
import pathlib

from PIL import Image

from tests.conftest import HEADERS, _setup_owner

from .receipt_fixtures import as_bytes, has_exif_block, receipt_image, with_exif


def _ledger(client) -> dict:
    """Two households, two accounts, two transactions. Two of everything."""
    _setup_owner(client)
    ours = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    theirs = client.post("/api/households", json={"name": "Theirs"}, headers=HEADERS).json()
    checking = client.post(
        f"/api/households/{ours['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    card = client.post(
        f"/api/households/{ours['id']}/accounts",
        json={"name": "Visa", "type": "credit_card", "currency": "GBP"},
        headers=HEADERS,
    ).json()

    def add(account, amount, payee, day="2026-09-19"):
        return client.post(
            f"/api/households/{ours['id']}/transactions",
            json={"account_id": account["id"], "date": day, "amount": amount, "payee_name": payee},
            headers=HEADERS,
        ).json()

    return {
        "ours": ours,
        "theirs": theirs,
        "one": add(checking, -5_705, "Mercadona"),
        "two": add(card, -1_240, "Repsol"),
        "checking": checking,
    }


def _upload(client, household_id, raw, *, name="IMG_0042.jpeg", **fields):
    return client.post(
        f"/api/households/{household_id}/receipts",
        files={"file": (name, raw, "application/octet-stream")},
        data={key: str(value) for key, value in fields.items() if value is not None},
        headers=HEADERS,
    )


# --------------------------------------------------------------------------- #
# It comes back
# --------------------------------------------------------------------------- #


def test_a_receipt_uploads_and_the_bytes_come_back(client):
    ledger = _ledger(client)
    raw = with_exif()

    made = _upload(client, ledger["ours"]["id"], raw, transaction_id=ledger["one"]["id"])
    assert made.status_code == 201, made.text
    receipt = made.json()["receipt"]
    assert made.json()["warning"] is None

    thumb = client.get(f"/api/receipts/{receipt['id']}/thumb")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/avif"
    with Image.open(io.BytesIO(thumb.content)) as image:
        assert max(image.size) == 320
    assert len(thumb.content) < len(raw)

    display = client.get(f"/api/receipts/{receipt['id']}/display")
    assert display.status_code == 200
    assert not has_exif_block(display.content), (
        "a receipt emailed to an accountant must carry nothing but the picture"
    )


def test_the_download_headers_are_the_ones_that_were_argued_for(client):
    """Test 9, redirected at the format that is actually accepted.

    The spec's version was about SVG, which is refused outright now -- but the
    headers it argued for are what stop a direct navigation to *any* receipt
    URL rendering as a document, and they are worth pinning on the route rather
    than on one format.
    """
    ledger = _ledger(client)
    made = _upload(client, ledger["ours"]["id"], with_exif(), transaction_id=ledger["one"]["id"])
    receipt = made.json()["receipt"]

    got = client.get(f"/api/receipts/{receipt['id']}/display")
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert got.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert got.headers["x-content-type-options"] == "nosniff"

    # `immutable` is safe here because the URL identifies content that cannot
    # change: a different image is a different sha256 and a different receipt.
    etag = got.headers["etag"]
    again = client.get(
        f"/api/receipts/{receipt['id']}/display", headers={"If-None-Match": etag}
    )
    assert again.status_code == 304
    assert again.content == b"", "a 304 with a body is not a 304"


def test_the_name_it_downloads_as_is_built_from_where_it_landed(client):
    ledger = _ledger(client)
    made = _upload(client, ledger["ours"]["id"], with_exif(), transaction_id=ledger["one"]["id"])
    receipt = made.json()["receipt"]

    assert receipt["download_name"].startswith("ours/2026-09-19-mercadona-")
    assert receipt["download_name"].endswith(".avif")

    # And it renames itself for free when the receipt moves.
    moved = client.patch(
        f"/api/receipts/{receipt['id']}", json={"detach": True}, headers=HEADERS
    ).json()
    assert "/inbox/" in moved["download_name"], (
        "nothing was rewritten; the name is computed from live state"
    )


# --------------------------------------------------------------------------- #
# The same file twice, over HTTP
# --------------------------------------------------------------------------- #


def test_the_same_file_on_the_same_transaction_is_a_409(client):
    """Test 1, through the route, and the count afterwards."""
    ledger = _ledger(client)
    raw = with_exif()
    first = _upload(client, ledger["ours"]["id"], raw, transaction_id=ledger["one"]["id"])
    assert first.status_code == 201

    again = _upload(client, ledger["ours"]["id"], raw, transaction_id=ledger["one"]["id"])
    assert again.status_code == 409
    assert "already attached" in again.json()["detail"]

    on_the_row = client.get(f"/api/transactions/{ledger['one']['id']}/receipts").json()
    assert len(on_the_row) == 1, "the refusal left one row, not two and not none"


def test_the_same_file_on_a_second_transaction_is_allowed_and_said_out_loud(client):
    """Test 2. A bill covering two rows is a real shape."""
    ledger = _ledger(client)
    raw = with_exif()
    _upload(client, ledger["ours"]["id"], raw, transaction_id=ledger["one"]["id"])
    second = _upload(client, ledger["ours"]["id"], raw, transaction_id=ledger["two"]["id"])

    assert second.status_code == 201
    assert "already attached to Mercadona, 2026-09-19" in second.json()["warning"]
    assert second.json()["receipt"]["also_on"] == 1

    inbox = client.get(f"/api/households/{ledger['ours']['id']}/receipts").json()
    assert len({one["content_sha256"] for one in inbox}) == 1, "one hash"
    assert len(inbox) == 2, "two attachments"


def test_a_household_cannot_read_another_households_receipt(client):
    """Test 3. 404, not 403 -- they must not learn the id is real."""
    ledger = _ledger(client)
    mine = _upload(
        client, ledger["ours"]["id"], with_exif(), transaction_id=ledger["one"]["id"]
    ).json()["receipt"]

    assert client.get(f"/api/receipts/{'f' * 32}/thumb").status_code == 404
    listed = client.get(f"/api/households/{ledger['theirs']['id']}/receipts").json()
    assert listed == [], "the other household holds nothing"
    assert client.get(f"/api/receipts/{mine['id']}/thumb").status_code == 200


# --------------------------------------------------------------------------- #
# The inbox, attaching and replacing
# --------------------------------------------------------------------------- #


def test_a_receipt_with_no_transaction_lands_in_the_inbox(client):
    ledger = _ledger(client)
    made = _upload(client, ledger["ours"]["id"], with_exif()).json()["receipt"]
    assert made["transaction_id"] is None

    unattached = client.get(
        f"/api/households/{ledger['ours']['id']}/receipts?unattached=true"
    ).json()
    assert [one["id"] for one in unattached] == [made["id"]]

    attached = client.patch(
        f"/api/receipts/{made['id']}",
        json={"transaction_id": ledger["one"]["id"]},
        headers=HEADERS,
    ).json()
    assert attached["transaction_id"] == ledger["one"]["id"]
    assert client.get(f"/api/households/{ledger['ours']['id']}/receipts?unattached=true").json() == []


def test_replace_detaches_the_old_one_rather_than_deleting_it(client):
    """Test 14.

    The obvious implementation deletes the old receipt, and the reason not to
    is in this project's own history: the worst bug the previous build had -- a
    refund on one card funding a different card -- was introduced by a fix and
    survived 180 passing tests. Destructive-by-default on a one-click button in
    a panel people open all day is that mistake waiting to happen again.
    """
    ledger = _ledger(client)
    old = _upload(
        client, ledger["ours"]["id"], with_exif(), transaction_id=ledger["one"]["id"]
    ).json()["receipt"]

    new = _upload(
        client,
        ledger["ours"]["id"],
        as_bytes(receipt_image((500, 700)), "PNG"),
        name="screenshot.png",
        transaction_id=ledger["one"]["id"],
        replaces_id=old["id"],
    )
    assert new.status_code == 201

    on_the_row = client.get(f"/api/transactions/{ledger['one']['id']}/receipts").json()
    assert [one["id"] for one in on_the_row] == [new.json()["receipt"]["id"]]

    still_there = client.get(f"/api/receipts/{old['id']}/thumb")
    assert still_there.status_code == 200, "the replaced receipt is in the inbox, not gone"
    inbox = client.get(f"/api/households/{ledger['ours']['id']}/receipts?unattached=true").json()
    assert [one["id"] for one in inbox] == [old["id"]]


def test_a_note_is_the_only_free_text_and_it_can_be_cleared(client):
    ledger = _ledger(client)
    made = _upload(client, ledger["ours"]["id"], with_exif()).json()["receipt"]

    noted = client.patch(
        f"/api/receipts/{made['id']}", json={"note": "the long one"}, headers=HEADERS
    ).json()
    assert noted["note"] == "the long one"

    cleared = client.patch(
        f"/api/receipts/{made['id']}", json={"clear_note": True}, headers=HEADERS
    ).json()
    assert cleared["note"] is None


# --------------------------------------------------------------------------- #
# Deleting, and putting it back
# --------------------------------------------------------------------------- #


def test_deleting_a_receipt_and_undoing_it_brings_back_the_picture(client):
    """Test 13. The row is back, attached, **and the bytes are still readable**."""
    ledger = _ledger(client)
    made = _upload(
        client, ledger["ours"]["id"], with_exif(), transaction_id=ledger["one"]["id"]
    ).json()["receipt"]

    gone = client.delete(f"/api/receipts/{made['id']}", headers=HEADERS)
    assert gone.status_code == 204
    assert client.get(f"/api/receipts/{made['id']}/thumb").status_code == 404

    # `include_single_edits`, because deleting one receipt is one change in one
    # manual batch -- the same shape as a keystroke in the register, and hidden
    # from the History screen for the same reason.
    house = ledger["ours"]["id"]
    batches = client.get(
        f"/api/households/{house}/batches?include_single_edits=true"
    ).json()
    removal = next(one for one in batches if "receipt" in one["detail"].lower())
    undo = client.post(
        f"/api/households/{house}/batches/{removal['id']}/undo", headers=HEADERS
    )
    assert undo.status_code in (200, 201), undo.text

    back = client.get(f"/api/transactions/{ledger['one']['id']}/receipts").json()
    assert len(back) == 1
    assert client.get(f"/api/receipts/{back[0]['id']}/thumb").status_code == 200, (
        "the grace period before the sweep is what makes an undo find its bytes"
    )


def test_deleting_a_transaction_leaves_its_receipt_in_the_inbox(client):
    """Test 15, over HTTP."""
    ledger = _ledger(client)
    made = _upload(
        client, ledger["ours"]["id"], with_exif(), transaction_id=ledger["one"]["id"]
    ).json()["receipt"]

    assert client.delete(f"/api/transactions/{ledger['one']['id']}", headers=HEADERS).status_code == 204

    inbox = client.get(f"/api/households/{ledger['ours']['id']}/receipts?unattached=true").json()
    assert [one["id"] for one in inbox] == [made["id"]]
    assert client.get(f"/api/receipts/{made['id']}/display").status_code == 200


# --------------------------------------------------------------------------- #
# The register marker
# --------------------------------------------------------------------------- #


def test_the_register_says_which_rows_have_a_receipt(client):
    """Test 12. True for exactly the receipted ids, and false for the others."""
    ledger = _ledger(client)
    _upload(client, ledger["ours"]["id"], with_exif(), transaction_id=ledger["one"]["id"])

    page = client.get(f"/api/households/{ledger['ours']['id']}/transactions").json()
    marked = {row["id"]: row["has_receipt"] for row in page["transactions"]}
    assert marked[ledger["one"]["id"]] is True
    assert marked[ledger["two"]["id"]] is False

    # And an unattached receipt marks nothing, which is the whole point of the
    # inbox being a real state rather than a broken attachment.
    _upload(client, ledger["ours"]["id"], as_bytes(receipt_image((300, 400)), "PNG"))
    page = client.get(f"/api/households/{ledger['ours']['id']}/transactions").json()
    assert sum(row["has_receipt"] for row in page["transactions"]) == 1


def test_the_running_balance_view_carries_the_marker_too(client):
    """The register has two code paths and only one of them was obvious."""
    ledger = _ledger(client)
    _upload(client, ledger["ours"]["id"], with_exif(), transaction_id=ledger["one"]["id"])

    page = client.get(
        f"/api/households/{ledger['ours']['id']}/transactions"
        f"?account_id={ledger['checking']['id']}"
    ).json()
    assert page["has_running_balance"] is True
    assert [row["has_receipt"] for row in page["transactions"]] == [True]


# --------------------------------------------------------------------------- #
# What an untrusted client may send
# --------------------------------------------------------------------------- #


def test_the_optional_client_fields_are_checked_and_never_a_500(client):
    """Test 23. Each one is a 4xx or is ignored, and the image still stores."""
    ledger = _ledger(client)
    raw = with_exif()

    bad_hash = _upload(client, ledger["ours"]["id"], raw, original_sha256="not-a-hash")
    assert bad_hash.status_code == 422, bad_hash.text

    huge = _upload(client, ledger["ours"]["id"], raw, exif="A" * 200_000)
    assert huge.status_code == 422

    rubbish = _upload(client, ledger["ours"]["id"], raw, exif="!!!not base64!!!")
    assert rubbish.status_code == 201, "a block that will not decode means no metadata, not a 500"
    assert rubbish.json()["receipt"]["camera"] == "Fictional Handset 9", (
        "and the server falls back to parsing what it actually received"
    )


def test_an_oversized_upload_is_refused(client):
    ledger = _ledger(client)
    from app.services.receipts import MAX_RECEIPT_BYTES

    too_big = b"\xff\xd8\xff" + b"\x00" * (MAX_RECEIPT_BYTES + 1)
    assert _upload(client, ledger["ours"]["id"], too_big).status_code == 413


def test_something_that_is_not_an_image_is_refused_with_a_sentence(client):
    ledger = _ledger(client)
    refused = _upload(client, ledger["ours"]["id"], b"date,amount\n2026-01-01,-1\n", name="x.jpg")
    assert refused.status_code == 422
    assert "not an image or a PDF" in refused.json()["detail"]


def test_a_plain_cross_origin_form_post_is_still_refused(client):
    """The upload route is where somebody will be tempted to drop a bare form."""
    ledger = _ledger(client)
    refused = client.post(
        f"/api/households/{ledger['ours']['id']}/receipts",
        files={"file": ("a.jpg", with_exif(), "image/jpeg")},
        headers={"Origin": "https://evil.example"},
    )
    assert refused.status_code == 403


def test_a_pdf_receipt_says_how_many_pages_it_has(client):
    ledger = _ledger(client)
    raw = pathlib.Path("tests/statement_files/card_bill.pdf").read_bytes()
    made = _upload(
        client, ledger["ours"]["id"], raw, name="bill.pdf", transaction_id=ledger["one"]["id"]
    )
    receipt = made.json()["receipt"]

    assert receipt["media_type"] == "application/pdf"
    assert receipt["page_count"] == 1
    assert receipt["has_original"] is True
    assert receipt["download_name"].endswith(".pdf"), (
        "the link offers the document, not a raster of its first page"
    )

    original = client.get(f"/api/receipts/{receipt['id']}/original")
    assert original.content == raw, "byte-identical, or pages 2 onward are gone"
    assert original.headers["content-type"] == "application/pdf"

    preview = client.get(f"/api/receipts/{receipt['id']}/display")
    assert preview.headers["content-type"] == "image/avif"


def test_the_panel_is_told_the_size_of_what_the_link_serves(client):
    """Naming a download `.avif` and sizing it as the JPEG is a quiet lie.

    `byte_size` is the file that was uploaded and belongs in *More info* beside
    its original filename. `download_bytes` is what the link hands over, and
    for a phone photo the two differ by an order of magnitude.
    """
    ledger = _ledger(client)
    photo = as_bytes(receipt_image((2400, 3200)), "JPEG", quality=92)
    receipt = _upload(
        client, ledger["ours"]["id"], photo, transaction_id=ledger["one"]["id"]
    ).json()["receipt"]

    assert receipt["byte_size"] == len(photo)
    assert receipt["download_name"].endswith(".avif")
    served = client.get(f"/api/receipts/{receipt['id']}/display").content
    assert receipt["download_bytes"] == len(served)
    assert receipt["download_bytes"] < receipt["byte_size"]


def test_a_pdf_reports_the_size_of_the_pdf(client):
    """Because for a PDF the link serves the original, not the raster."""
    ledger = _ledger(client)
    raw = pathlib.Path("tests/statement_files/card_bill.pdf").read_bytes()
    receipt = _upload(
        client, ledger["ours"]["id"], raw, name="bill.pdf", transaction_id=ledger["one"]["id"]
    ).json()["receipt"]

    assert receipt["download_bytes"] == len(raw) == receipt["byte_size"]


def test_several_receipts_can_be_attached_to_one_transaction(client):
    """Selecting a month of receipts at once is the ordinary case.

    The endpoint takes one file per request and the client issues three at a
    time -- so each has its own progress, its own error and its own retry, and
    one failure does not abandon the other twenty-nine. This is the server half
    of that: several uploads land on one row, in order, each its own receipt.
    """
    ledger = _ledger(client)
    sizes = [(400, 520), (420, 540), (440, 560), (460, 580)]
    made = [
        _upload(
            client,
            ledger["ours"]["id"],
            as_bytes(receipt_image(size), "JPEG", quality=90),
            name=f"page-{index}.jpg",
            transaction_id=ledger["one"]["id"],
        )
        for index, size in enumerate(sizes)
    ]
    assert [one.status_code for one in made] == [201] * 4

    on_the_row = client.get(f"/api/transactions/{ledger['one']['id']}/receipts").json()
    assert len(on_the_row) == 4
    # Each gets its own name, so four downloads do not collide in a folder.
    assert len({one["download_name"] for one in on_the_row}) == 4
    assert on_the_row[1]["download_name"].endswith("-2.avif")
    assert len({one["content_sha256"] for one in on_the_row}) == 4, "four different files"


def test_several_receipts_can_go_to_the_inbox_at_once(client):
    ledger = _ledger(client)
    for index in range(3):
        raw = as_bytes(receipt_image((300 + index * 20, 400)), "PNG")
        assert _upload(client, ledger["ours"]["id"], raw, name=f"{index}.png").status_code == 201

    inbox = client.get(f"/api/households/{ledger['ours']['id']}/receipts?unattached=true").json()
    assert len(inbox) == 3
    assert all(one["transaction_id"] is None for one in inbox)


def test_the_same_file_twice_in_the_inbox_is_refused(client):
    """Reported: one file uploaded twice produced two identical cards.

    The constraint cannot catch this -- uniqueness is scoped to
    `(transaction_id, content_sha256)` and two NULLs are not equal in SQL --
    so the refusal is in the service, and this is what proves it is there.
    """
    ledger = _ledger(client)
    raw = with_exif()

    first = _upload(client, ledger["ours"]["id"], raw)
    assert first.status_code == 201

    again = _upload(client, ledger["ours"]["id"], raw)
    assert again.status_code == 409
    assert "already waiting in the inbox" in again.json()["detail"]

    inbox = client.get(f"/api/households/{ledger['ours']['id']}/receipts?unattached=true").json()
    assert len(inbox) == 1, "the refusal left one card, not two"


def test_replacing_cannot_reach_a_receipt_in_another_household(client):
    """#77. `replaces_id` was loaded from any household the caller is in, and
    the batch that detached it was filed under the one in the path -- so the
    other household's receipt was moved by a batch its own members could not
    see, and this household's members could undo it."""
    ledger = _ledger(client)
    theirs_id = ledger["theirs"]["id"]
    account = client.post(
        f"/api/households/{theirs_id}/accounts",
        json={"name": "Theirs", "type": "checking"},
        headers=HEADERS,
    ).json()
    their_txn = client.post(
        f"/api/households/{theirs_id}/transactions",
        json={"account_id": account["id"], "date": "2026-09-19", "amount": -900,
              "payee_name": "Lidl"},
        headers=HEADERS,
    ).json()
    theirs = _upload(
        client, theirs_id, with_exif(), transaction_id=their_txn["id"]
    ).json()["receipt"]
    ours_before = client.get(f"/api/households/{ledger['ours']['id']}/batches").json()

    refused = _upload(
        client,
        ledger["ours"]["id"],
        as_bytes(receipt_image((500, 700)), "PNG"),
        name="screenshot.png",
        transaction_id=ledger["one"]["id"],
        replaces_id=theirs["id"],
    )

    assert refused.status_code == 404, refused.text
    still = client.get(f"/api/transactions/{their_txn['id']}/receipts").json()
    assert [one["id"] for one in still] == [theirs["id"]], "still attached where it was"
    assert client.get(f"/api/households/{ledger['ours']['id']}/batches").json() == ours_before
    assert client.get(f"/api/transactions/{ledger['one']['id']}/receipts").json() == []


def test_the_inbox_refusal_does_not_block_a_second_household(client):
    """Scoped to the household, like everything else.

    Two households legitimately holding the same PDF is the case
    content-addressing exists for, and neither can tell the other has it.
    """
    ledger = _ledger(client)
    raw = with_exif()
    assert _upload(client, ledger["ours"]["id"], raw).status_code == 201
    assert _upload(client, ledger["theirs"]["id"], raw).status_code == 201

    assert len(client.get(f"/api/households/{ledger['ours']['id']}/receipts").json()) == 1
    assert len(client.get(f"/api/households/{ledger['theirs']['id']}/receipts").json()) == 1


def test_replacing_still_works_when_the_same_file_is_already_in_the_inbox(client):
    """The reason the inbox rule is a service check and not a unique index.

    "Replace" detaches a receipt **to the inbox**. If the same bytes were
    already sitting there, a partial unique index would turn a legitimate
    one-click action into a 500 -- and detaching is not submitting a file.
    """
    ledger = _ledger(client)
    raw = with_exif()

    loose = _upload(client, ledger["ours"]["id"], raw).json()["receipt"]
    attached = _upload(
        client, ledger["ours"]["id"], raw, transaction_id=ledger["one"]["id"]
    ).json()["receipt"]

    replacement = _upload(
        client,
        ledger["ours"]["id"],
        as_bytes(receipt_image((360, 480)), "PNG"),
        name="new.png",
        transaction_id=ledger["one"]["id"],
        replaces_id=attached["id"],
    )
    assert replacement.status_code == 201, replacement.text

    inbox = client.get(f"/api/households/{ledger['ours']['id']}/receipts?unattached=true").json()
    assert {one["id"] for one in inbox} == {loose["id"], attached["id"]}, (
        "both are in the inbox, identical bytes and all -- nothing was destroyed"
    )


def test_attaching_a_receipt_shows_up_in_the_transaction_s_own_history(client):
    """"What has happened to this" showed every edit except the evidence.

    A receipt is its own audited row, so attaching one writes a change against
    the *receipt's* id -- and the row-history query is keyed on the
    transaction's. So the panel that exists to answer "why is this EUR 45
    here?" was silent about the photograph of the till slip somebody had just
    pinned to it.
    """
    world = _ledger(client)
    house, txn = world["ours"]["id"], world["one"]["id"]

    before = client.get(
        f"/api/households/{house}/changes?table=transactions&row_id={txn}",
        headers=HEADERS,
    ).json()

    made = _upload(client, house, with_exif(), transaction_id=txn)
    assert made.status_code == 201, made.text

    after = client.get(
        f"/api/households/{house}/changes?table=transactions&row_id={txn}",
        headers=HEADERS,
    ).json()

    assert len(after) > len(before), "attaching a receipt left no trace on the row"
    said = " ".join(one["summary"] for one in after)
    assert "receipt" in said.lower()

    # And detaching is in there too -- a history that shows the attaching but
    # not the removing is worse than one that shows neither.
    receipt_id = made.json()["receipt"]["id"]
    client.patch(
        f"/api/receipts/{receipt_id}", json={"detach": True}, headers=HEADERS
    )
    detached = client.get(
        f"/api/households/{house}/changes?table=transactions&row_id={txn}",
        headers=HEADERS,
    ).json()
    assert len(detached) > len(after)


# --------------------------------------------------------------------------- #
# Deleting a selection, which is one act
# --------------------------------------------------------------------------- #


def test_deleting_a_selection_of_receipts_is_one_act_and_one_undo(client):
    """The multi-select on the Receipts screen, held to the audit design.

    The point of the route is not that it is fewer requests. It is that six
    receipts chosen in one gesture come back in one undo, in the state they
    were in -- including which transaction each was attached to, which a loop
    over `DELETE /receipts/{id}` would restore in six separate acts that have
    to be undone in the right order.
    """
    ledger = _ledger(client)
    house = ledger["ours"]["id"]

    first = _upload(
        client, house, with_exif(), transaction_id=ledger["one"]["id"]
    ).json()["receipt"]
    second = _upload(
        client,
        house,
        as_bytes(receipt_image((500, 700)), "PNG"),
        transaction_id=ledger["two"]["id"],
    ).json()["receipt"]
    loose = _upload(client, house, as_bytes(receipt_image((400, 560)), "JPEG")).json()[
        "receipt"
    ]

    gone = client.post(
        f"/api/households/{house}/receipts/bulk-delete",
        json={"receipt_ids": [first["id"], second["id"], loose["id"]]},
        headers=HEADERS,
    )
    assert gone.status_code == 200, gone.text
    assert set(gone.json()) == {first["id"], second["id"], loose["id"]}
    assert client.get(f"/api/households/{house}/receipts").json() == []
    for one in (first, second, loose):
        assert client.get(f"/api/receipts/{one['id']}/thumb").status_code == 404

    # Three changes in one batch, so it is not a single edit and the History
    # screen shows it without being asked to include keystrokes.
    batches = client.get(f"/api/households/{house}/batches").json()
    removal = next(one for one in batches if "receipt" in one["detail"].lower())
    assert removal["change_count"] == 3
    assert removal["detail"] == "3 receipts removed."
    # A delete, headlined as one -- it was "Bulk edit" (#110).
    assert removal["headline"] == "Bulk delete"

    undo = client.post(
        f"/api/households/{house}/batches/{removal['id']}/undo", headers=HEADERS
    )
    assert undo.status_code in (200, 201), undo.text

    back = client.get(f"/api/households/{house}/receipts").json()
    assert len(back) == 3, "one undo, the whole selection"
    where = {one["content_sha256"]: one["transaction_id"] for one in back}
    assert where[first["content_sha256"]] == ledger["one"]["id"]
    assert where[second["content_sha256"]] == ledger["two"]["id"]
    assert where[loose["content_sha256"]] is None, (
        "the one that was in the inbox goes back to the inbox, not onto a row"
    )
    for one in back:
        assert client.get(f"/api/receipts/{one['id']}/thumb").status_code == 200


def test_a_bulk_delete_cannot_reach_past_the_household_in_the_path(client):
    """404, not 403, and the receipt is still there afterwards."""
    ledger = _ledger(client)
    mine = _upload(client, ledger["ours"]["id"], with_exif()).json()["receipt"]

    refused = client.post(
        f"/api/households/{ledger['theirs']['id']}/receipts/bulk-delete",
        json={"receipt_ids": [mine["id"]]},
        headers=HEADERS,
    )
    assert refused.status_code == 404
    assert client.get(f"/api/receipts/{mine['id']}/thumb").status_code == 200, (
        "a refusal that deleted it anyway would be the worst of both"
    )


def test_a_selection_that_has_gone_stale_deletes_what_is_still_there(client):
    """Two sessions, one selection. The half that still exists goes."""
    ledger = _ledger(client)
    house = ledger["ours"]["id"]
    here = _upload(client, house, with_exif()).json()["receipt"]

    answer = client.post(
        f"/api/households/{house}/receipts/bulk-delete",
        json={"receipt_ids": [here["id"], "f" * 32]},
        headers=HEADERS,
    )
    assert answer.status_code == 200
    assert answer.json() == [here["id"]], "it says what it removed, not what it was asked"
    assert client.get(f"/api/households/{house}/receipts").json() == []

    # And a selection where nothing at all is left is a refusal, not a
    # cheerful empty list: the caller believed it was deleting something.
    again = client.post(
        f"/api/households/{house}/receipts/bulk-delete",
        json={"receipt_ids": [here["id"]]},
        headers=HEADERS,
    )
    assert again.status_code == 404
