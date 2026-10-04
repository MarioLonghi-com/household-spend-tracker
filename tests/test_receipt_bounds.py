"""What one receipt upload is allowed to cost. Issues #88 and #89.

`test_receipt_compression.py` proves what comes *out* of an ingest. This file
proves what the ingest *spends* getting there, because both reviews found the
output correct and the cost unbounded:

* **#88** -- a PDF was rendered at 200 dpi and only then measured, so 551
  bytes declaring a 4000-point page allocated ~825 MiB before it was refused.
* **#89** -- an image was decoded at full size, turned (a full copy), then
  fitted twice (two more). A 25 MP PNG grew the process by ~350 MiB.

Memory is measured in a child process, because peak RSS only ever goes up:
measured in the test process it would report whatever the heaviest earlier
test did. Pillow's and pdfium's buffers are native, so `tracemalloc` cannot
see them either.
"""

from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys

import pypdfium2
import pytest
from PIL import Image, ImageDraw

from app.errors import ValidationError
from app.models import BlobRole
from app.services import receipts

from .receipt_fixtures import MADRID_LAT_DEGREES, has_exif_block, with_exif

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _blob(prepared, role: BlobRole):
    return next(one for one in prepared.blobs if one.role is role)


def _pdf(width_pt: float, height_pt: float) -> bytes:
    document = pypdfium2.PdfDocument.new()
    document.new_page(width_pt, height_pt)
    buffer = io.BytesIO()
    document.save(buffer)
    document.close()
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# #88: a PDF page is sized before it is rendered
# --------------------------------------------------------------------------- #


@pytest.fixture()
def renders(monkeypatch):
    """Every bitmap pdfium is asked for, as (width, height) in pixels.

    The size of that bitmap *is* the allocation -- four bytes a pixel -- so
    this records what was spent rather than that a function ran.
    """
    seen: list[tuple[int, int]] = []
    real = pypdfium2.PdfPage.render

    def spy(self, *args, **kwargs):
        bitmap = real(self, *args, **kwargs)
        seen.append((bitmap.width, bitmap.height))
        return bitmap

    monkeypatch.setattr(pypdfium2.PdfPage, "render", spy)
    return seen


def test_a_poster_sized_pdf_page_is_rendered_small_rather_than_refused(renders):
    """4000 points is 11,111 px at 200 dpi: ~470 MiB of bitmap for 551 bytes."""
    raw = _pdf(4000, 4000)
    assert len(raw) < 1024, "the point is that it is small on the wire"

    prepared = receipts.prepare(raw)

    assert renders, "page 1 was rendered"
    assert max(max(size) for size in renders) <= receipts.PDF_PREVIEW_MAX_EDGE + 1, (
        f"pdfium was asked for {renders}, which is the allocation #88 is about"
    )
    display = _blob(prepared, BlobRole.display)
    assert max(display.width, display.height) == receipts.PDF_PREVIEW_MAX_EDGE
    assert _blob(prepared, BlobRole.original).data == raw, "and the PDF itself is kept"
    assert prepared.page_count == 1


def test_the_largest_page_the_pdf_spec_allows_is_rendered_small_too(renders):
    """14400 points. At 200 dpi that is 40,000 px square and about 6 GB."""
    prepared = receipts.prepare(_pdf(14400, 14400))
    assert max(max(size) for size in renders) <= receipts.PDF_PREVIEW_MAX_EDGE + 1
    assert max(prepared.width, prepared.height) <= receipts.PDF_PREVIEW_MAX_EDGE + 1


def test_a_long_thin_page_keeps_its_shape():
    """A till roll: 80 mm wide and very long. The long edge is what is capped."""
    prepared = receipts.prepare(_pdf(227, 4000))
    display = _blob(prepared, BlobRole.display)
    assert display.height == receipts.PDF_PREVIEW_MAX_EDGE
    assert display.width == pytest.approx(227 / 4000 * receipts.PDF_PREVIEW_MAX_EDGE, abs=2)


def test_a_small_page_is_still_rendered_at_200_dpi():
    """The cap only ever lowers the scale. 100x200 pt is 278x556 px at 200 dpi."""
    prepared = receipts.prepare(_pdf(100, 200))
    display = _blob(prepared, BlobRole.display)
    assert (display.width, display.height) == (278, 556)


def test_a_pdf_whose_render_fails_is_a_sentence_and_the_document_is_closed(monkeypatch):
    """Closed on the error path as well: pdfium's memory is not the GC's to find."""
    closed: list[bool] = []
    real_close = pypdfium2.PdfDocument.close

    def close(self, *args, **kwargs):
        closed.append(True)
        return real_close(self, *args, **kwargs)

    def broken(self, *args, **kwargs):
        raise RuntimeError("pdfium could not render this")

    raw = _pdf(300, 400)  # before the spy, so its own close is not counted
    monkeypatch.setattr(pypdfium2.PdfDocument, "close", close)
    monkeypatch.setattr(pypdfium2.PdfPage, "render", broken)

    with pytest.raises(ValidationError, match="could not be read"):
        receipts.prepare(raw)
    assert closed, "the document was left open"


# --------------------------------------------------------------------------- #
# #89: an image is decoded once, at the smallest size that will do
# --------------------------------------------------------------------------- #


def _striped(size: tuple[int, int]) -> Image.Image:
    """Receipt-like, and quick to draw at this size (the fixture's loop is not)."""
    image = Image.new("RGB", size, (243, 241, 236))
    draw = ImageDraw.Draw(image)
    for row in range(40, size[1] - 40, 60):
        draw.rectangle([40, row, size[0] - 40, row + 14], fill=(30, 28, 26))
    return image


_MEASURE = """
import json, sys
def status(field):
    for line in open("/proc/self/status"):
        if line.startswith(field + ":"):
            return int(line.split()[1])  # kB
raw = open(sys.argv[1], "rb").read()
from app.services import receipts
import PIL.AvifImagePlugin  # loaded before the baseline, as it is in a server
# Reset the high-water mark to what is resident now. Not `ru_maxrss`: Linux
# carries that across fork and exec, so a child would start at pytest's peak.
with open("/proc/self/clear_refs", "w") as refs:
    refs.write("5")
before = status("VmRSS")
if sys.argv[2] == "decode":
    # The decode and the shrink alone, without the AVIF encoder's working set
    # on top -- which is what a JPEG's `draft` saves on.
    image = receipts._open_checked(raw)
    receipts._shrink_once(image, receipts.DISPLAY_MAX_EDGE)
    blobs = [["decoded", *image.size]]
else:
    prepared = receipts.prepare(raw, keep_original_bytes=False)
    blobs = [[b.role.value, b.width, b.height] for b in prepared.blobs]
grew = (status("VmHWM") - before) / 1024
print(json.dumps({"grew_mib": grew, "blobs": blobs}))
"""


def _peak(tmp_path, raw: bytes, *, stage: str = "prepare") -> dict:
    source = tmp_path / "upload.bin"
    source.write_bytes(raw)
    done = subprocess.run(
        [sys.executable, "-c", _MEASURE, str(source), stage],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


linux_only = pytest.mark.skipif(
    not pathlib.Path("/proc/self/clear_refs").exists(),
    reason="the peak is read from /proc, which is Linux's",
)


@linux_only
def test_a_large_png_is_not_copied_three_times_on_its_way_to_avif(tmp_path):
    """25 MP, sideways. Decoded, it is 72 MiB; the ingest used to spend ~350.

    What is left is one decode, the shrink, and the AVIF encoder's own working
    set. The bound is loose enough for another machine's allocator and tight
    enough that putting any one of the three full-size copies back breaks it.
    """
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    _striped((5000, 5000)).save(buffer, format="PNG", exif=exif)
    raw = buffer.getvalue()

    measured = _peak(tmp_path, raw)

    assert measured["grew_mib"] < 250, f"one upload grew the process by {measured['grew_mib']:.0f} MiB"
    assert ["display", 2000, 2000] in measured["blobs"]


def _phone_jpeg() -> bytes:
    exif = Image.Exif()
    exif[0x0112] = 6
    buffer = io.BytesIO()
    _striped((4000, 6000)).save(buffer, format="JPEG", quality=90, exif=exif)
    return buffer.getvalue()


@linux_only
def test_a_phone_sized_jpeg_costs_a_third_of_what_it_did(tmp_path):
    """24 MP, as a phone takes it. The whole ingest used to grow by ~325 MiB."""
    measured = _peak(tmp_path, _phone_jpeg())

    assert measured["grew_mib"] < 150, f"one upload grew the process by {measured['grew_mib']:.0f} MiB"
    assert ["display", 2000, 1333] in measured["blobs"], "turned upright, then fitted"


@linux_only
def test_a_phone_sized_jpeg_is_never_decoded_at_full_size(tmp_path):
    """`draft` decodes at a fraction of the size, in the DCT, before a pixel exists.

    Measured here: ~50 MiB to decode and shrink with it, ~134 without -- the
    full 72 MiB of pixels plus the resampler's working copy.
    """
    measured = _peak(tmp_path, _phone_jpeg(), stage="decode")

    assert measured["grew_mib"] < 90, f"decoding grew the process by {measured['grew_mib']:.0f} MiB"
    assert measured["blobs"] == [["decoded", 1333, 2000]], "shrunk to the display bound"


def test_the_shrunk_path_still_strips_and_still_turns(tmp_path):
    """The metadata is read from the header, before the shrink; none of it is served."""
    raw = with_exif(image=_striped((3000, 4000)), orientation=6)
    assert has_exif_block(raw)

    prepared = receipts.prepare(raw)

    assert prepared.gps_lat == pytest.approx(MADRID_LAT_DEGREES, abs=1e-4)
    assert (prepared.width, prepared.height) == (3000, 4000), "the columns describe the upload"
    display = _blob(prepared, BlobRole.display)
    thumb = _blob(prepared, BlobRole.thumb)
    assert (display.width, display.height) == (2000, 1500), "orientation 6, on the pixels"
    assert (thumb.width, thumb.height) == (320, 240)
    for one in (display, thumb):
        assert not has_exif_block(one.data), f"the {one.role} copy carries metadata"
