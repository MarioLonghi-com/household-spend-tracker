"""Images built by the tests, never committed.

`test_data_hygiene` already reads the statement PDFs as text, because two real
ones survived every grep aimed at them -- a PDF keeps its strings in a
compressed stream. **An image is the same problem and worse**: a JPEG fixture
of a real receipt carries a real shop, a real card's last four, a real date
and, before stripping, real coordinates.

So there are no image fixtures on disk. Everything here is drawn by
`Image.new()` at the moment it is needed, and `test_data_hygiene` asserts that
nothing committed decodes to anything else.
"""

from __future__ import annotations

import io

from PIL import Image
from PIL.TiffImagePlugin import IFDRational

#: 40 deg 25' 17.4" N, 3 deg 41' 20.0" W -- Madrid, near enough. Written as the
#: EXIF rationals a camera actually produces, so the degrees/minutes/seconds
#: conversion is exercised rather than a float that has already had it done.
MADRID = (IFDRational(40), IFDRational(25), IFDRational(174, 10))
MADRID_LON = (IFDRational(3), IFDRational(41), IFDRational(200, 10))
MADRID_LAT_DEGREES = 40.4215
MADRID_LON_DEGREES = -3.688888888888889


def receipt_image(size: tuple[int, int] = (1200, 1600)) -> Image.Image:
    """Something with edges in it, so an encoder has work to do."""
    image = Image.new("RGB", size, (243, 241, 236))
    pixels = image.load()
    assert pixels is not None
    for row in range(40, size[1] - 40, 60):
        for column in range(40, size[0] - 40):
            for thickness in range(14):
                pixels[column, row + thickness] = (30, 28, 26) if column % 7 else (120, 118, 116)
    return image


def as_bytes(image: Image.Image, format: str = "JPEG", **params) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=format, **params)
    return buffer.getvalue()


def with_exif(
    image: Image.Image | None = None,
    *,
    gps: bool = True,
    taken: str | None = "2026:09:19 18:42:07",
    offset: str | None = "+02:00",
    lat=MADRID,
    lat_ref: str = "N",
    lon=MADRID_LON,
    lon_ref: str = "W",
    orientation: int | None = None,
    maker_note: bytes | None = None,
) -> bytes:
    """A JPEG carrying the tags a phone really writes."""
    from PIL import ExifTags

    image = image or receipt_image((600, 800))
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "Fictional"
    exif[ExifTags.Base.Model] = "Handset 9"
    if orientation is not None:
        exif[ExifTags.Base.Orientation] = orientation
    if maker_note is not None:
        exif[ExifTags.Base.MakerNote] = maker_note

    sub = exif.get_ifd(ExifTags.IFD.Exif)
    if taken is not None:
        sub[ExifTags.Base.DateTimeOriginal] = taken
    if offset is not None:
        sub[ExifTags.Base.OffsetTimeOriginal] = offset

    if gps:
        block = exif.get_ifd(ExifTags.IFD.GPSInfo)
        block[ExifTags.GPS.GPSLatitude] = lat
        block[ExifTags.GPS.GPSLatitudeRef] = lat_ref
        block[ExifTags.GPS.GPSLongitude] = lon
        block[ExifTags.GPS.GPSLongitudeRef] = lon_ref
        block[ExifTags.GPS.GPSHPositioningError] = IFDRational(4)
        block[ExifTags.GPS.GPSImgDirection] = IFDRational(1125, 10)

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def has_exif_block(raw: bytes) -> bool:
    """Whether these bytes carry metadata, asked of the bytes themselves.

    Deliberately not "was a strip function called". One line -- passing
    `exif=` to `save()` -- puts the coordinates back into every download, and
    nothing else in the suite would notice.
    """
    if b"Exif\x00\x00" in raw[:65536]:
        return True
    try:
        with Image.open(io.BytesIO(raw)) as image:
            return bool(image.getexif())
    except Exception:
        return False


def png_header_claiming(width: int, height: int) -> bytes:
    """A valid-enough PNG that *says* it is this big, without ever being it.

    The point of the pixel cap is that `Image.open` parses the header without
    decoding, so the refusal costs nothing. Building a real 12000x12000 image
    to prove that would allocate half a gigabyte in the test suite -- which is
    the very thing being defended against.
    """
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 16))
        + chunk(b"IEND", b"")
    )
