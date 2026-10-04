"""Sniffing, refusing, stripping and encoding -- still with no route in sight.

Every assertion is about a value that came out: the bytes are AVIF, the column
holds -3.6889, the served file carries no EXIF block. None of them asserts that
a function was called.
"""

from __future__ import annotations

import io
import json
import pathlib

import pytest
from PIL import Image

from app.errors import TooLarge, ValidationError
from app.models import BlobRole
from app.services import receipts

from .receipt_fixtures import (
    MADRID,
    MADRID_LAT_DEGREES,
    MADRID_LON,
    MADRID_LON_DEGREES,
    as_bytes,
    has_exif_block,
    png_header_claiming,
    receipt_image,
    with_exif,
)

PDF_FIXTURE = "tests/statement_files/card_bill.pdf"


def _blob(prepared, role: BlobRole):
    for one in prepared.blobs:
        if one.role is role:
            return one
    raise AssertionError(f"no {role} blob: {[b.role for b in prepared.blobs]}")


# --------------------------------------------------------------------------- #
# The encode
# --------------------------------------------------------------------------- #


def test_a_photo_becomes_a_smaller_avif_within_the_display_bound():
    """Test 4."""
    raw = as_bytes(receipt_image((3000, 4000)), "JPEG", quality=92)
    prepared = receipts.prepare(raw)

    display = _blob(prepared, BlobRole.display)
    assert display.media_type == "image/avif"
    with Image.open(io.BytesIO(display.data)) as image:
        assert image.format == "AVIF"
        assert max(image.size) == receipts.DISPLAY_MAX_EDGE
    assert display.byte_size < len(raw), (
        f"the whole point is fewer bytes: {display.byte_size} from {len(raw)}"
    )

    thumb = _blob(prepared, BlobRole.thumb)
    with Image.open(io.BytesIO(thumb.data)) as image:
        assert max(image.size) == receipts.THUMB_MAX_EDGE


def test_the_uploaded_bytes_are_not_kept_by_default():
    """`KEEP_ORIGINAL` is off, which is where the 4 MiB to 175 KiB comes from."""
    prepared = receipts.prepare(as_bytes(receipt_image((900, 1200))))
    assert [one.role for one in prepared.blobs] == [BlobRole.display, BlobRole.thumb]


def test_keeping_the_original_is_one_environment_variable(monkeypatch):
    monkeypatch.setenv("SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL", "1")
    raw = as_bytes(receipt_image((900, 1200)))
    prepared = receipts.prepare(raw)
    assert _blob(prepared, BlobRole.original).data == raw, (
        "byte-identical, or it is not the original"
    )


# --------------------------------------------------------------------------- #
# The metadata: into the database, out of the file
# --------------------------------------------------------------------------- #


def test_the_coordinates_are_parsed_and_every_served_byte_is_stripped():
    """Test 5, and it asserts the *absence* of the block in the bytes.

    Asserting that a strip function ran would pass forever after somebody adds
    `exif=im.info["exif"]` to `save()` to "keep the date" -- at which point
    every downloaded receipt carries the household's coordinates again and
    nothing else in this suite notices.
    """
    raw = with_exif()
    assert has_exif_block(raw), "the fixture has to carry what we are stripping"

    prepared = receipts.prepare(raw)

    assert prepared.gps_lat == pytest.approx(MADRID_LAT_DEGREES, abs=1e-4)
    assert prepared.gps_lon == pytest.approx(MADRID_LON_DEGREES, abs=1e-4)
    assert prepared.gps_accuracy_m == 4.0
    assert prepared.gps_bearing == pytest.approx(112.5)
    assert prepared.camera == "Fictional Handset 9"
    assert prepared.captured_at is not None

    for one in prepared.blobs:
        assert not has_exif_block(one.data), (
            f"the {one.role} copy still carries metadata; a receipt emailed to an "
            "accountant would carry where it was taken"
        )


def test_each_hemisphere_gets_its_own_sign():
    """Test 5b. A dropped Ref is a sign error that looks entirely plausible."""
    south = receipts.prepare(with_exif(lat_ref="S", lon_ref="E"))
    assert south.gps_lat == pytest.approx(-MADRID_LAT_DEGREES, abs=1e-4)
    assert south.gps_lon == pytest.approx(-MADRID_LON_DEGREES, abs=1e-4)

    north = receipts.prepare(with_exif(lat=MADRID, lon=MADRID_LON, lat_ref="N", lon_ref="E"))
    assert north.gps_lat > 0 and north.gps_lon > 0


def test_a_camera_that_gave_no_offset_stores_the_time_as_written():
    """Test 5c. Guessing UTC moves a receipt across midnight."""
    prepared = receipts.prepare(with_exif(offset=None, taken="2026:09:19 23:40:00"))
    assert prepared.captured_at_is_local is True
    assert prepared.captured_at is not None
    assert (prepared.captured_at.hour, prepared.captured_at.day) == (23, 19), (
        "stored as the camera wrote it, not shifted onto the twentieth"
    )

    shifted = receipts.prepare(with_exif(offset="+02:00", taken="2026:09:19 23:40:00"))
    assert shifted.captured_at_is_local is False
    assert (shifted.captured_at.hour, shifted.captured_at.day) == (21, 19)


def test_an_image_with_no_metadata_at_all_still_stores():
    """Test 5d. Screenshots, scans and anything a messaging app touched."""
    prepared = receipts.prepare(as_bytes(receipt_image((600, 800)), "PNG"))
    assert prepared.media_type == "image/png"
    assert (prepared.captured_at, prepared.gps_lat, prepared.gps_lon, prepared.camera) == (
        None,
        None,
        None,
        None,
    )
    assert prepared.exif is None
    assert _blob(prepared, BlobRole.display).byte_size > 0, "and it is still a receipt"


def test_the_makernote_and_the_embedded_thumbnail_are_dropped_not_stored():
    """Test 5e. The raw block is ~30 KiB and the same tags as JSON are ~1.7."""
    raw = with_exif(maker_note=b"VENDORBLOB" * 3000)
    prepared = receipts.prepare(raw)

    assert prepared.exif is not None
    assert "MakerNote" not in prepared.exif
    assert all(not isinstance(value, bytes) for value in prepared.exif.values())
    size = len(json.dumps(prepared.exif).encode())
    assert size < receipts.MAX_EXIF_JSON_BYTES, f"the JSON grew to {size} bytes"


def test_a_sideways_photo_is_stored_upright():
    """Test 6. The rotation goes on the pixels, so no viewer has to agree."""
    raw = with_exif(image=receipt_image((800, 600)), orientation=6)
    prepared = receipts.prepare(raw)

    display = _blob(prepared, BlobRole.display)
    assert display.height > display.width, "orientation 6 means the picture is portrait"
    with Image.open(io.BytesIO(display.data)) as image:
        assert not image.getexif().get(274), "and it carries no orientation tag to argue with"


# --------------------------------------------------------------------------- #
# The refusals
# --------------------------------------------------------------------------- #


def test_the_byte_cap_is_not_the_memory_cap():
    """Test 7.

    Pillow warns between 89 and 179 megapixels and only raises above that, so
    the dangerous case is the one it lets through with a warning nobody reads.
    Both are checked: 64 Mpx, which Pillow permits, and 144 Mpx, which it
    refuses on its own.
    """
    permitted_by_pillow = png_header_claiming(8000, 8000)
    assert len(permitted_by_pillow) < 1024, "a bomb is small on the wire; that is the point"
    with pytest.raises(TooLarge) as refused:
        receipts.prepare(permitted_by_pillow)
    assert "megapixels" in str(refused.value)

    with pytest.raises(TooLarge):
        receipts.prepare(png_header_claiming(12000, 12000))


def test_an_ordinary_photo_is_nowhere_near_the_pixel_cap():
    """The guard has to not fire on the thing it is guarding."""
    prepared = receipts.prepare(as_bytes(receipt_image((4000, 3000))))
    assert prepared.width * prepared.height < receipts.MAX_PIXELS


def test_a_file_larger_than_the_cap_is_refused_before_anything_else():
    with pytest.raises(TooLarge):
        receipts.prepare(b"\xff\xd8\xff" + b"x" * receipts.MAX_RECEIPT_BYTES)


def test_an_svg_is_refused_with_a_sentence():
    """Not sanitised -- refused.

    The spec designed a three-layer defence for SVG: sanitise on ingest,
    rasterise for display, and serve the original as an attachment under its
    own `default-src 'none'; sandbox`. All three are sound and none of them is
    worth building here, because Pillow cannot rasterise SVG at all -- the
    display copy would need cairosvg, which needs a system libcairo, which is
    exactly the `apt-get` line that HEIC was chosen *because* it avoids.

    So: a format nothing photographs a receipt in, whose entire cost is a
    script host served from this origin in an app with one-click irreversible
    actions, against a system dependency in the container. Refused, and the
    refusal says what to do instead.
    """
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)">'
    with pytest.raises(ValidationError) as refused:
        receipts.prepare(svg)
    assert "SVG is not accepted" in str(refused.value)
    assert "PNG" in str(refused.value), "a refusal with no way forward is a dead end"


def test_something_that_is_not_an_image_at_all_is_refused():
    with pytest.raises(ValidationError):
        receipts.prepare(b"account,date,amount\n123,2026-01-01,-12.00\n")


def test_an_empty_file_is_refused():
    with pytest.raises(ValidationError):
        receipts.prepare(b"")


# --------------------------------------------------------------------------- #
# The extension is never believed
# --------------------------------------------------------------------------- #


def test_a_pdf_called_png_is_treated_as_a_pdf():
    """Test 10. The sniff wins, always.

    The previous build took the extension out of the user's filename and
    concatenated it into a path, which is both a lie about the type and a
    traversal. Nothing here builds a path out of anything the uploader sent.
    """
    raw = pathlib.Path(PDF_FIXTURE).read_bytes()
    prepared = receipts.prepare(raw)

    assert prepared.media_type == "application/pdf"
    assert prepared.page_count == 1
    assert _blob(prepared, BlobRole.original).data == raw, "the PDF itself is kept verbatim"
    preview = _blob(prepared, BlobRole.display)
    assert preview.media_type == "image/avif"
    assert max(preview.width, preview.height) == receipts.PDF_PREVIEW_MAX_EDGE


def test_a_broken_pdf_is_a_sentence_and_not_a_500():
    with pytest.raises(ValidationError) as refused:
        receipts.prepare(b"%PDF-1.7\nthis is not a pdf at all")
    assert "could not be read" in str(refused.value)


@pytest.mark.parametrize("fmt,expected", [("PNG", "image/png"), ("GIF", "image/gif"),
                                          ("BMP", "image/bmp"), ("TIFF", "image/tiff"),
                                          ("WEBP", "image/webp")])
def test_every_accepted_raster_format_round_trips_to_avif(fmt, expected):
    prepared = receipts.prepare(as_bytes(receipt_image((400, 500)), fmt))
    assert prepared.media_type == expected
    assert _blob(prepared, BlobRole.display).media_type == "image/avif", (
        "one codec out, so the browser has one decoder path"
    )


# --------------------------------------------------------------------------- #
# HEIC
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not receipts.heif_available(), reason="pillow-heif is not installed")
def test_a_heic_keeps_its_metadata_through_the_read():
    """Test 11. The format most likely to carry a GPS block has to prove it."""
    import pillow_heif

    pillow_heif.register_heif_opener()
    jpeg = with_exif(image=receipt_image((600, 800)))
    with Image.open(io.BytesIO(jpeg)) as source:
        buffer = io.BytesIO()
        source.save(buffer, format="HEIF", exif=source.info.get("exif"))
    raw = buffer.getvalue()

    assert receipts.sniff(raw) is receipts.HEIC
    prepared = receipts.prepare(raw)
    assert prepared.camera == "Fictional Handset 9"
    assert prepared.captured_at is not None
    assert not has_exif_block(_blob(prepared, BlobRole.display).data)


def test_without_the_wheel_a_heic_is_a_sentence_rather_than_a_500(monkeypatch):
    """The optional import's other branch, which is the one nobody exercises."""
    monkeypatch.setattr(receipts, "_heif_ready", False)
    heic = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
    with pytest.raises(ValidationError) as refused:
        receipts.sniff(heic)
    assert "Most Compatible" in str(refused.value), (
        "and it says what to change on the phone, which is the only actionable part"
    )


# --------------------------------------------------------------------------- #
# The name, computed and never stored
# --------------------------------------------------------------------------- #


def _receipt_row(**kwargs):
    from app.models import Receipt

    fields = {
        "household_id": "h",
        "content_sha256": "c" * 64,
        "blob_sha256": "c" * 64,
        "media_type": "image/avif",
        "byte_size": 100,
    }
    fields.update(kwargs)
    return Receipt(**fields)


def test_an_attached_receipt_is_named_after_where_it_landed(session, household, accounts, owner):
    """The previous build stored an absolute path, which a container broke.

    Deriving the name inverts that defect exactly: the receipt below moves from
    the inbox onto a transaction and renames itself with nothing rewritten.
    """
    from datetime import date

    from app.audit.batch import batch
    from app.models import BatchKind
    from app.services import transactions as txn_service

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn = txn_service.create(
            session, account=accounts["checking"], date=date(2026, 9, 19), amount=-5_705
        )

    receipt = _receipt_row(household_id=household.id, transaction_id=txn.id)
    name = receipts.display_name(receipt, txn, household, payee_name="Mercadona S.A.")
    assert name == f"doe-smith/2026-09-19-mercadona-s-a-{txn.id[:8]}.avif"

    second = receipts.display_name(receipt, txn, household, payee_name="Mercadona", index=1)
    assert second.endswith("-2.avif"), "the second receipt on one row is -2, not a collision"

    loose = receipts.display_name(receipt, None, household)
    assert loose.startswith("doe-smith/inbox/"), "and in the inbox it is named for when it was taken"


def test_the_extension_comes_from_the_sniffed_type_and_nothing_else(household):
    receipt = _receipt_row(original_filename="a.png/../../../etc/cron.d/x")
    name = receipts.display_name(receipt, None, household, media_type="application/pdf")
    assert name.endswith(".pdf")
    assert ".." not in name and "cron" not in name, (
        "the uploaded name is provenance, never a path and never an extension"
    )
