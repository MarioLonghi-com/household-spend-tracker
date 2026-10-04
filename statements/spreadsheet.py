"""Turning a bank's spreadsheet export into rows the table reader understands.

Banks that offer "Excel" mostly mean a small sheet with a title block on top and
a table underneath -- the same shape as their CSV, in a container that needs
unpacking first. So this does exactly that and no more: read the cells, write
them out as CSV, and hand them to the reader that already knows how to find a
header row and guess a date format.

Nothing here decides what a column means. That stays in one place.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime

from .errors import UnreadableSpreadsheet

#: The library's refusals are sentences a person can act on, so what the
#: underlying reader said goes here, at DEBUG, rather than into the sentence
#: (#224).
log = logging.getLogger(__name__)

#: OLE2 compound document, which is what a real `.xls` is.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
#: A zip, which is what `.xlsx` is -- and also what a `.docx` is, so finding
#: this is not on its own a promise that the file is a spreadsheet.
_ZIP_MAGIC = b"PK\x03\x04"

#: The most an .xls can hold, which no statement comes near. Checked anyway,
#: because a damaged file can claim more.
MAX_ROWS = 65_536
MAX_COLUMNS = 256
#: Rows with something in them, times the width of the sheet: what the CSV
#: handed to the table reader will hold. Ten thousand transactions across
#: fifty columns is half of it.
MAX_CELLS = 1_000_000


def looks_like_spreadsheet(raw: bytes) -> bool:
    return raw[:8] == _OLE2_MAGIC or raw[:4] == _ZIP_MAGIC


def _cell(value: object, kind: int, datemode: int) -> str:
    """One cell as text, without inventing precision.

    Floats are the trap: `str(-18.46)` is fine but arithmetic elsewhere can
    leave `1234.5600000000001`, and a bank's export is not the place to find
    out. Six decimals is far past any currency and cuts the noise.
    """
    import xlrd

    if kind == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(value, datemode).date().isoformat()
        except (ValueError, OverflowError):
            return ""
    if kind == xlrd.XL_CELL_NUMBER:
        number = float(value)
        if number.is_integer():
            return str(int(number))
        return f"{number:.6f}".rstrip("0").rstrip(".")
    if kind == xlrd.XL_CELL_BOOLEAN:
        return "true" if value else "false"
    if kind == xlrd.XL_CELL_EMPTY or kind == xlrd.XL_CELL_BLANK:
        return ""
    return str(value).strip()


def to_csv_bytes(raw: bytes) -> bytes:
    """The first sheet, as UTF-8 CSV.

    The first sheet because that is where every bank export seen here puts the
    statement; a workbook with the table on sheet two would need a control this
    app does not have yet, and guessing would be worse than saying so.
    """
    if raw[:4] == _ZIP_MAGIC:
        raise UnreadableSpreadsheet(
            "this looks like an .xlsx file, which is not supported yet -- open it and "
            "save as CSV, or export CSV from your bank instead"
        )
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - the dependency is declared
        raise UnreadableSpreadsheet(
            "reading .xls files needs the xlrd package, which is not installed"
        ) from exc

    try:
        # `on_demand` so only the sheet read below is unpacked, and
        # `ragged_rows` so a row is as long as its last real cell rather than
        # as wide as the widest row in the sheet. Without them one cell at the
        # far corner of the grid made a 5.6 KB file expand to every one of
        # 65,536 x 256 cells, on every sheet: ten seconds and most of a
        # gigabyte (issue #86).
        book = xlrd.open_workbook(file_contents=raw, on_demand=True, ragged_rows=True)
    except Exception as exc:
        log.debug("xlrd could not open the workbook", exc_info=True)
        raise UnreadableSpreadsheet(
            "this file could not be read as a spreadsheet. It may be damaged; download it "
            "from your bank again, or export it as CSV."
        ) from exc

    try:
        if book.nsheets == 0:
            raise UnreadableSpreadsheet("this workbook has no sheets in it")
        try:
            sheet = book.sheet_by_index(0)
        except Exception as exc:
            log.debug("xlrd could not read the first sheet", exc_info=True)
            raise UnreadableSpreadsheet(
                "the first sheet of this workbook could not be read. It may be damaged; "
                "download it from your bank again, or export it as CSV."
            ) from exc
        return _sheet_as_csv(sheet, book.datemode)
    finally:
        book.release_resources()


def _sheet_as_csv(sheet, datemode: int) -> bytes:
    if sheet.nrows > MAX_ROWS or sheet.ncols > MAX_COLUMNS:
        raise UnreadableSpreadsheet(
            f"the first sheet of this workbook reaches row {sheet.nrows:,} and column "
            f"{sheet.ncols:,}, which is far larger than a statement -- this app reads at most "
            f"{MAX_ROWS:,} rows and {MAX_COLUMNS:,} columns. If the statement is in there, "
            "export it from your bank as CSV instead."
        )
    width = sheet.ncols
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    written = 0
    for index in range(sheet.nrows):
        # Only the cells this row really has; the rest of the width is padding
        # and is added as padding, so every row still has `width` cells -- the
        # shape this function has always written.
        row = [
            _cell(value, kind, datemode)
            for value, kind in zip(sheet.row_values(index), sheet.row_types(index), strict=True)
        ]
        if not any(cell.strip() for cell in row):
            continue
        written += 1
        if written * width > MAX_CELLS:
            raise UnreadableSpreadsheet(
                f"the first sheet of this workbook holds more than {MAX_CELLS:,} cells, which "
                "is far larger than a statement. If the statement is in there, export it from "
                "your bank as CSV instead."
            )
        writer.writerow(row + [""] * (width - len(row)))
    return out.getvalue().encode("utf-8")


def describe(raw: bytes) -> str:
    """For the preview, so the person knows it was unpacked rather than sniffed."""
    del raw
    return f"read from a spreadsheet at {datetime.now():%H:%M}"
