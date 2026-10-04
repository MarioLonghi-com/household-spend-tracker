"""Reading a statement out of a PDF, by where the words are.

A PDF has no columns -- it has glyphs at coordinates. Extracting its text and
splitting on spaces gets you a line like::

    2026-08-30 Uttag -5 000,00 kr 51 491,52 kr

where the last number is the **running balance** and the one before it is the
amount. Take the last number, as any "find a number on the line" reader would,
and every row imports the account's balance as a transaction. The Itau extrato
is worse: its balance-of-the-day lines carry a number in the *saldo* column and
nothing in *valor*, so text alone cannot tell a 6.044,66 balance from a 6.044,66
payment.

So this reads positions. It finds the header row, learns where each column sits
on the page, and assigns every later word to a column by where it is -- which is
the same thing the CSV reader does, only the columns have to be measured first.
The header appears on page one and the table runs on without it, so the
measurement carries to every later page.

What comes out is CSV. Everything after that -- which column is the date, which
way the money went, what a balance column is -- stays in `sniffing`, where it
already works for six other formats. **This module decides nothing about
meaning.**

It refuses rather than guesses. A designed credit-card bill with two tables side
by side and marketing copy between them is not a table with a header, and half
of one imported as transactions is worse than a clear no.
"""

from __future__ import annotations

import csv
import io
import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass

from . import pdf_layouts, pdf_limits, signs
from .errors import UnreadablePdf

#: The library's refusals are sentences a person can act on, so what the
#: underlying reader said goes here, at DEBUG, rather than into the sentence
#: (#224).
log = logging.getLogger(__name__)

#: Words closer together than this vertically are the same line. Statement rows
#: are ~10pt apart; this is loose enough for a font change mid-row and tight
#: enough not to merge two rows.
_LINE_TOLERANCE = 3.0
#: A gap wider than this between two header words starts a new column rather
#: than continuing a multi-word label like "valor (R$)".
_COLUMN_GAP = 12.0
#: How far down the document to look for the header before giving up.
_HEADER_SEARCH_LINES = 60


@dataclass(slots=True)
class _Column:
    label: str
    x0: float
    x1: float


def looks_like_pdf(raw: bytes) -> bool:
    return raw[:5] == b"%PDF-"


def _lines(page) -> list[list[dict]]:
    """Words grouped into visual lines, each left to right."""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    rows: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if rows and abs(rows[-1][0]["top"] - word["top"]) <= _LINE_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def _columns(header: list[dict]) -> list[_Column]:
    """Group the header's words into columns, keeping multi-word labels whole."""
    columns: list[_Column] = []
    for word in header:
        if columns and word["x0"] - columns[-1].x1 <= _COLUMN_GAP:
            columns[-1].label += f" {word['text']}"
            columns[-1].x1 = word["x1"]
        else:
            columns.append(_Column(label=word["text"], x0=word["x0"], x1=word["x1"]))
    return columns


def _boundaries(columns: list[_Column]) -> list[float]:
    """Where one column stops and the next begins.

    Midway through the gap between two headers. Anchoring on the next header's
    left edge instead was tried and is worse: statement labels are not aligned
    with their own data, so descriptions fell into the date column and Swedish
    amounts -- written "-5 000,00 kr", four words with a space for thousands --
    fragmented across three cells. The midpoint is stable for numbers, and the
    one thing it gets wrong, a long description overflowing into the money
    column, is repaired afterwards by `_reclaim_overflow`.
    """
    return [(columns[i].x1 + columns[i + 1].x0) / 2 for i in range(len(columns) - 1)]


def _assign(words: list[dict], columns: list[_Column], edges: list[float]) -> list[str]:
    """One cell per column, words joined in reading order."""
    cells: list[list[str]] = [[] for _ in columns]
    for word in words:
        # Text is left-aligned in its column and numbers are right-aligned in
        # theirs, so each is placed by the edge that is actually pinned. Using
        # the centre for both put the last word of a long description into the
        # amount column: "PAG BOLETO PLANO DE APOSENTADORIA EXEMPLO -930,00"
        # lost EXEMPLO off the payee and carried it into the money cell.
        edge = word["x1"] if _numeric(word["text"]) else word["x0"]
        index = 0
        while index < len(edges) and edge > edges[index]:
            index += 1
        cells[index].append(word["text"])
    return _reclaim_overflow([" ".join(cell).strip() for cell in cells])


def _reclaim_overflow(cells: list[str]) -> list[str]:
    """Give a description back the words that spilled into the money column.

    A long payee runs past the midpoint and its last word lands in the amount
    cell: "EXEMPLO -930,00". The amount still reads correctly -- the parser
    strips non-digits -- but the payee is quietly short a word, which is the
    kind of wrong nobody notices.

    The signature is narrow on purpose: a cell whose *last* word is written the
    way a bank writes money, and which has non-numeric words before it. A cell
    like "-5 000,00 kr", where the last word is a currency suffix, is left
    exactly as it is.

    Money, not merely digits -- that distinction is the whole guard. An Itau
    current-account statement writes its description and then a document number:
    "EXAMPLE SHOP-XX 10000001". Treated as overflow, "EXAMPLE SHOP-XX" is pushed
    left into the *date* cell, which then reads "01/01/2026 EXAMPLE SHOP-XX",
    fails to parse, and the transaction is dropped. Three rows of a real file
    went missing that way.

    So the repair only fires when what stays behind is recognisably an amount:
    signed, bracketed, or carrying a decimal fraction. The failure it trades
    for is the harmless one -- a bare-integer amount leaves its payee a word
    short, and the amount itself still reads correctly either way.
    """
    for index in range(1, len(cells)):
        words = cells[index].split()
        if len(words) < 2 or not _looks_like_money(words[-1]):
            continue
        leading = words[:-1]
        if any(_numeric(word) for word in leading):
            continue  # more than one number in here; not a simple overflow
        cells[index - 1] = " ".join(filter(None, [cells[index - 1], *leading]))
        cells[index] = words[-1]
    return cells


def _looks_like_money(text: str) -> bool:
    """Is this word written the way a bank writes an amount?

    Stricter than `_numeric`: a bare run of digits is a reference number as
    often as it is a sum, and the two are only told apart by how they are
    written -- a sign, brackets, or a decimal fraction.
    """
    if not _numeric(text):
        return False
    stripped = signs.ascii_minus(text).strip()
    if stripped[0] in "+-(" or stripped[-1] in ")-":
        return True
    # A decimal fraction: the last separator has one or two digits after it.
    tail = stripped.rsplit(",", 1)[-1] if "," in stripped else stripped.rsplit(".", 1)[-1]
    return tail != stripped and len(tail) in (1, 2)


def _numeric(text: str) -> bool:
    """Is this word a number, as a bank writes one?

    Signed at either end, and with any of `signs.MINUS_SIGNS` for the minus: a
    PDF's text layer may write "−12,50" or "12,50-" for "-12,50" (#261, #262),
    and a number not seen as one is placed by its left edge, like text.

    At most one sign, at one end: ``--12--`` and ``+12.50-`` are not numbers,
    the same as `sniffing.parse_amount` says of them.
    """
    stripped = signs.ascii_minus(text).strip().strip("()")
    if not stripped:
        return False
    leading, trailing = stripped[0] in "+-", stripped[-1] in "+-"
    if leading and trailing:
        return False
    core = stripped[1 if leading else 0 : len(stripped) - (1 if trailing else 0)]
    core = core.replace(".", "").replace(",", "").replace("\u00a0", "")
    return bool(core) and core.isdigit()


def _read_pages(raw: bytes, pdfplumber) -> list[list[list[dict]]]:
    """Every page's lines, within the limits in `pdf_limits` (issue #87).

    The pages are counted before any of them is read, and everything pdfminer
    does -- unpacking, interpreting, laying out -- is charged to one budget. A
    limit crossed anywhere in here comes out as that limit's own sentence,
    whatever pdfplumber wrapped it in on the way.
    """
    from pdfminer.pdfpage import PDFPage

    with pdf_limits.budget() as spent:
        try:
            try:
                pdf = pdfplumber.open(io.BytesIO(raw))
            except Exception as exc:
                log.debug("pdfplumber could not open the file", exc_info=True)
                raise UnreadablePdf(
                    "this file could not be opened as a PDF. It may be damaged; "
                    "download it from your bank again."
                ) from exc
            with pdf:
                # Walked lazily and stopped one past the limit, so a document
                # claiming a million pages costs a hundred and one.
                counted = sum(
                    1 for _ in itertools.islice(
                        PDFPage.create_pages(pdf.doc), pdf_limits.MAX_PAGES + 1
                    )
                )
                pdf_limits.check_pages(counted)
                return [_lines(page) for page in pdf.pages]
        except Exception as exc:
            if spent.refusal is not None:
                raise spent.refusal from None
            if isinstance(exc, UnreadablePdf):
                raise
            log.debug("pdfminer could not read the PDF", exc_info=True)
            raise UnreadablePdf(
                "this PDF could not be read. It may be damaged; download it from your bank again."
            ) from exc


def to_csv_bytes(raw: bytes, *, score_header: Callable[[list[str]], int]) -> bytes:
    """The statement table, as CSV.

    ``score_header`` says how much a row of text looks like a header. It is
    passed in rather than imported so that what a column *means* stays in one
    module, and so this one can be read without knowing about any of it.
    """
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - the dependency is declared
        raise UnreadablePdf(
            "reading PDFs needs the pdfplumber package, which is not installed"
        ) from exc

    pages = _read_pages(raw, pdfplumber)

    # A document with a rule of its own is tried first -- it is there precisely
    # because reading it as a table gives the wrong answer.
    by_layout = pdf_layouts.apply(pages)
    if by_layout is not None:
        return by_layout

    if not any(pages):
        raise UnreadablePdf(
            "there is no text in this PDF -- it is probably a scan, and reading those "
            "needs character recognition this app does not do. Ask your bank for CSV or OFX."
        )

    header: list[dict] | None = None
    header_at: tuple[int, int] | None = None
    best = 0
    for page_index, lines in enumerate(pages):
        for line_index, line in enumerate(lines[:_HEADER_SEARCH_LINES]):
            texts = [w["text"] for w in line]
            # A header is labels. A row carrying figures is a summary box, and
            # a designed bill is full of them: the Itau fatura's "Encargos R$
            # 119,74 - Valor solicitado R$ 1.098,90 98,30 %" scores well on
            # vocabulary alone and yields 146 rows of nonsense.
            if any(_numeric(text) for text in texts):
                continue
            score = score_header(texts)
            if score > best:
                best, header, header_at = score, line, (page_index, line_index)
        if header is not None:
            break

    # Two matches is a date column and a money column, which is the least a
    # statement can have. One is a coincidence -- "Data" in a footer.
    if header is None or best < 2 or header_at is None:
        raise UnreadablePdf(
            "no statement table was found in this PDF. Some documents -- a designed "
            "credit-card bill with several tables side by side, for instance -- are not a "
            "single table with a header, and importing half of one would be worse than "
            "not importing it. Ask your bank for CSV or OFX."
        )

    columns = _columns(header)
    edges = _boundaries(columns)

    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([column.label for column in columns])

    first_page, first_line = header_at
    for page_index, lines in enumerate(pages):
        if page_index < first_page:
            continue
        start = first_line + 1 if page_index == first_page else 0
        for line in lines[start:]:
            cells = _assign(line, columns, edges)
            if any(cell for cell in cells):
                writer.writerow(cells)

    return out.getvalue().encode("utf-8")
