"""How much work one PDF is allowed to cost, and the hooks that enforce it.

pdfminer, which pdfplumber reads with, trusts the file completely. It inflates
every compressed stream with a bare ``zlib.decompress`` -- no ceiling -- so a
few hundred kilobytes of zeros-compressed-well becomes hundreds of megabytes in
memory. And it interprets every page of a document, however many there are and
however often they share one enormous content stream, so an 8 KB file of twenty
pages that all point at one stream took five seconds (issue #87). An upload may
be 8 MiB.

pdfminer has no setting for any of this, so the limits are applied from the
outside, at the places the work actually happens:

- **inflating a stream** -- Flate, LZW and RunLength are the filters that can
  make data bigger, and each is replaced by a version that stops at the
  ceiling instead of after it (fax compression, which cannot be stopped, is an
  image filter and is not decoded at all);
- **interpreting content** -- every content stream a page runs, form XObjects
  included, counted each time it is run, because a stream shared by a thousand
  pages is decoded once and interpreted a thousand times;
- **placing a glyph or a path** -- the per-object cost of building a layout.

The hooks are installed once and do nothing unless a `Budget` is active in the
current context, so any other code in the process that uses pdfminer sees it
exactly as it was. A context variable rather than a global, because imports run
in a thread pool and two may read PDFs at the same time.

Every refusal is an `UnreadablePdf` with a sentence a person can act on. It is
also recorded on the budget, because pdfplumber wraps whatever the interpreter
raises in an exception of its own, and a refusal must not come out of that
looking like a damaged file.
"""

from __future__ import annotations

import contextvars
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .errors import UnreadablePdf

#: A statement is a few pages a month; a year of a busy account is a few dozen.
MAX_PAGES = 50
#: One stream, decoded. Fonts are the largest thing a statement decodes (images
#: are never decoded here: nothing reads their pixels), and a whole embedded
#: font is rarely more than a megabyte.
MAX_STREAM_BYTES = 16 * 1024 * 1024
#: Every stream in the document, decoded, together. What is decoded is kept for
#: the life of the document, so this is close to the memory it can cost.
MAX_DECODED_BYTES = 64 * 1024 * 1024
#: Content-stream bytes interpreted, across every page, counting a shared stream
#: once per page that runs it. A dense statement page is tens of kilobytes, and
#: pdfminer interprets on the order of a hundred kilobytes a second on a slow
#: machine -- so this is the limit that bounds the time, not the memory.
MAX_CONTENT_BYTES = 2 * 1024 * 1024
#: Glyphs and paths laid out, across every page. Each one becomes an object with
#: a dozen fields and costs a tenth of a millisecond or more; a full statement
#: page has two or three thousand.
MAX_OBJECTS = 100_000

_ASK_FOR_ANOTHER = "Ask your bank for CSV or OFX, or a statement covering fewer months."


@dataclass(slots=True)
class Budget:
    """What one PDF has spent so far, and the first limit it went over."""

    decoded: int = 0
    content: int = 0
    objects: int = 0
    refusal: UnreadablePdf | None = None

    def refuse(self, message: str) -> UnreadablePdf:
        if self.refusal is None:
            self.refusal = UnreadablePdf(message)
        return self.refusal

    def charge_decoded(self, size: int) -> None:
        if size > MAX_STREAM_BYTES:
            raise self.refuse(
                "this PDF contains a compressed block that unpacks to more than "
                f"{MAX_STREAM_BYTES // (1024 * 1024)} MB, which no statement needs -- it may "
                f"be damaged, or built to be read slowly. {_ASK_FOR_ANOTHER}"
            )
        self.decoded += size
        if self.decoded > MAX_DECODED_BYTES:
            raise self.refuse(
                "this PDF unpacks to more than "
                f"{MAX_DECODED_BYTES // (1024 * 1024)} MB, which is far more than a statement "
                f"needs -- it may be damaged, or built to be read slowly. {_ASK_FOR_ANOTHER}"
            )

    def charge_content(self, size: int) -> None:
        self.content += size
        if self.content > MAX_CONTENT_BYTES:
            raise self.refuse(
                "the pages of this PDF hold more drawing instructions than a statement of any "
                f"length would -- it may be damaged, or built to be read slowly. "
                f"{_ASK_FOR_ANOTHER}"
            )

    def charge_object(self) -> None:
        self.objects += 1
        if self.objects > MAX_OBJECTS:
            raise self.refuse(
                f"this PDF puts more than {MAX_OBJECTS:,} characters and shapes on its pages, "
                f"which is far more than a statement holds. {_ASK_FOR_ANOTHER}"
            )


_active: contextvars.ContextVar[Budget | None] = contextvars.ContextVar(
    "statements_pdf_budget", default=None
)


@contextmanager
def budget() -> Iterator[Budget]:
    """Everything pdfminer does inside this block is charged to one budget."""
    _install()
    spent = Budget()
    token = _active.set(spent)
    try:
        yield spent
    finally:
        _active.reset(token)


def check_pages(count: int) -> None:
    """Called with the page count before any page is read."""
    if count > MAX_PAGES:
        raise UnreadablePdf(
            f"this PDF has more than {MAX_PAGES} pages, which is longer than any statement "
            f"this app reads. {_ASK_FOR_ANOTHER}"
        )


# --- The capped decoders -----------------------------------------------------
#
# Each one decodes at most one byte past what the budget allows, so it knows the
# limit was crossed without ever holding more than the limit.


def _room(spent: Budget) -> int:
    """How many bytes the next stream may decode to, plus the one that says no.

    Never zero: to zlib a ``max_length`` of zero means no limit at all. And once
    the budget has refused, nothing more is decoded, even when whoever caught the
    refusal carried on regardless.
    """
    if spent.refusal is not None:
        raise spent.refusal
    return max(1, min(MAX_STREAM_BYTES, MAX_DECODED_BYTES - spent.decoded) + 1)


def _inflate(data: bytes, spent: Budget) -> bytes:
    """`zlib.decompress`, stopping one byte past the ceiling.

    A damaged stream still raises `zlib.error`, and so does one that ends before
    its end -- both exactly as `zlib.decompress` does -- because pdfminer answers
    that by trying again leniently, below.
    """
    room = _room(spent)
    inflater = zlib.decompressobj()
    out = inflater.decompress(data, room)
    if len(out) < room and not inflater.eof:
        raise zlib.error("Error -5 while decompressing data: incomplete or truncated stream")
    spent.charge_decoded(len(out))
    return out


def _inflate_leniently(data: bytes, spent: Budget) -> bytes:
    """pdfminer's `decompress_corrupted`, stopping one byte past the ceiling.

    The same verdict as pdfminer's: keep what came out when only the last
    three bytes -- the checksum -- are bad, and nothing when the damage is
    earlier. pdfminer's own version feeds one byte at a time and appends to a
    growing ``bytes``, which is quadratic in what it produces; this feeds the
    body in one call and only the checksum bytewise.
    """
    room = _room(spent)
    inflater = zlib.decompressobj()
    body, checksum = data[:-3], data[-3:]
    try:
        out = inflater.decompress(body, room)
    except zlib.error:
        return b""
    for byte in checksum:
        if len(out) >= room or inflater.eof:
            break
        try:
            out += inflater.decompress(bytes((byte,)), room - len(out))
        except zlib.error:
            break
    spent.charge_decoded(len(out))
    return out


class _CappedZlib:
    """Stands in for the `zlib` module inside `pdfminer.pdftypes` only."""

    error = zlib.error

    def __getattr__(self, name: str):
        return getattr(zlib, name)

    @staticmethod
    def decompress(data: bytes, *args, **kwargs) -> bytes:
        spent = _active.get()
        if spent is None:
            return zlib.decompress(data, *args, **kwargs)
        return _inflate(data, spent)


def _decompress_corrupted(data: bytes) -> bytes:
    spent = _active.get()
    if spent is None:
        return _ORIGINAL["decompress_corrupted"](data)
    return _inflate_leniently(data, spent)


def _lzwdecode(data: bytes) -> bytes:
    spent = _active.get()
    if spent is None:
        return _ORIGINAL["lzwdecode"](data)
    from io import BytesIO

    from pdfminer.lzw import LZWDecoder

    room = _room(spent)
    pieces: list[bytes] = []
    size = 0
    for piece in LZWDecoder(BytesIO(data)).run():
        pieces.append(piece)
        size += len(piece)
        if size >= room:
            break
    spent.charge_decoded(size)
    return b"".join(pieces)


def _rldecode(data: bytes) -> bytes:
    """RunLength, as pdfminer reads it, with the output counted as it grows."""
    spent = _active.get()
    if spent is None:
        return _ORIGINAL["rldecode"](data)
    room = _room(spent)
    out = bytearray()
    index = 0
    while index < len(data) and len(out) < room:
        length = data[index]
        index += 1
        if length == 128:
            break
        if length < 128:
            out += data[index : index + length + 1]
            index += length + 1
        elif index < len(data):
            out += bytes((data[index],)) * (257 - length)
            index += 1
    spent.charge_decoded(len(out))
    return bytes(out)


def _ccittfaxdecode(data: bytes, params) -> bytes:
    """Fax compression is for scanned images, and nothing here reads an image.

    The one way it reaches a decoder while reading text is a content stream
    that claims it, and its output is sized by the file's own ``/Columns``
    rather than by its data -- so there is no ceiling to stop at. Nothing.
    """
    if _active.get() is None:
        return _ORIGINAL["ccittfaxdecode"](data, params)
    return b""


# --- The interpreter and the layout ------------------------------------------


def _charge_streams(streams) -> None:
    spent = _active.get()
    if spent is None:
        return
    from pdfminer.pdftypes import PDFStream, list_value, resolve1

    for stream in list_value(streams):
        stream = resolve1(stream)
        if isinstance(stream, PDFStream):
            spent.charge_content(len(stream.get_data()))


def _wrap_render_contents(original):
    def render_contents(self, resources, streams, *args, **kwargs):
        _charge_streams(streams)
        return original(self, resources, streams, *args, **kwargs)

    return render_contents


def _wrap_placing(original):
    def placing(self, *args, **kwargs):
        spent = _active.get()
        if spent is not None:
            spent.charge_object()
        return original(self, *args, **kwargs)

    return placing


_ORIGINAL: dict[str, Any] = {}
_installing = threading.Lock()


def _install() -> None:
    """Put the hooks in, once per process."""
    with _installing:
        if not _ORIGINAL:
            _patch()


def _patch() -> None:
    from pdfminer import converter, pdfinterp, pdftypes

    _ORIGINAL["decompress_corrupted"] = pdftypes.decompress_corrupted
    _ORIGINAL["lzwdecode"] = pdftypes.lzwdecode
    _ORIGINAL["rldecode"] = pdftypes.rldecode
    _ORIGINAL["ccittfaxdecode"] = pdftypes.ccittfaxdecode
    pdftypes.zlib = _CappedZlib()
    pdftypes.decompress_corrupted = _decompress_corrupted
    pdftypes.lzwdecode = _lzwdecode
    pdftypes.rldecode = _rldecode
    pdftypes.ccittfaxdecode = _ccittfaxdecode

    interpreter = pdfinterp.PDFPageInterpreter
    interpreter.render_contents = _wrap_render_contents(interpreter.render_contents)
    analyzer = converter.PDFLayoutAnalyzer
    analyzer.render_char = _wrap_placing(analyzer.render_char)
    analyzer.paint_path = _wrap_placing(analyzer.paint_path)
