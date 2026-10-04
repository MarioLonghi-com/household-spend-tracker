"""Reading bank and credit-card statements.

Give it the bytes of a file — CSV, a delimited export under any separator, an
OFX 1.x SGML or 2.x XML download, an .xls workbook, or a PDF statement — and it
tells you what it is and what is in it. It has no database, no web framework and
no configuration; the only thing it raises on purpose is `UnreadableStatement`.

The two calls that matter:

    sniffed = sniff(raw)                  # what is this file?
    rows = parse(raw, sniffed.format)     # what is in it?

`sniff` decides the shape — which column is the date, which one the money, which
way the money went, what the decimal separator is — and reports a confidence and
its reasons, so a caller can show its work before anything is written. A file
whose every amount reads either way (``1.500``) cannot say which separator it
uses; `sniff(raw, decimal_preference=DecimalPreference(",", "..."))` lets the
caller, who knows whose statement it is, break that tie. `parse`
then walks the file with that verdict. A line it cannot read comes back as a
`ParsedRow` with a `problem` set, never as an exception: one bad date costs one
row, not the file.

Amounts come out as `Decimal`. Rounding to minor units needs to know a currency,
and a statement does not reliably state one.

There is a command line too, for looking at a file without a running app:

    python -m statements statement.csv
    python -m statements statement.pdf --json
"""

from __future__ import annotations

from . import ofx, pdf_layouts, pdf_limits, pdf_statement, sniffing, spreadsheet
from .errors import (
    LayoutMismatch,
    UnreadablePdf,
    UnreadableSpreadsheet,
    UnreadableStatement,
)
from .parsing import (
    ParsedRow,
    build_balance_import_id,
    build_import_id,
    file_digest,
    parse,
    read,
)
from .sniffing import DecimalPreference, Format, Sniffed, sniff

__all__ = [
    "DecimalPreference",
    "Format",
    "LayoutMismatch",
    "ParsedRow",
    "Sniffed",
    "UnreadablePdf",
    "UnreadableSpreadsheet",
    "UnreadableStatement",
    "build_balance_import_id",
    "build_import_id",
    "file_digest",
    "ofx",
    "parse",
    "pdf_layouts",
    "pdf_limits",
    "pdf_statement",
    "read",
    "sniff",
    "sniffing",
    "spreadsheet",
]
