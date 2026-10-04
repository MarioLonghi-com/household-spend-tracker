"""The ingest, run over real receipts rather than over drawings.

**Nothing here is committed.** The corpus is a folder of the maintainer's own receipts
under `.temp/`, which is gitignored, and this file skips itself when that
folder is absent -- so the harness travels with the repository and the
photographs never do. Point it somewhere else with
`SPENDTRACKER_RECEIPT_SAMPLES`.

It exists because the synthetic fixtures passed while the real files produced
**no metadata at all**. `_parse_exif` was writing raw tag values into the dict
destined for the JSON column, so an `ExifVersion` of `b"0232"` raised a
TypeError inside `json.dumps`, and the "a malformed block means no metadata
rather than a 500" guard swallowed it. Every drawn fixture passed, because a
drawn fixture carries only the tags the drawing put there. Real phone
photographs carrying a GPS block each produced nothing, silently.

That is the whole argument for this file: a guard that turns a failure into a
shrug will hide our bugs as happily as it handles theirs, and the only thing
that finds it is a file somebody's phone actually wrote.
"""

from __future__ import annotations

import collections
import hashlib
import os
import pathlib

import pytest

from app.services import receipts

from .receipt_fixtures import has_exif_block

SAMPLES = pathlib.Path(
    os.environ.get(
        "SPENDTRACKER_RECEIPT_SAMPLES",
        pathlib.Path(__file__).resolve().parent.parent / ".temp" / "receipts",
    )
)

pytestmark = pytest.mark.skipif(
    not SAMPLES.is_dir(), reason=f"no receipt corpus at {SAMPLES}"
)


def _files() -> list[pathlib.Path]:
    return sorted(one for one in SAMPLES.rglob("*") if one.is_file())


@pytest.fixture(scope="module")
def ingested() -> list[tuple[pathlib.Path, receipts.Prepared]]:
    out = []
    for path in _files():
        out.append((path, receipts.prepare(path.read_bytes())))
    return out


def test_every_real_receipt_is_accepted(ingested):
    """No refusals, and something readable out of each one."""
    assert ingested, "the corpus is empty"
    for path, prepared in ingested:
        assert prepared.blobs, f"{path.name} produced nothing"
        assert any(one.role.value == "display" for one in prepared.blobs), path.name


def test_the_phone_photographs_really_do_carry_a_location(ingested):
    """The test the drawn fixtures could not be.

    A drawn fixture carries exactly the tags the drawing put there, so it
    proves the parser handles what we already know how to write. These are
    files a phone wrote.
    """
    photos = [
        (path, prepared)
        for path, prepared in ingested
        if prepared.media_type.startswith("image/")
    ]
    if not photos:
        pytest.skip("this corpus has no photographs in it")

    located = [one for _, one in photos if one.gps_lat is not None]
    assert located, "not one photograph parsed a coordinate, which is how this bug looked"
    assert len(located) >= len(photos) // 2, (
        f"only {len(located)} of {len(photos)} photographs kept their location"
    )

    for prepared in located:
        assert -90 <= prepared.gps_lat <= 90 and -180 <= prepared.gps_lon <= 180
        assert prepared.captured_at is not None, "a phone that knows where also knows when"
        assert prepared.camera, "and which camera took it"
        assert prepared.exif, "and the rest of the block is in the JSON"


def test_not_one_served_byte_carries_metadata(ingested):
    """Over the whole corpus, not over one drawing."""
    leaked = [
        f"{path.name}:{blob.role.value}"
        for path, prepared in ingested
        for blob in prepared.blobs
        if blob.role.value != "original" and has_exif_block(blob.data)
    ]
    assert leaked == [], f"these derivatives still carry EXIF: {leaked[:5]}"


def test_the_json_stays_small_on_real_blocks(ingested):
    """The raw block is ~30 KiB; the parsed tags are supposed to be ~1.7."""
    import json

    sizes = [
        len(json.dumps(prepared.exif).encode())
        for _, prepared in ingested
        if prepared.exif
    ]
    if not sizes:
        pytest.skip("nothing in this corpus carries EXIF")
    assert max(sizes) <= receipts.MAX_EXIF_JSON_BYTES, f"largest block: {max(sizes)} bytes"


def test_a_sideways_phone_photo_comes_out_upright(ingested):
    """Apple writes Orientation 6 on a portrait shot and stores it landscape."""
    rotated = [
        (path, prepared)
        for path, prepared in ingested
        if prepared.media_type == "image/jpeg" and prepared.width > prepared.height
    ]
    if not rotated:
        pytest.skip("no landscape-stored photographs here")

    upright = 0
    for _, prepared in rotated:
        display = next(one for one in prepared.blobs if one.role.value == "display")
        if display.height > display.width:
            upright += 1
    assert upright, (
        "every landscape-stored photograph stayed landscape, so the EXIF "
        "orientation is not reaching the pixels"
    )


def test_the_real_corpus_holds_byte_identical_duplicates(ingested):
    """Dedupe is not theoretical: this corpus has fourteen pairs in it.

    Which is also the shape the feature exists for -- the same PDF saved twice
    under two names, months apart, by somebody filing receipts.
    """
    groups = collections.defaultdict(list)
    for path, prepared in ingested:
        groups[prepared.sha256].append(path.name)
    repeated = {sha: names for sha, names in groups.items() if len(names) > 1}
    if not repeated:
        pytest.skip("this corpus happens to have no duplicates")

    for sha, names in repeated.items():
        first = (SAMPLES / names[0]) if (SAMPLES / names[0]).exists() else None
        if first is not None:
            assert hashlib.sha256(first.read_bytes()).hexdigest() == sha, (
                "the dedupe key is the hash of the bytes as uploaded"
            )


def test_multi_page_pdfs_say_how_many_pages(ingested):
    """Which is why `page_count` is a column.

    Only twenty-nine of the hundred PDFs here are a single page. The rest run
    to eight, twenty, fifty and in one case eighty-one -- so a frame showing
    page 1 with nothing beside it presents one page as the document.
    """
    pdfs = [one for _, one in ingested if one.media_type == "application/pdf"]
    if not pdfs:
        pytest.skip("this corpus has no PDFs")

    assert all(one.page_count and one.page_count >= 1 for one in pdfs)
    assert any(one.page_count > 1 for one in pdfs), (
        "a corpus with no multi-page PDF cannot prove this"
    )
    for one in pdfs:
        assert any(blob.role.value == "original" for blob in one.blobs), (
            "the whole PDF is kept, because pages 2 onward are the part a "
            "page-1 raster throws away"
        )
