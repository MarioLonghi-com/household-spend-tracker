"""What the bytes of a statement file say about their own encoding.

Shared by `sniffing` (tables) and `ofx`, which cannot import each other this way
round: `sniffing` already imports `ofx`. One table of byte-order marks, so the
two readers cannot disagree about which marks they believe (#263).
"""

from __future__ import annotations

import codecs

#: A byte-order mark names its encoding outright, so it is believed before any
#: guess. UTF-32 first: its little-endian mark is UTF-16's followed by two NULs.
#: The codecs are the BOM-aware ones, which read the mark and drop it.
BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def bom_encoding(raw: bytes) -> str | None:
    """The encoding `raw`'s byte-order mark names, or None when it has none.

    UTF-8's mark is not here: `utf-8-sig` already reads it, and a file with it
    is read by the same strict list as one without.
    """
    for bom, encoding in BOMS:
        if raw.startswith(bom):
            return encoding
    return None


#: The five bytes cp1252 leaves undefined. Read as Latin-1 reads them -- the
#: C1 control character of the same number -- rather than failing the whole
#: file over one of them (#258).
CP1252_UNDEFINED = frozenset({0x81, 0x8D, 0x8F, 0x90, 0x9D})

#: The error handler that does that. Registered once, process-wide, by name.
CP1252_FALLBACK = "statements-cp1252-latin1"


def _cp1252_fallback(error: UnicodeError) -> tuple[str, int]:
    if not isinstance(error, UnicodeDecodeError):
        raise error
    chunk = error.object[error.start : error.end]
    if not all(byte in CP1252_UNDEFINED for byte in chunk):
        raise error
    return "".join(chr(byte) for byte in chunk), error.end


codecs.register_error(CP1252_FALLBACK, _cp1252_fallback)


def decode_cp1252(raw: bytes) -> str:
    """cp1252, with its five undefined bytes read as Latin-1 reads them.

    Never raises: every other byte is defined in cp1252. A byte read through
    the fallback is a C1 control character in the text, which is what the
    caller warns about.
    """
    return raw.decode("cp1252", errors=CP1252_FALLBACK)


def has_c1_controls(text: str) -> bool:
    """Any character in U+0080-U+009F: a byte no Windows or Latin encoding
    meant as text, so the file probably mixes two encodings."""
    return any("\x80" <= char <= "\x9f" for char in text)
