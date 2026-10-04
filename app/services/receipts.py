"""Turning an uploaded file into a receipt: sniff it, refuse it, or compress it.

Nothing in this module touches a session or knows what HTTP is. The whole
ingest is a pure function over bytes, because it is the CPU-bound part -- 300 to
1200 ms for a phone photo -- and the route runs it in a thread *before* it opens
a batch, so a slow encode never holds SQLite's single writer lock.

Every step is a refusal point, and they are in this order for a reason:

1. the byte cap, checked against the declared size before a byte is read
   (``api/uploads.py``);
2. the magic bytes, never the extension and never the browser's Content-Type --
   the previous build stored the browser's claim as fact;
3. the *pixel* cap, which is not the same thing as the byte cap at all
   (see ``MAX_PIXELS``);
4. the SHA-256 of the original bytes, taken before any transformation, so the
   same file hashes the same whatever the encoder does that day;
5. the metadata, parsed *before* the encode, because the encode is what
   destroys it;
6. the orientation, applied to the pixels so no viewer has to agree about a
   tag;
7. the encode, which strips.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from PIL import ExifTags, Image, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import TooLarge, ValidationError
from ..models import (
    BlobRole,
    Household,
    Receipt,
    ReceiptBlob,
    Transaction,
    utcnow,
)

log = logging.getLogger("spendtracker.receipts")

#: 25 MiB. Above any phone JPEG or flatbed scan, below the point where a
#: handful of concurrent uploads is an event. The import path's 8 MiB is too
#: tight here -- phone photos are bigger than bank exports.
MAX_RECEIPT_BYTES = 25 * 1024 * 1024

#: Pillow's own default is 89,478,485, above which it *warns*, and it only
#: raises above twice that. So an image between 89 and 179 megapixels decodes
#: with a warning nobody reads. Measured here: a 12000x12000 PNG is 164 KiB on
#: the wire and grows peak RSS by roughly half a gigabyte -- an amplification of
#: three thousand times, against the import path's already-documented 4.6x, and
#: under a 25 MiB cap you can send a hundred and fifty of them.
#:
#: 50 megapixels is eight times the biggest phone sensor sold. The refusal is
#: free because `Image.open` parses the header without decoding, so the size is
#: known before `.load()` allocates anything.
MAX_PIXELS = 50_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

#: One codec for both derivatives, on purpose: one decoder path in the browser,
#: one set of constants, one thing to change if AVIF is ever the wrong answer.
#:
#: Measured on this machine at 2000px: AVIF q60 reproduces the text region of a
#: till receipt as faithfully as JPEG q85 (RMS 1.27 against 1.24) at a fifth of
#: the size, and beats WebP at every quality tried -- WebP's error is flat
#: across q75-q85, so raising it spends bytes without recovering the glyphs.
DISPLAY_MAX_EDGE = 2000
DISPLAY_QUALITY = 60
DISPLAY_SPEED = 6
#: A page-1 render is a document, not a photograph -- 1600px is enough to read
#: a bill's total and the original is kept anyway.
PDF_PREVIEW_MAX_EDGE = 1600
THUMB_MAX_EDGE = 320
THUMB_QUALITY = 55
THUMB_SPEED = 8

AVIF = "image/avif"

#: How long a blob nothing points at is kept before the sweep takes it.
#:
#: Twenty-four hours and not five minutes, because `audit/undo.py` replays
#: change images and `receipt_blobs` has no change rows: an undo re-inserts the
#: receipt row and does not know about bytes. The grace period is the only
#: thing that makes "undo that" correct rather than a row pointing at nothing,
#: which is worse than not undoing because it looks like it worked.
ORPHAN_GRACE = timedelta(hours=24)

#: Base64 of an APP1 block a client sends alongside a re-encoded image. Real
#: blocks measure 29-32 KiB; this is attacker-supplied binary going into a
#: parser, so it is capped well above the real figure and well below anything
#: worth handing to one.
MAX_EXIF_BYTES = 64 * 1024

#: Kept out of the JSON entirely. MakerNote is an undocumented vendor blob and
#: the EXIF thumbnail is a miniature of the receipt itself -- in an audited
#: table that lands in changes.before *and* changes.after on every edit. The
#: raw block is ~30 KiB; the same tags as JSON are ~1.7 KiB, and the gap is
#: almost entirely these two.
_DROPPED_TAGS = {"MakerNote", "UserComment", "ThumbnailData", "PrintImageMatching"}

#: What `receipts.exif` is allowed to grow to. A block that parses to more than
#: this is a block doing something other than describing a photograph.
MAX_EXIF_JSON_BYTES = 4 * 1024


def keep_original_everywhere() -> bool:
    """The operator's answer, from the environment. A **floor**, not the answer.

    `SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL` used to be the whole decision. It is
    now the one a self-hoster makes for the whole instance: on means on for
    every household, because somebody who has decided their disk is big enough
    should not have to visit each one. Off -- the default, and what every
    install that never set it has always done -- leaves the decision to the
    household. Issue #62.
    """
    import os

    return (os.environ.get("SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def keep_original(household=None) -> bool:
    """Whether the uploaded bytes are kept as well as the derivatives.

    Off by default, and the measurement is why: a 3024x4032 phone JPEG is
    **2.3 MB**, and the display and thumbnail AVIFs together are **33 KB** --
    a factor of seventy. Five years at a hundred receipts a month is ~700 MiB
    off and ~4.2 GiB on, and a 2000px AVIF of a till receipt is more legible
    than the crumpled paper.

    Household **or** environment, never and: see `keep_original_everywhere`.

    One consequence worth stating: with this off, **the original EXIF block
    does not survive anywhere** -- only the parsed columns and the JSON do. If a
    receipt ever needs to be byte-identical to what the camera produced, this
    has to be on *before* the upload, not after. Turning it off does not delete
    the originals already stored; it only stops new ones being kept.
    """
    if household is not None and getattr(household, "receipts_keep_original", False):
        return True
    return keep_original_everywhere()


# --------------------------------------------------------------------------- #
# What is accepted
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Kind:
    media_type: str
    #: The extension a download gets. Taken from the *sniffed* type, never from
    #: whatever the uploader called the file.
    suffix: str
    #: Whether the uploaded bytes are kept verbatim regardless of the
    #: household setting and SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL. True for
    #: PDFs, whose pages 2 and 3 a page-1 raster throws away.
    keep_original_always: bool = False


JPEG = Kind("image/jpeg", "jpg")
PNG = Kind("image/png", "png")
#: A PDF is already compressed, so there is little to win by re-encoding it --
#: and a PDF bill is frequently the only copy of a document whose terms are on
#: pages 2 and 3, which a page-1 raster throws away.
PDF = Kind("application/pdf", "pdf", keep_original_always=True)
WEBP = Kind("image/webp", "webp")
AVIF_KIND = Kind("image/avif", "avif")
GIF = Kind("image/gif", "gif")
TIFF = Kind("image/tiff", "tiff")
BMP = Kind("image/bmp", "bmp")
HEIC = Kind("image/heic", "heic")

#: iPhone HEIC, wired in lazily so an install without the wheel refuses the
#: format with a sentence rather than failing at startup -- the same shape as
#: `api/dbview.py`'s lazy datasette import.
_heif_ready: bool | None = None


def heif_available() -> bool:
    global _heif_ready
    if _heif_ready is None:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
            _heif_ready = True
        except Exception:  # pragma: no cover - only on an install without the wheel
            log.warning("pillow-heif is not installed, so HEIC uploads will be refused")
            _heif_ready = False
    return _heif_ready


def sniff(raw: bytes) -> Kind:
    """What this actually is, from its first bytes.

    Never the extension, never `file.content_type`. The previous build took the
    suffix out of the user's filename with `rsplit(".", 1)[-1]`, which keeps
    slashes, and concatenated it into a path -- so `a.png/../../x` traversed.
    Nothing here is ever used to build a path.
    """
    head = raw[:32]
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"%PDF-"):
        return PDF
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return GIF
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return TIFF
    if head.startswith(b"BM"):
        return BMP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"avif", b"avis"}:
            return AVIF_KIND
        if brand in {b"heic", b"heix", b"heim", b"heis", b"hevc", b"mif1", b"msf1"}:
            if not heif_available():
                raise ValidationError(
                    "that is an iPhone HEIC and this install cannot read it. "
                    "Settings -> Camera -> Formats -> Most Compatible makes the camera "
                    "write JPEG instead."
                )
            return HEIC
    if _looks_like_svg(raw):
        # Refused rather than sanitised. See the module note in
        # tests/test_receipt_compression.py::test_an_svg_is_refused_with_a_sentence.
        raise ValidationError(
            "SVG is not accepted as a receipt. It is a document that can carry script "
            "rather than a picture, and nothing photographs a receipt as one. "
            "A screenshot as PNG, or the PDF itself, both work."
        )
    raise ValidationError(
        "that file is not an image or a PDF this can read. "
        "JPEG, PNG, HEIC, WebP, AVIF, GIF, TIFF, BMP and PDF are accepted."
    )


def _looks_like_svg(raw: bytes) -> bool:
    head = raw[:1024].lstrip()
    if head[:5].lower() == b"<?xml" or head[:4].lower() == b"<svg":
        return b"<svg" in raw[:4096].lower()
    return False


# --------------------------------------------------------------------------- #
# What comes out
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PreparedBlob:
    role: BlobRole
    data: bytes
    media_type: str
    width: int | None = None
    height: int | None = None

    @property
    def byte_size(self) -> int:
        return len(self.data)


@dataclass(frozen=True)
class Prepared:
    """Everything an ingest produced, with nothing stored yet.

    Built off the event loop and handed to the route, which opens one batch and
    writes it. Keeping the two apart is what stops a 1.2-second encode holding
    the writer lock while somebody else is trying to save a transaction.
    """

    #: The dedupe key: the client's claim about the original where it made
    #: one, otherwise the hash of what arrived.
    sha256: str
    #: The hash of what actually arrived, always computed here. This is what
    #: the blob store is keyed by, and no client can choose it.
    blob_sha256: str
    media_type: str
    byte_size: int
    width: int | None = None
    height: int | None = None
    captured_at: datetime | None = None
    captured_at_is_local: bool = False
    gps_lat: float | None = None
    gps_lon: float | None = None
    gps_accuracy_m: float | None = None
    gps_bearing: float | None = None
    camera: str | None = None
    exif: dict | None = None
    client_encoded: bool = False
    page_count: int | None = None
    blobs: list[PreparedBlob] = field(default_factory=list)


@dataclass(frozen=True)
class DeviceFix:
    """Where the *phone* was when it sent the photograph.

    A different claim from EXIF GPS and a weaker one. EXIF GPS means *this is
    where the receipt was photographed*; a browser fix means *this is where the
    phone was when somebody pressed send* -- the same thing at a till and a
    completely different thing when a wallet is emptied on the kitchen table on
    Sunday.

    It lands in the same four columns anyway, because the alternative is two
    notions of where a receipt was taken and no screen able to say which it is
    looking at. What keeps the columns honest is the pair of rules in
    `_with_device_fix`: a fix never overwrites what the camera wrote, and a
    coordinate that came from here is stamped `SpendTrackerLocationSource` so
    the panel can say where it came from.
    """

    lat: float
    lon: float
    #: Metres, from `coords.accuracy`, which every browser supplies. The same
    #: column as `GPSHPositioningError`, and a browser fix off cell towers is
    #: exactly the several-hundred-metre case that column exists to admit to.
    accuracy_m: float | None = None


#: Written into the `exif` JSON, **after** `_capped`, so the trimmer cannot
#: drop the one key that says what a coordinate means. A key rather than a
#: column because a typed column is a migration, and the honest home for this
#: is `receipts.gps_from_device` the next time the table is touched.
LOCATION_SOURCE_KEY = "SpendTrackerLocationSource"
LOCATION_FROM_DEVICE = "device"


def _with_device_fix(meta: dict[str, Any], fix: DeviceFix | None) -> dict[str, Any]:
    """Fill the location in from the device, but only where the camera left it empty.

    The camera's own fix wins, always: it is a statement about the photograph
    and this is a statement about the upload. So a phone with location services
    on and the toggle on stores one coordinate, not two, and the toggle earns
    its keep on exactly the photos that have no GPS of their own -- a share
    sheet, an Android browser that strips it, a scan.
    """
    if fix is None:
        return meta
    if meta.get("gps_lat") is not None and meta.get("gps_lon") is not None:
        return meta

    out = dict(meta)
    out["gps_lat"] = fix.lat
    out["gps_lon"] = fix.lon
    out["gps_accuracy_m"] = fix.accuracy_m
    # Left alone deliberately: a browser fix knows where the phone is and
    # nothing at all about which way the camera was pointing.
    out["gps_bearing"] = None
    exif = dict(out.get("exif") or {})
    exif[LOCATION_SOURCE_KEY] = LOCATION_FROM_DEVICE
    out["exif"] = exif
    return out


# --------------------------------------------------------------------------- #
# The metadata: parsed into the database, stripped from every served byte
# --------------------------------------------------------------------------- #


def _rational(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _degrees(triple: Any, ref: Any) -> float | None:
    """Degrees/minutes/seconds plus an N/S/E/W ref, as one signed float.

    Converted at ingest and stored converted, so no reader re-implements this
    and forgets the hemisphere -- which is a sign error that looks entirely
    plausible on a map until you notice you are in the Atlantic.
    """
    try:
        degrees, minutes, seconds = (_rational(part) for part in triple)
    except (TypeError, ValueError):
        return None
    if degrees is None or minutes is None or seconds is None:
        return None
    value = degrees + minutes / 60 + seconds / 3600
    if isinstance(ref, str) and ref.strip().upper() in {"S", "W"}:
        value = -value
    return value


def _jsonable(value: Any) -> Any:
    """Scalars only. Anything binary is dropped, not encoded."""
    if isinstance(value, bytes | bytearray):
        return None
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, tuple | list):
        parts = [_jsonable(one) for one in value]
        return parts if all(one is not None for one in parts) else None
    number = _rational(value)
    return number


def _sub_ifd(exif: Any, which: Any) -> dict:
    try:
        return dict(exif.get_ifd(which))
    except Exception:  # pragma: no cover - a block without that IFD
        return {}


def _parse_exif(image: Image.Image | None, raw_block: bytes | None) -> dict[str, Any]:
    """Every scalar tag into a dict, with the six that drive behaviour named.

    Three ways this goes wrong and all three are ordinary: no EXIF at all
    (screenshots, scans, PDFs, and anything a messaging app has touched -- the
    common case, not the exception); GPS present but meaningless, which is why
    the accuracy is kept beside the coordinate; and `DateTimeOriginal` with no
    `OffsetTimeOriginal`, where guessing UTC moves a receipt across midnight and
    onto the day the statement will not match.
    """
    out: dict[str, Any] = {}
    try:
        if raw_block is not None:
            exif = Image.Exif()
            exif.load(raw_block)
        elif image is not None:
            exif = image.getexif()
        else:
            return out
        if not exif:
            return out

        tags: dict[str, Any] = {}
        gps: dict[str, Any] = {}

        # Named, not indexed. An earlier version walked a list and decided the
        # third entry was GPS -- which is true until `get_ifd` raises on the
        # second and the GPS block silently becomes the second entry, at which
        # point every coordinate is read with the wrong tag names.
        for block, names, is_gps in (
            (dict(exif), ExifTags.TAGS, False),
            (_sub_ifd(exif, ExifTags.IFD.Exif), ExifTags.TAGS, False),
            (_sub_ifd(exif, ExifTags.IFD.GPSInfo), ExifTags.GPSTAGS, True),
        ):
            for key, value in block.items():
                name = names.get(key, str(key))
                if name in _DROPPED_TAGS:
                    continue
                if is_gps:
                    # The GPS block is kept raw as well, because the columns
                    # below need the rationals and the N/S/E/W refs as they
                    # were written. Only this block; `tags` is the JSON that
                    # goes in the database and nothing unserialisable reaches it.
                    gps[name] = value
                clean = _jsonable(value)
                if clean is not None:
                    tags[name] = clean

        captured, is_local = _captured(tags)
        out["captured_at"] = captured
        out["captured_at_is_local"] = is_local
        out["gps_lat"] = _degrees(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
        out["gps_lon"] = _degrees(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
        out["gps_accuracy_m"] = _rational(gps.get("GPSHPositioningError"))
        out["gps_bearing"] = _rational(gps.get("GPSImgDirection"))

        make = str(tags.get("Make") or "").strip()
        model = str(tags.get("Model") or "").strip()
        camera = " ".join(part for part in (make, model) if part)
        out["camera"] = camera[:120] or None

        out["exif"] = _capped(tags) or None
    except Exception:
        # A malformed block means *no metadata*, never a 500 -- the picture is
        # still a receipt. Logged with the traceback rather than a sentence,
        # because this guard once hid a bug of ours rather than a bad file.
        log.warning("could not read the EXIF block; storing without metadata", exc_info=True)
        return {}
    return out


def _capped(tags: dict[str, Any]) -> dict[str, Any]:
    """Drop the longest values until the JSON fits.

    A block that parses to more than a few kilobytes is doing something other
    than describing a photograph, and the alternative to a cap is an unbounded
    attacker-chosen string in an audited table.
    """
    import json

    # `default=str` is a belt: everything here has already been through
    # `_jsonable`, and a TypeError from this line would be swallowed by the
    # caller's "no metadata rather than a 500" guard -- which is how a real
    # bug hid behind eighteen real iPhone photos producing no metadata at all
    # while every synthetic test passed.
    def size(value: object) -> int:
        return len(json.dumps(value, default=str).encode())

    trimmed = dict(tags)
    while trimmed and size(trimmed) > MAX_EXIF_JSON_BYTES:
        trimmed.pop(max(trimmed, key=lambda key: size(trimmed[key])))
    return trimmed


def _captured(tags: dict[str, Any]) -> tuple[datetime | None, bool]:
    raw = tags.get("DateTimeOriginal") or tags.get("DateTime")
    if not isinstance(raw, str):
        return None, False
    try:
        taken = datetime.strptime(raw.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None, False

    offset = tags.get("OffsetTimeOriginal") or tags.get("OffsetTime")
    minutes = _offset_minutes(offset)
    if minutes is None:
        # Stored as written, and flagged as naive. The offset lives in a
        # separate tag and plenty of cameras omit it; guessing UTC would move a
        # receipt across midnight and onto a day the statement will not match.
        return taken, True
    # Held as naive UTC, like everything else with a DateTime column here.
    return taken - timedelta(minutes=minutes), False


def _offset_minutes(offset: Any) -> int | None:
    """"+02:00" -> 120. Anything else -> None, meaning "the camera did not say"."""
    if not isinstance(offset, str):
        return None
    text = offset.strip().replace(":", "")
    if len(text) != 5 or text[0] not in "+-" or not text[1:].isdigit():
        return None
    minutes = int(text[1:3]) * 60 + int(text[3:5])
    return -minutes if text[0] == "-" else minutes


# --------------------------------------------------------------------------- #
# The encode
# --------------------------------------------------------------------------- #


def _fit(image: Image.Image, max_edge: int) -> Image.Image:
    if max(image.size) <= max_edge:
        return image
    copy = image.copy()
    copy.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return copy


def _shrink_once(image: Image.Image, max_edge: int) -> Image.Image:
    """Decode at the smallest size that still fills `max_edge`, and shrink in place.

    Issue #89. The ingest used to decode at full size, then `exif_transpose`
    copied the whole image, then `_fit` copied it twice more -- once for the
    display derivative and once for the thumbnail. A 49 MP PNG held ~470 MiB
    for two seconds, and nothing bounded how many of those ran at once.

    Now the full-size pixels exist at most once and only briefly:

    * a JPEG is **drafted** -- libjpeg decodes straight to 1/2, 1/4 or 1/8
      scale in the DCT, so a 48 MP phone photo is never 48 MP in memory;
    * everything else decodes once and is shrunk **in place**, so the
      full-size buffer is released the moment the smaller one exists;
    * the rotation and both derivatives then work on the display-size copy,
      where a copy costs twelve megabytes rather than a hundred and fifty.

    The fit is to a square, so it does not matter that the orientation has not
    been applied yet: the longest edge is the longest edge either way up.
    """
    width, height = image.size
    longest = max(width, height)
    if longest <= max_edge:
        image.load()
        return image
    ratio = max_edge / longest
    # Pillow's `draft` keeps both edges at or above what is asked for, so the
    # LANCZOS pass below still has at least `max_edge` pixels to work from.
    # A no-op for anything that is not a JPEG.
    image.draft(None, (max(1, int(width * ratio)), max(1, int(height * ratio))))
    image.thumbnail((max_edge, max_edge), Image.LANCZOS)
    return image


def _to_avif(
    image: Image.Image, *, role: BlobRole, max_edge: int, quality: int, speed: int
) -> PreparedBlob:
    """Encode, which strips.

    Pillow carries no metadata into a new image, so the derivatives are clean
    by construction -- and that is exactly why it is fragile. The day somebody
    adds `exif=im.info["exif"]` here to "keep the date", every downloaded
    receipt starts carrying coordinates again and nothing fails. The test
    asserts the *absence* of an EXIF block in the bytes, not that a strip
    function ran.
    """
    fitted = _fit(image, max_edge)
    if fitted.mode not in {"RGB", "RGBA"}:
        fitted = fitted.convert("RGB")
    buffer = io.BytesIO()
    fitted.save(buffer, format="AVIF", quality=quality, speed=speed)
    return PreparedBlob(
        role=role,
        data=buffer.getvalue(),
        media_type=AVIF,
        width=fitted.width,
        height=fitted.height,
    )


def _open_checked(raw: bytes) -> Image.Image:
    """Open the header, refuse on dimensions, and only then decode."""
    try:
        image = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError as bomb:
        raise TooLarge(
            "that image is far too large to decode. A receipt is a photograph, "
            "not a poster."
        ) from bomb
    except Exception as broken:
        raise ValidationError("that file could not be read as an image") from broken

    width, height = image.size
    if width * height > MAX_PIXELS:
        raise TooLarge(
            f"that image is {width}x{height}, which is {width * height // 1_000_000} "
            "megapixels. The limit is 50, which is eight times the biggest phone "
            "sensor sold -- a file this size is a decompression bomb however it got here."
        )
    return image


#: pdfium is not thread-safe -- not even across two different documents -- and
#: renders now run on a pool two workers wide (`api/offload.py`), next to the
#: agent's plain-`def` routes on FastAPI's own threadpool. One render at a time
#: in the process; a page-1 render at preview size is tens of milliseconds.
_PDFIUM = threading.Lock()


def _render_pdf(raw: bytes) -> tuple[Image.Image, int]:
    with _PDFIUM:
        return _render_pdf_locked(raw)


def _render_pdf_locked(raw: bytes) -> tuple[Image.Image, int]:
    """Page 1, at 200 dpi or at the preview size, whichever is smaller.

    Page 1 in the frame and a download for the rest: paging in a side panel is
    a viewer, and this app is not one. `main.py`'s CSP already has
    `object-src 'none'`, which forbids the embedded PDF viewer that would be
    the obvious first attempt -- rendering server-side gets the same result and
    relaxes nothing.

    **The page size is read before anything is rendered.** Issue #88: this
    used to render at 200 dpi and only then compare against `MAX_PIXELS`, so a
    551-byte PDF declaring a 4000-point page allocated ~825 MiB before it was
    refused, and a 14400-point page -- the largest the PDF spec allows -- would
    have wanted about 6 GB. A page's size is a number in its header, as free
    to read as a PNG's, so the scale is chosen from it: nothing is ever
    rendered larger than `PDF_PREVIEW_MAX_EDGE`, which is all the display copy
    keeps anyway. A poster-sized page is not refused; it just renders small.

    Runs on untrusted input, so anything pypdfium2 raises becomes a sentence
    rather than a 500. The document is closed on every path: pdfium's memory is
    native, and the garbage collector does not feel its weight.
    """
    import pypdfium2

    try:
        document = pypdfium2.PdfDocument(raw)
    except Exception as broken:
        raise ValidationError("that PDF could not be read") from broken
    try:
        pages = len(document)
        if pages < 1:
            raise ValidationError("that PDF has no pages")
        page = document[0]
        try:
            width, height = page.get_size()
            if not (width > 0 and height > 0):
                raise ValidationError("that PDF's first page has no size")
            scale = min(200 / 72, PDF_PREVIEW_MAX_EDGE / max(width, height))
            bitmap = page.render(scale=scale)
            try:
                # A copy, because `to_pil` may share pdfium's buffer and the
                # bitmap is about to be closed underneath it.
                image = bitmap.to_pil().copy()
            finally:
                bitmap.close()
        finally:
            page.close()
    except ValidationError:
        raise
    except Exception as broken:
        raise ValidationError("that PDF could not be read") from broken
    finally:
        document.close()
    return image, pages


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #


def prepare(
    raw: bytes,
    *,
    original_sha256: str | None = None,
    exif_block: bytes | None = None,
    client_encoded: bool = False,
    device_fix: DeviceFix | None = None,
    keep_original_bytes: bool | None = None,
) -> Prepared:
    """Sniff, refuse, read the metadata, and encode. No session, no HTTP.

    ``original_sha256`` and ``exif_block`` are for a client that compressed
    before sending -- today the mobile capture page, tomorrow an agent. Both are
    attacker-supplied. The hash is used **only** to deduplicate against, never
    to look a blob up: a lie about it can create a duplicate or block one of
    your own uploads and can do nothing else, and that property is what makes
    accepting it safe. It has to stay true.

    ``device_fix`` is the browser's own position, sent only when somebody has
    switched the location toggle on at the capture page. It fills the GPS
    columns in **only where the photograph carried none** -- see `DeviceFix`.

    ``keep_original_bytes`` is the household's answer, resolved by the caller.
    Passed in rather than looked up because **this function has no session and
    is not going to get one**: it is 300-1200 ms of CPU that runs on a
    threadpool worker precisely so it is not holding SQLite's single writer
    lock while it encodes. `None` falls back to the environment alone, which is
    what every caller outside a request -- a test, a benchmark -- wants.
    """
    if len(raw) > MAX_RECEIPT_BYTES:
        raise TooLarge("that file is larger than this is meant for")
    if not raw:
        raise ValidationError("that file is empty")

    kind = sniff(raw)
    received = hashlib.sha256(raw).hexdigest()
    # The claim is used to deduplicate against and for nothing else. It never
    # decides which bytes are stored or served, which is the property that
    # makes accepting it from a client safe at all.
    digest = original_sha256 or received

    if kind is PDF:
        image, pages = _render_pdf(raw)
        meta = _with_device_fix(_parse_exif(None, exif_block), device_fix)
        blobs = [
            # Kept verbatim: it is already compressed, and a PDF bill is often
            # the only copy of a document whose terms are on pages 2 and 3.
            PreparedBlob(BlobRole.original, raw, kind.media_type),
            _to_avif(
                image,
                role=BlobRole.display,
                max_edge=PDF_PREVIEW_MAX_EDGE,
                quality=DISPLAY_QUALITY,
                speed=DISPLAY_SPEED,
            ),
            _to_avif(
                image,
                role=BlobRole.thumb,
                max_edge=THUMB_MAX_EDGE,
                quality=THUMB_QUALITY,
                speed=THUMB_SPEED,
            ),
        ]
        return Prepared(
            sha256=digest,
            blob_sha256=received,
            media_type=kind.media_type,
            byte_size=len(raw),
            width=image.width,
            height=image.height,
            page_count=pages,
            client_encoded=client_encoded,
            blobs=blobs,
            **_columns(meta),
        )

    image = _open_checked(raw)
    # Taken from the header, before the shrink below changes `image.size`: the
    # columns describe what was uploaded, not the derivative.
    width, height = image.size
    # Parsed BEFORE the encode, because the encode is what destroys it.
    meta = _with_device_fix(_parse_exif(image, exif_block), device_fix)
    # Shrunk first, turned second: see `_shrink_once` for why the order is the
    # memory bound and not a detail.
    try:
        _shrink_once(image, DISPLAY_MAX_EDGE)
    except (OSError, SyntaxError, ValueError) as broken:
        # The header read fine and the pixels did not: a truncated upload.
        raise ValidationError("that file could not be read as an image") from broken
    # Applied to the pixels, so the stored image needs no orientation tag and
    # every viewer agrees which way is up. A receipt photographed in portrait
    # and stored sideways is the most common complaint about every app that
    # skips this.
    upright = ImageOps.exif_transpose(image) or image

    display = _to_avif(
        upright,
        role=BlobRole.display,
        max_edge=DISPLAY_MAX_EDGE,
        quality=DISPLAY_QUALITY,
        speed=DISPLAY_SPEED,
    )
    # From the display-size copy, never from the upload: the thumbnail is a
    # sixth of the display edge and does not need the original's pixels.
    thumb = _to_avif(
        upright,
        role=BlobRole.thumb,
        max_edge=THUMB_MAX_EDGE,
        quality=THUMB_QUALITY,
        speed=THUMB_SPEED,
    )
    blobs = [display, thumb]
    wanted = keep_original_everywhere() if keep_original_bytes is None else keep_original_bytes
    if kind.keep_original_always or wanted:
        blobs.insert(0, PreparedBlob(BlobRole.original, raw, kind.media_type))

    return Prepared(
        sha256=digest,
        blob_sha256=received,
        media_type=kind.media_type,
        byte_size=len(raw),
        width=width,
        height=height,
        client_encoded=client_encoded,
        blobs=blobs,
        **_columns(meta),
    )


def _columns(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "captured_at": meta.get("captured_at"),
        "captured_at_is_local": bool(meta.get("captured_at_is_local")),
        "gps_lat": meta.get("gps_lat"),
        "gps_lon": meta.get("gps_lon"),
        "gps_accuracy_m": meta.get("gps_accuracy_m"),
        "gps_bearing": meta.get("gps_bearing"),
        "camera": meta.get("camera"),
        "exif": meta.get("exif"),
    }


# --------------------------------------------------------------------------- #
# The name it downloads as
# --------------------------------------------------------------------------- #

#: Beyond this a filename is unreadable rather than informative, and some
#: filesystems stop at 255 bytes for the whole thing.
_SLUG_MAX = 40


def _slug(text: str | None) -> str:
    keep = []
    for character in (text or "").lower():
        if character.isalnum() and character.isascii():
            keep.append(character)
        elif keep and keep[-1] != "-":
            keep.append("-")
    return "".join(keep).strip("-")[:_SLUG_MAX].strip("-")


def suffix_for(media_type: str) -> str:
    """The extension, from the SNIFFED type and never from the uploaded name."""
    for kind in (JPEG, PNG, PDF, WEBP, AVIF_KIND, GIF, TIFF, BMP, HEIC):
        if kind.media_type == media_type:
            return kind.suffix
    return "bin"


def display_name(
    receipt: Receipt,
    txn: Transaction | None,
    household: Household,
    *,
    media_type: str | None = None,
    payee_name: str | None = None,
    index: int = 0,
) -> str:
    """The name this receipt downloads as, computed from live state.

    Stored nowhere. The previous build kept an absolute path in the database,
    which recorded where *one machine* put a file -- broken by a container, by a
    restore to another host, and by the data directory moving, which the
    production plan says should happen. Deriving it inverts that defect: a
    receipt that moves from the inbox onto a transaction renames itself with no
    rewrite anywhere.
    """
    house = _slug(household.name) or "household"
    suffix = suffix_for(media_type or receipt.media_type)
    tail = f"-{index + 1}" if index else ""

    if txn is None:
        # `created_at` is only filled at insert, so a row that has not been
        # flushed yet -- which is what the panel holds while an upload is in
        # flight -- falls back to now rather than crashing on a name.
        when = (receipt.captured_at or receipt.created_at or utcnow()).date().isoformat()
        return f"{house}/inbox/{when}-{receipt.id[:8]}.{suffix}"

    when = txn.date.isoformat()
    payee = _slug(payee_name)
    middle = f"{when}-{payee}" if payee else when
    return f"{house}/{middle}-{txn.id[:8]}{tail}.{suffix}"


# --------------------------------------------------------------------------- #
# Storing what `prepare` produced
# --------------------------------------------------------------------------- #


def existing_attachment(
    session: Session, *, household_id: str, transaction_id: str | None, sha256: str
) -> Receipt | None:
    """The row that makes this upload a duplicate, if there is one.

    Two cases, and the second was missed the first time round.

    **On a transaction**, the unique constraint refuses it and this is only the
    good message -- checked before the batch opens, so a mis-click does not
    leave a `failed` batch in History.

    **In the inbox**, there is no constraint to lean on: uniqueness is scoped
    to `(transaction_id, content_sha256)` and SQL holds that two NULLs are not
    equal, so the database happily takes the same file twice. The first
    version of this treated that as correct -- "the inbox is where a photo
    lands before anybody has decided anything" -- and it is wrong. The reason
    a duplicate is *allowed* elsewhere is that one bill can be evidence for two
    rows; neither of two inbox copies is evidence for anything, so there is no
    reading under which the second one is meant.

    It is refused here rather than by a partial unique index, deliberately.
    An index would also catch "replace", which detaches a receipt **to the
    inbox** -- and if the same bytes were already sitting there, a legitimate
    one-click action would become a 500. Detaching is not submitting a file.
    """
    if transaction_id is None:
        return session.execute(
            select(Receipt)
            .where(
                Receipt.household_id == household_id,
                Receipt.transaction_id.is_(None),
                Receipt.content_sha256 == sha256,
            )
            .order_by(Receipt.created_at)
        ).scalars().first()
    return session.execute(
        select(Receipt).where(
            Receipt.transaction_id == transaction_id,
            Receipt.content_sha256 == sha256,
        )
    ).scalar_one_or_none()


def elsewhere_in_household(
    session: Session, *, household_id: str, sha256: str, exclude_id: str | None = None
) -> list[Receipt]:
    """The same bytes already attached somewhere else in this household.

    Allowed, and worth saying: the same PDF can legitimately be evidence for
    two transactions -- a card bill covering two rows, an invoice paid in two
    instalments -- and refusing that would be refusing a real shape. But the
    other reading is that somebody attached it to the wrong row a minute ago.
    """
    stmt = select(Receipt).where(
        Receipt.household_id == household_id,
        Receipt.content_sha256 == sha256,
        Receipt.transaction_id.is_not(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(Receipt.id != exclude_id)
    return list(session.execute(stmt).scalars())


def _put_blobs(session: Session, prepared: Prepared) -> None:
    """Insert what is not already there, and un-orphan what is.

    Not audited, so no batch is involved and the hook ignores these entirely.
    Two households uploading the same PDF store it once and neither can tell.
    """
    for blob in prepared.blobs:
        existing = session.get(ReceiptBlob, (prepared.blob_sha256, blob.role))
        if existing is not None:
            # Something points at it again, so it is no longer a sweep
            # candidate. Clearing this is what stops a re-attach inside the
            # grace window racing the sweep.
            existing.orphaned_at = None
            continue
        session.add(
            ReceiptBlob(
                sha256=prepared.blob_sha256,
                role=blob.role,
                data=blob.data,
                media_type=blob.media_type,
                byte_size=blob.byte_size,
                width=blob.width,
                height=blob.height,
            )
        )
    session.flush()


def store(
    session: Session,
    *,
    household_id: str,
    prepared: Prepared,
    transaction_id: str | None,
    uploaded_by_id: str | None,
    original_filename: str | None = None,
    note: str | None = None,
) -> Receipt:
    """Write the blobs and the row. Call inside a batch."""
    _put_blobs(session, prepared)
    receipt = Receipt(
        household_id=household_id,
        transaction_id=transaction_id,
        content_sha256=prepared.sha256,
        blob_sha256=prepared.blob_sha256,
        original_filename=(original_filename or None),
        media_type=prepared.media_type,
        byte_size=prepared.byte_size,
        width=prepared.width,
        height=prepared.height,
        page_count=prepared.page_count,
        captured_at=prepared.captured_at,
        captured_at_is_local=prepared.captured_at_is_local,
        gps_lat=prepared.gps_lat,
        gps_lon=prepared.gps_lon,
        gps_accuracy_m=prepared.gps_accuracy_m,
        gps_bearing=prepared.gps_bearing,
        camera=prepared.camera,
        exif=prepared.exif,
        client_encoded=prepared.client_encoded,
        note=note,
    )
    receipt.uploaded_by_id = uploaded_by_id
    session.add(receipt)
    session.flush()
    return receipt


def blob_for(session: Session, sha256: str, role: BlobRole) -> ReceiptBlob | None:
    return session.get(ReceiptBlob, (sha256, role))


def for_transaction(session: Session, transaction_id: str) -> list[Receipt]:
    return list(
        session.execute(
            select(Receipt)
            .where(Receipt.transaction_id == transaction_id)
            .order_by(Receipt.created_at, Receipt.id)
        ).scalars()
    )


def receipted_ids(session: Session, household_id: str) -> set[str]:
    """Which transactions have a receipt. One indexed read, one set.

    The same shape as the register's `_payee_names` and `_category_names`:
    fetch the small lookup whole, once, and resolve against it in Python. A
    household with 5,000 receipts hands back 5,000 short strings.

    Deliberately **not** a column on `transactions`. That would be a second
    place the truth lives, maintained from four code paths including undo, and
    it would drift -- which is the first standing rule in CLAUDE.md. And
    deliberately not a LEFT JOIN with a GROUP BY on the register query, which
    would put an aggregate on the one query whose cost is already measured and
    would disturb `_ordering`'s six sort modes and its running-balance path.
    """
    return set(
        session.execute(
            select(Receipt.transaction_id)
            .where(
                Receipt.household_id == household_id,
                Receipt.transaction_id.is_not(None),
            )
            .distinct()
        ).scalars()
    )


def copy_onto(session: Session, receipt: Receipt, transaction_id: str) -> Receipt:
    """A second attachment of the same bytes, on another transaction.

    Free, because the store is content-addressed: five parts of a split cost
    five rows of about 200 bytes and one image.
    """
    twin = Receipt(
        household_id=receipt.household_id,
        transaction_id=transaction_id,
        content_sha256=receipt.content_sha256,
        blob_sha256=receipt.blob_sha256,
        original_filename=receipt.original_filename,
        media_type=receipt.media_type,
        byte_size=receipt.byte_size,
        width=receipt.width,
        height=receipt.height,
        page_count=receipt.page_count,
        captured_at=receipt.captured_at,
        captured_at_is_local=receipt.captured_at_is_local,
        gps_lat=receipt.gps_lat,
        gps_lon=receipt.gps_lon,
        gps_accuracy_m=receipt.gps_accuracy_m,
        gps_bearing=receipt.gps_bearing,
        camera=receipt.camera,
        exif=receipt.exif,
        client_encoded=receipt.client_encoded,
        note=receipt.note,
    )
    twin.uploaded_by_id = receipt.uploaded_by_id
    session.add(twin)
    session.flush()
    return twin


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #


def sweep_orphan_blobs(session: Session, *, now: datetime | None = None) -> int:
    """Delete bytes nothing points at any more, twenty-four hours after.

    Content-addressing makes this a reference count rather than a cascade, and
    it must not be a cascade: two receipts can legitimately share one sha256 --
    a bill covering two rows, or the parts of a split -- so deleting one must
    not take the other's bytes with it.

    **The grace period is the whole design.** `audit/undo.py` replays change
    images and `receipt_blobs` has no change rows, so an undo re-inserts a
    receipt row and knows nothing about bytes. Without the window, "undo that"
    restores a receipt pointing at nothing -- which is worse than not undoing,
    because it looks like it worked.

    Two passes, and `orphaned_at` is a column rather than a use of
    `created_at`, because the window has to run from when the blob was
    *orphaned*: a blob uploaded a week ago whose last receipt was deleted a
    minute ago is exactly the one an undo is about to need.

    audit-exempt: `receipt_blobs` is `__audit__ = False`, so these are bulk
    statements with nothing for the hook to miss -- and this says so where the
    grep can see it.
    """
    at = now or utcnow()
    # `blob_sha256`, not `content_sha256`: the store is keyed by what arrived.
    referenced = select(Receipt.blob_sha256).distinct().scalar_subquery()

    # Pass one: anything nothing points at starts its clock, and anything that
    # has regained a reference stops it. The second half matters -- a receipt
    # re-attached from the inbox inside the window must not be swept out from
    # under.
    newly_orphaned = session.execute(  # audit-exempt: receipt_blobs is not audited
        select(ReceiptBlob).where(
            ReceiptBlob.orphaned_at.is_(None),
            ReceiptBlob.sha256.not_in(referenced),
        )
    ).scalars()
    for blob in newly_orphaned:
        blob.orphaned_at = at

    adopted = session.execute(  # audit-exempt: receipt_blobs is not audited
        select(ReceiptBlob).where(
            ReceiptBlob.orphaned_at.is_not(None),
            ReceiptBlob.sha256.in_(referenced),
        )
    ).scalars()
    for blob in adopted:
        blob.orphaned_at = None
    session.flush()

    # Pass two: whatever has been orphaned for longer than the window.
    doomed = list(
        session.execute(  # audit-exempt: receipt_blobs is not audited
            select(ReceiptBlob).where(
                ReceiptBlob.orphaned_at.is_not(None),
                ReceiptBlob.orphaned_at <= at - ORPHAN_GRACE,
                ReceiptBlob.sha256.not_in(referenced),
            )
        ).scalars()
    )
    for blob in doomed:
        session.delete(blob)
    session.flush()
    return len(doomed)
