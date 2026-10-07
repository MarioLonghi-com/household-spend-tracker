"""Reading a bank export without being told how.

A monthly statement should import with nothing typed. Everything here is a
guess made from the file's own contents, and every guess is shown in the preview
so a wrong one is visible before it writes anything.

The previous build sniffed the delimiter, the columns, the date format and the
decimal separator -- but never the encoding, which was hard-coded to UTF-8 with
``errors="replace"``. A Latin-1 Spanish export therefore filled with replacement
characters silently. That is fixed here.
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from . import encodings, ofx, pdf_statement, signs, spreadsheet
from .errors import UnreadableStatement

#: The library's refusals are sentences a person can act on, so what the
#: underlying reader said goes here, at DEBUG, rather than into the sentence
#: (#224).
log = logging.getLogger(__name__)

#: Tried in order after a byte-order mark (`encodings.BOMS`). The
#: last resort is cp1252, not ISO-8859-1: Latin-1 decodes *every* byte, so
#: anything after it was never reached, which is how cp1252 sat behind it unused
#: and a Windows export's `€` and curly quotes arrived as C1 control characters
#: (#258). cp1252 leaves five bytes undefined, and one of them -- a stray UTF-8
#: "Ł" is C5 81 -- used to send the whole file back to Latin-1. Now those five
#: alone are read as Latin-1 reads them (`encodings.decode_cp1252`) and the
#: preview warns, and the rest of the file keeps its euro signs.
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252")

#: Above this share of NULs, decoded text is not text: it is UTF-16 or UTF-32
#: read a byte at a time. ASCII in UTF-16 is half NULs and in UTF-32 three
#: quarters, and no 8-bit export carries more than a stray one.
_NUL_SHARE = 0.1

#: Said when the decoded text is mostly NULs (#263). The header row is not the
#: problem, which is what the column warnings would otherwise say.
NO_BYTE_ORDER_MARK = (
    "this file looks like UTF-16 or UTF-32 text without a byte-order mark, so how to read "
    "it cannot be told for certain -- save it again as UTF-8 (or CSV UTF-8) and import that"
)

#: Said when a byte only Latin-1 could read is in the text (#258).
C1_CONTROLS = (
    "some characters in this file are control codes rather than letters -- it may mix two "
    "encodings, so check the payee names in the preview"
)

DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%Y/%m/%d",
    "%d-%m-%Y", "%m-%d-%Y", "%d %b %Y", "%d %B %Y", "%Y%m%d",
    "%d/%m/%y", "%d.%m.%y",
    # Month names with a comma, which is how en-us exports write them:
    # "Dec 24, 2025", "May 23, 2026".
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    # A date with its time on it. Revolut writes "2025-12-24 11:58:09"; reading
    # it used to work only through the `fromisoformat` fallback, while the
    # sniffer reported the format as not worked out -- a warning describing a
    # failure that never happened. The time is dropped; the cell is kept in
    # `details` like every other.
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
)

#: Swedish is in here because a Swedbank export was the first file that failed
#: on nothing but vocabulary: "Belopp", "Bokforingsdag", "Beskrivning".
_DATE_NEEDLES = ("date", "fecha", "datum", "data", "dag")
_PAYEE_NEEDLES = (
    "payee", "descrip", "concepto", "merchant", "name", "beneficiar", "detalle",
    "beskrivning", "referens", "lancamento", "lançamento", "meddelande",
    "transaktionstyp", "estabelecimento",
)
_MEMO_NEEDLES = ("memo", "note", "reference", "observ")
_AMOUNT_NEEDLES = ("amount", "importe", "value", "valor", "bedrag", "betrag", "belopp")
#: A running balance is not a transaction amount. Picking one imports the
#: account's whole history as a single enormous movement, and the column names
#: are close enough to the real ones that a needle lands on them by accident.
_NEVER_MONEY = ("balance", "saldo", "aer", "nir", "rate")
#: A row the bank is telling us did not happen. Importing a reverted card
#: payment invents money movement: one Revolut export here carries six of them,
#: -454.00 EUR between them, and nothing in the file makes them look different
#: from the forty-two that did happen.
_STATE_NEEDLES = ("state", "status", "estado")
_DID_NOT_HAPPEN = frozenset(
    {"reverted", "declined", "failed", "cancelled", "canceled", "rejected", "anulado"}
)
#: A row the bank says has not happened *yet*. Importing it is worse than
#: waiting for it: a pending card payment that is later reverted leaves a
#: phantom expense, and one that settles at a different amount (a tip, an
#: exchange rate) gets a different dedupe key and imports twice (issue #68).
#: It comes back, settled, in the next download.
_NOT_YET = frozenset({"pending", "pendiente", "hold"})
#: A column saying which of several accounts a row belongs to. Revolut's
#: account statement holds checking as `Current` and every savings pocket as
#: `Deposit`, in one file (issue #68).
_PRODUCT_NEEDLES = ("product",)
#: The currency a row's amount is in. Revolut writes `Currency` on every row of
#: an export that may hold several (issue #260); without reading it, a USD row
#: staged into a EUR account became the same figure in euros.
_CURRENCY_NEEDLES = ("currency", "moneda", "divisa", "ccy")
#: A column saying what a card purchase cost *before* it was converted --
#: "Original Currency", "Moneda origen", "Foreign currency", "Local currency",
#: "Transaction currency", "Divisa de la operación". The amount is already in
#: the account's currency; taking one of these as the row's would turn every
#: purchase abroad into a row from another account, skipped and never in the
#: ledger. Substrings, so "origin" covers "original" and "transacci" both
#: spellings of "transacción"; "origen" is Spanish and is not "origin".
_NOT_THE_ROWS_CURRENCY = (
    "origin", "original", "origen", "foreign", "extranjer", "exchange", "local",
    "transaction", "transacci", "operación", "operacion", "source", "target",
)
#: When several columns still qualify, the one these words name is the
#: currency the amounts were *posted* in: the account's, the bill's, the
#: settlement's. Spanish banks write "liquidación" for the last.
_POSTING_CURRENCY = ("account", "cuenta", "billing", "settlement", "liquidaci")
#: A header that is nothing but the word is the posting currency too: a
#: Revolut `Currency`, a Spanish `Moneda` or `Divisa`.
_PLAIN_CURRENCY = frozenset({"currency", "moneda", "divisa", "ccy"})
#: What a stated currency has to look like to be believed: an ISO 4217 code.
#: A symbol or a word ("€", "Euro") states nothing that can be compared, and is
#: left in `details` as before rather than guessed into a code.
_CURRENCY_CODE = re.compile(r"[A-Za-z]{3}")
#: What the bank charged on top of the amount. Revolut's running balance is the
#: sum of `Amount - Fee`, so a fee left unread is money the ledger never sees.
_FEE_NEEDLES = ("fee", "comisi", "commission")
#: The running balance. Never the amount (see `_NEVER_MONEY`), but read, because
#: it is the statement checking its own arithmetic (issues #68, #69).
_BALANCE_NEEDLES = ("balance", "saldo")
#: When a file has several date columns, the one that says when the bank
#: booked it. Revolut writes "Started Date" (when the card was used) and
#: "Completed Date" (when it settled); the running balance follows the second,
#: and so does the other leg of a transfer.
_BOOKED_NEEDLES = ("completed",)
#: "Debe" is the debit column -- money out -- and "Haber" the credit. Getting
#: these the wrong way round inverts every sign in a Spanish export, which is
#: the one locale this app is certain to meet.
_OUTFLOW_NEEDLES = (
    "outflow", "debit", "withdrawal", "cargo", "saida", "saída", "debe",
    "money out", "paid out",
)
_INFLOW_NEEDLES = (
    "inflow", "credit", "deposit", "abono", "entrada", "haber",
    "money in", "paid in",
)


@dataclass(slots=True)
class Format:
    """What we think this file is."""

    #: "table" for anything read as rows and columns, "ofx" for a file that
    #: states its own meaning and needs no guessing at all.
    kind: str = "table"
    encoding: str = "utf-8"
    delimiter: str = ","
    date_column: str | None = None
    payee_column: str | None = None
    memo_column: str | None = None
    amount_column: str | None = None
    outflow_column: str | None = None
    inflow_column: str | None = None
    date_format: str = "%Y-%m-%d"
    decimal_separator: str = "."
    invert_amounts: bool = False
    skip_rows: int = 0
    #: A column whose value can say the transaction never completed.
    state_column: str | None = None
    #: Which account a row belongs to, in a file that holds several.
    product_column: str | None = None
    #: The currency of each row's amount, when the file states one per row.
    currency_column: str | None = None
    fee_column: str | None = None
    balance_column: str | None = None

    def describe(self) -> dict:
        """For the batch's ``source`` and for the preview screen."""
        return {
            "kind": self.kind,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "date_column": self.date_column,
            "payee_column": self.payee_column,
            "memo_column": self.memo_column,
            "amount_column": self.amount_column,
            "outflow_column": self.outflow_column,
            "inflow_column": self.inflow_column,
            "date_format": self.date_format,
            "decimal_separator": self.decimal_separator,
            "state_column": self.state_column,
            "product_column": self.product_column,
            "currency_column": self.currency_column,
            "fee_column": self.fee_column,
            "balance_column": self.balance_column,
            "skip_rows": self.skip_rows,
        }


@dataclass(slots=True)
class Sniffed:
    format: Format
    headers: list[str] = field(default_factory=list)
    sample_rows: list[dict[str, str]] = field(default_factory=list)
    row_count: int = 0
    #: Anything the guesses could not settle, in words for the preview screen.
    warnings: list[str] = field(default_factory=list)


def decode(raw: bytes) -> tuple[str, str]:
    """Return ``(text, encoding)``, preferring the encoding that reads cleanly.

    Tried strictly, so a mis-guess raises instead of quietly producing U+FFFD --
    which is how the previous build turned "Nómina" into "N?mina" with nobody
    the wiser.

    A byte-order mark is believed first. UTF-16 or UTF-32 *without* one is
    refused with a sentence rather than guessed at: the NULs give it away, but
    a guess decides what every payee and amount in the file says, and the
    person can fix it in one step by saving the file again as UTF-8. Refused,
    it is a sentence they can act on; guessed wrong, it would be a ledger full
    of wrong text with nothing to say so.
    """
    bom = encodings.bom_encoding(raw)
    if bom:
        try:
            text, encoding = raw.decode(bom), bom
        except UnicodeDecodeError:
            # The mark is still the best evidence there is. Falling through to
            # the 8-bit readers would lose every column; this way the preview
            # warns and the rest reads.
            text, encoding = raw.decode(bom, errors="replace"), f"{bom} (with unreadable characters)"
    else:
        text, encoding = _decode_without_bom(raw)
    if text and text.count("\x00") > len(text) * _NUL_SHARE:
        raise UnreadableStatement(NO_BYTE_ORDER_MARK)
    return text, encoding


def _decode_without_bom(raw: bytes) -> tuple[str, str]:
    for encoding in ENCODINGS[:-1]:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    # Never raises; see `ENCODINGS`.
    return encodings.decode_cp1252(raw), ENCODINGS[-1]


def _guess_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Frequency, restricted to the four that appear in bank exports.
        return max(",;\t|", key=sample.count)


#: More rows than any statement has: a busy account's decade is tens of
#: thousands. A bound on everything done per row, whatever the rows hold (#222).
MAX_ROWS = 200_000


def read_rows(lines, delimiter: str) -> list[list[str]]:
    """Every row of a delimited file, or a sentence saying why not.

    The `csv` module refuses a cell longer than its field limit (128 KiB) and a
    few other shapes no statement has, with a `csv.Error` -- which is not a
    refusal this library makes on purpose, so it reached the web layer as a
    server error rather than as something the person could act on (issue #90).
    """
    rows: list[list[str]] = []
    try:
        for row in csv.reader(lines, delimiter=delimiter):
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise UnreadableStatement(
                    f"this file has more than {MAX_ROWS:,} rows, which no statement has -- "
                    "it may not be a statement. Download a shorter period from your bank."
                )
        return rows
    except csv.Error as exc:
        if "field limit" in str(exc):
            raise UnreadableStatement(
                f"one cell in this file is longer than {csv.field_size_limit():,} characters, "
                "which no statement has -- it may not be a statement, or it may be damaged. "
                "Download it from your bank again."
            ) from exc
        log.debug("csv could not read the file", exc_info=True)
        raise UnreadableStatement(
            "this file could not be read as a table. Download it from your bank again, "
            "or export it as CSV."
        ) from exc


def did_not_happen(record: dict[str, str], fmt: Format) -> str | None:
    """The state a row carries, when that state means no money moved.

    Returns the bank's own word for it, so the rejected line can quote it back
    rather than inventing a reason.
    """
    if not fmt.state_column:
        return None
    value = (record.get(fmt.state_column) or "").strip()
    return value if value.lower() in _DID_NOT_HAPPEN else None


def not_yet(record: dict[str, str], fmt: Format) -> str | None:
    """The state a row carries, when that state means it has not settled."""
    if not fmt.state_column:
        return None
    value = (record.get(fmt.state_column) or "").strip()
    return value if value.lower() in _NOT_YET else None


def currency_of(value: str | None) -> str | None:
    """A stated currency as an upper-case ISO code, or None when it is not one."""
    text = (value or "").strip()
    return text.upper() if _CURRENCY_CODE.fullmatch(text) else None


def _pick_currency(
    headers: list[str], *, taken: tuple[str | None, ...], warnings: list[str]
) -> str | None:
    """The column naming the currency each row's amount was posted in.

    Never one naming where a converted amount came from. With more than one
    candidate, the one named for the account, the bill or the settlement --
    or named only "Currency" -- wins; if that does not settle it, no column
    is used and the preview says so. Guessing wrong here skips real spending
    without a word, and using none leaves every row in the account's
    currency, which is what every row was before issue #260.
    """
    candidates = [
        header
        for header in headers
        if header not in taken
        and any(n in header.strip().lower() for n in _CURRENCY_NEEDLES)
        and not any(n in header.strip().lower() for n in _NOT_THE_ROWS_CURRENCY)
    ]
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    posting = [
        header
        for header in candidates
        if header.strip().lower() in _PLAIN_CURRENCY
        or any(n in header.strip().lower() for n in _POSTING_CURRENCY)
    ]
    if len(posting) == 1:
        return posting[0]
    warnings.append(
        f"this file has more than one currency column ({', '.join(candidates)}), and which one "
        "the amounts are in could not be told, so none was used: every row is taken to be in "
        "the account's currency"
    )
    return None


def _header_score(cells: list[str]) -> int:
    """How much this row looks like a header rather than data."""
    low = [c.strip().lower() for c in cells if c and c.strip()]
    if len(low) < 2:
        return 0
    groups = (_DATE_NEEDLES, _PAYEE_NEEDLES, _AMOUNT_NEEDLES, _OUTFLOW_NEEDLES, _INFLOW_NEEDLES)
    return sum(
        1 for needles in groups if any(needle in cell for cell in low for needle in needles)
    )


def find_header(rows: list[list[str]], limit: int = 15) -> int:
    """Which row is the header. Not always the first one.

    Banks put a title block above the table: a Santander export opens with the
    account number, the holder and the balance and only reaches "FECHA
    OPERACION | CONCEPTO | IMPORTE EUR" on row 7; a Swedbank CSV puts a period
    line above it. Reading row 0 as the header turned both into a file with no
    date column and no amount column -- the most confusing failure this
    importer had, because the file plainly has both.

    `Format.skip_rows` has existed since the first import slice and nothing ever
    set it. This is what it was for.
    """
    best, best_score = 0, 0
    for index, row in enumerate(rows[:limit]):
        score = _header_score(row)
        if score > best_score:
            best, best_score = index, score
    return best


def _pick(
    headers: list[str],
    needles: tuple[str, ...],
    *,
    taken: tuple[str | None, ...] = (),
    money: bool = False,
) -> str | None:
    """The first header matching one of these needles.

    `taken` is what makes this safe. A column can only have one job, and the
    needles overlap in ways that are obvious only in hindsight: "value" is an
    amount needle and *"Transaction/Value date"* is a date column, so a savings
    export was read with its date as its amount -- "May 23, 2026" parsed as
    23.2026, and a EUR 100.00 deposit would have imported as EUR 23.20 with
    every row looking perfectly well-formed.

    `money` additionally refuses columns that are plainly a running balance.
    """
    for header in headers:
        if header in taken:
            continue
        low = header.strip().lower()
        if money and any(bad in low for bad in _NEVER_MONEY):
            continue
        # A column whose name says "date" is never money, however many date
        # columns there are. Excluding only the one picked as *the* date left
        # "FECHA VALOR" -- the value date -- matching the amount needle "valor"
        # and becoming the amount column, which is the same mistake as
        # "Transaction/Value date" in another language.
        if money and any(needle in low for needle in _DATE_NEEDLES):
            continue
        if any(needle in low for needle in needles):
            return header
    return None


def guess_date_format(samples: list[str]) -> tuple[str, list[str]]:
    """Decide day-first vs month-first from the whole file, not one row.

    A single ``03/04/2026`` is ambiguous; a file containing ``13/04/2026``
    anywhere is not. Evidence across every row beats any one of them.
    """
    warnings: list[str] = []
    values = [v.strip() for v in samples if v and v.strip()]
    if not values:
        return "%Y-%m-%d", ["no dates found to learn the format from"]

    day_first = month_first = False
    for value in values:
        parts = re.split(r"[/.\-]", value)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            first, second = int(parts[0]), int(parts[1])
            if first > 12:
                day_first = True
            if second > 12:
                month_first = True

    ordered = list(DATE_FORMATS)
    separated = [v for v in values if len(re.split(r"[/.\-]", v)) == 3]
    if day_first and not month_first:
        ordered = ["%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%y", *ordered]
    elif month_first and not day_first:
        ordered = ["%m/%d/%Y", "%m-%d-%Y", *ordered]
    elif day_first and month_first:
        warnings.append(
            "this file has dates that read as day-first and others as month-first; "
            "check the dates in the preview"
        )
    elif separated:
        # Nothing in the file settles it: every day is 12 or less. A US export
        # of the first ten days of January would import as ten months, and
        # silently. Say so and let the preview be looked at.
        warnings.append(
            "every date in this file could be read day-first or month-first, so it was read "
            "day-first — check the dates in the preview before importing"
        )

    from datetime import datetime

    for candidate in ordered:
        try:
            for value in values:
                datetime.strptime(value, candidate)
            return candidate, warnings
        except ValueError:
            continue

    warnings.append(f"could not work out the date format from values like {values[0]!r}")
    return "%Y-%m-%d", warnings


@dataclass(frozen=True, slots=True)
class DecimalPreference:
    """Which decimal separator to assume when the file itself cannot say.

    Given by the caller, because this library knows nothing about who the
    statement belongs to: the app knows an account is held in Spain, and a
    Spanish bank writes ``1.500`` for fifteen hundred (#259). ``because``
    finishes the sentence the warning says it in -- "... was assumed, because
    ``<because>``" -- so the preview names the reason rather than a guess.
    """

    separator: str
    because: str

    def __post_init__(self) -> None:
        if self.separator not in {",", "."}:
            raise ValueError(f"{self.separator!r} is not a decimal separator")


def _settle(samples: list[str]) -> tuple[str | None, str | None]:
    """``(separator the samples prove, first amount that reads either way)``.

    Whichever separator comes last is the decimal one. With only one kind
    present, a separator written twice (``1.500.000``) is the thousands one, and
    a single one with a three-digit tail (``1.500``, ``1,500``) reads either way
    and is skipped rather than guessed from -- but remembered, as the example a
    warning can quote. One pass: this runs over every amount in the file.
    """
    either_way: str | None = None
    for value in samples:
        # A trailing sign is not part of the tail: "12,50-" has a two-digit
        # fraction, not the three-character one that reads as thousands (#261).
        # Nor are the letters of "12,500 DR" (#84).
        cleaned = signs.debit_credit(signs.ascii_minus(value or ""))[0].strip().rstrip("-+ ")
        if not cleaned:
            continue
        last_comma, last_dot = cleaned.rfind(","), cleaned.rfind(".")
        if last_comma == -1 and last_dot == -1:
            continue
        if last_comma > -1 and last_dot > -1:
            # Both present: whichever comes last is the decimal one.
            return ("," if last_comma > last_dot else "."), None
        only = "," if last_comma > -1 else "."
        if cleaned.count(only) > 1:
            # Written twice, so it groups thousands; the decimal is the other.
            return ("." if only == "," else ","), None
        # Only one separator. A three-digit tail is a thousands group, not a
        # fraction -- "1,500" and "1.500" are the same shape and neither
        # settles anything, so move on rather than guess from it.
        tail = cleaned[max(last_comma, last_dot) + 1 :]
        if len(tail) == 3:
            either_way = either_way or value.strip()
            continue
        return only, None
    return None, either_way


def settle_decimal_separator(samples: list[str]) -> str | None:
    """The decimal separator the samples prove, or None when none of them does."""
    return _settle(samples)[0]


def decide_decimal_separator(
    samples: list[str],
    preferred: DecimalPreference | None = None,
    *,
    supporting: list[str] | None = None,
) -> tuple[str, str | None]:
    """``(separator, warning)``: what the samples prove, or what was assumed.

    ``samples`` are the amounts. ``supporting`` are other cells read with the
    same separator -- a fee, a running balance -- consulted only when no amount
    settles it.

    When nothing settles it, the caller's preference, and "." without one --
    and a sentence for the preview saying so, because a comma-decimal file of
    whole thousands read with "." is a thousand times too small with every row
    looking well-formed (#259). A file with no separator in any amount gets no
    warning: it reads the same either way.

    When only the supporting cells settle it, their answer is kept -- it is
    evidence, and the preference is not -- but if it disagrees with the
    preference while the amounts read either way, that is said too: one
    stray "3250.4" in a balance column of a Spanish account would otherwise
    read every "-1.500" of rent as 1.50 without a word.
    """
    settled, example = _settle(samples)
    if settled is not None:
        return settled, None
    backed, side_example = _settle(supporting or [])
    if backed is not None:
        if example is None or preferred is None or preferred.separator == backed:
            return backed, None
        return backed, (
            f'the amounts in this file read either way (like "{example}"), so the decimal '
            f'separator "{backed}" was taken from the fee or balance column — which differs '
            f'from the "{preferred.separator}" expected because {preferred.because}. '
            "Check the amounts in the preview before importing"
        )
    example = example or side_example
    separator = preferred.separator if preferred else "."
    if example is None:
        return separator, None
    because = preferred.because if preferred else "that is the default when nothing says otherwise"
    return separator, (
        "the decimal separator could not be worked out from this file — an amount like "
        f'"{example}" reads either way — so "{separator}" was assumed, because {because}. '
        "Check the amounts in the preview before importing"
    )


def guess_decimal_separator(
    samples: list[str], preferred: DecimalPreference | None = None
) -> str:
    """The separator alone; `decide_decimal_separator` also says if it was assumed."""
    return decide_decimal_separator(samples, preferred)[0]


def parse_amount(text: str, *, decimal_separator: str = ".") -> Decimal:
    """Read a bank's idea of a number.

    Handles accounting parentheses, currency symbols, thin and non-breaking
    spaces, either separator convention, a minus sign written at the end
    (``12.50-``, issue #261) and a minus written as a dash or a real minus sign
    (`signs.MINUS_SIGNS`, issue #262), and a debit or credit said in letters
    after the figure (``12.50 DR``, ``12.50 CR``, #84). An empty cell is zero,
    which is what makes separate outflow and inflow columns work, and so is a
    cell holding nothing but a dash.

    A cell that says something and is not a number raises `ValueError` with a
    reason, and so does one signed twice (``-12.50-``, ``(-12.50)``,
    ``-12.50 DR``): which sign the bank meant is a guess. Reading either as zero is what hid #261 --
    the row was skipped as "moves no money" with the debit still in it.
    """
    cleaned, marker = signs.debit_credit(signs.ascii_minus(text or "").strip())
    cleaned = cleaned.replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    cleaned = cleaned.replace("\u2009", "")
    if not cleaned:
        return Decimal(0)

    bracketed = cleaned.startswith("(") and cleaned.endswith(")")
    if bracketed:
        cleaned = cleaned[1:-1]

    digits = re.sub(r"[^\d,.\-+]", "", cleaned)
    if digits in {"-", "+", "."}:
        # A placeholder for "nothing here", as an empty cell is.
        return Decimal(0)
    if not digits:
        raise ValueError(f"could not read {text!r} as an amount")

    leading = digits[0] if digits[0] in "-+" else ""
    trailing = digits[-1] if digits[-1] in "-+" else ""
    if (
        (leading and trailing)
        or (bracketed and (leading or trailing))
        or (marker and (leading or trailing or bracketed))
    ):
        raise ValueError(
            f"{text!r} is signed twice, so whether it is money in or money out would be a guess"
        )
    body = digits[len(leading) : len(digits) - len(trailing)]
    if "-" in body or "+" in body:
        # "SHOP-XX -930,00": a sign inside the figure is not one this reads.
        raise ValueError(f"could not read {text!r} as an amount")
    body = body.replace(".", "").replace(",", ".") if decimal_separator == "," else body.replace(",", "")
    try:
        value = Decimal(body)
    except InvalidOperation:
        raise ValueError(f"could not read {text!r} as an amount") from None
    negative = bracketed or "-" in (leading, trailing, marker)
    return -value if negative else value


def _unreadable(text: str, fmt: Format) -> bool:
    """Is this a cell the bank wrote and `parse_amount` cannot read?"""
    try:
        parse_amount(text, decimal_separator=fmt.decimal_separator)
    except ValueError:
        return True
    return False


def parse_date(text: str, preferred: str | None = None) -> date:
    """Parse with the sniffed format first, then anything else plausible.

    The cheap tries come first -- the sniffed format, ISO, and ISO's first ten
    characters -- because the sniffer chose ``preferred`` from the whole file,
    so a row that fails it is almost always not a date at all, and the long
    list is where such a row spends its time (#222). The long list still runs,
    so a file mixing two formats reads as it did.
    """
    from datetime import datetime

    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError(f"could not read {text!r} as a date")
    quick = [preferred] if preferred else []
    if "%Y-%m-%d" not in quick:
        quick.append("%Y-%m-%d")
    for candidate in quick:
        try:
            return datetime.strptime(cleaned, candidate).date()
        except (ValueError, TypeError):
            continue
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError:
        pass
    for candidate in DATE_FORMATS:
        if candidate in quick:
            continue
        try:
            return datetime.strptime(cleaned, candidate).date()
        except (ValueError, TypeError):
            continue
    raise ValueError(f"could not read {text!r} as a date")


def as_table_bytes(raw: bytes) -> bytes:
    """A spreadsheet becomes CSV; everything else is already what it is.

    Called by both `sniff` and `parse` so the two cannot disagree about what
    they are reading -- which is the bug that made `skip_rows` meaningless the
    first time around.
    """
    if spreadsheet.looks_like_spreadsheet(raw):
        return spreadsheet.to_csv_bytes(raw)
    if pdf_statement.looks_like_pdf(raw):
        # The PDF reader measures where the columns are; it is handed the same
        # scorer used to find a header row in a CSV, so what a column *means*
        # is decided here and nowhere else.
        return pdf_statement.to_csv_bytes(raw, score_header=_header_score)
    return raw


def sniff(
    raw: bytes,
    *,
    converted: bool = False,
    decimal_preference: DecimalPreference | None = None,
) -> Sniffed:
    """Work out how to read this file, and say what was guessed.

    ``converted`` says `raw` is already what `as_table_bytes` returned, so a
    spreadsheet or PDF is not unpacked a second time (see `parsing.read`).
    ``decimal_preference`` breaks the tie when no amount in the file settles
    the decimal separator; one that does settle it always wins.
    """
    # OFX first, and it is not sniffed: the file names its own fields, so
    # everything below -- which column, which way round, which separator --
    # would be guessing at something already stated.
    if not converted:
        raw = as_table_bytes(raw)
    if ofx.looks_like_ofx(raw):
        statement = ofx.read(raw)
        sample = [
            {
                "date": t.posted.isoformat() if t.posted else "",
                "payee": t.name or "",
                "memo": t.memo or "",
                "amount": str(t.amount) if t.amount is not None else "",
            }
            for t in statement.transactions[:5]
        ]
        return Sniffed(
            format=Format(kind="ofx", encoding="utf-8"),
            headers=["date", "payee", "memo", "amount"],
            sample_rows=sample,
            row_count=len(statement.transactions),
            warnings=list(statement.warnings),
        )

    text, encoding = decode(raw)
    warnings: list[str] = []
    if "unreadable" in encoding:
        warnings.append(
            "some characters in this file could not be decoded; payee names may look wrong"
        )
    if encodings.has_c1_controls(text):
        warnings.append(C1_CONTROLS)

    delimiter = _guess_delimiter(text[:8000])
    rows = [row for row in read_rows(text.splitlines(), delimiter) if any(row)]
    if not rows:
        return Sniffed(format=Format(encoding=encoding, delimiter=delimiter), warnings=["this file has no rows"])

    skip = find_header(rows)
    headers = [h.strip() for h in rows[skip]]
    body = [dict(zip(headers, row, strict=False)) for row in rows[skip + 1 :]]

    # Date first, and every later pick excludes what is already spoken for.
    # The booking date when there is one, so the date agrees with the running
    # balance and with the other leg of a transfer.
    date_column = _pick(
        [h for h in headers if any(n in h.lower() for n in _DATE_NEEDLES)], _BOOKED_NEEDLES
    ) or _pick(headers, _DATE_NEEDLES)
    outflow_column = _pick(headers, _OUTFLOW_NEEDLES, taken=(date_column,), money=True)
    inflow_column = _pick(
        headers, _INFLOW_NEEDLES, taken=(date_column, outflow_column), money=True
    )
    amount_column = _pick(
        headers,
        _AMOUNT_NEEDLES,
        taken=(date_column, outflow_column, inflow_column),
        money=True,
    )
    if amount_column and (outflow_column or inflow_column):
        # The two-column shape is the more specific reading; a third money
        # column alongside it is a balance or a fee, not the amount.
        amount_column = None
    money_columns = [c for c in (amount_column, outflow_column, inflow_column) if c]
    # The whole file, not its first forty rows. Both verdicts below are about
    # the file, and a head sample judged a Revolut export -- whose first rows
    # are all savings deposits and interest -- to have no negative amount in
    # it, when 84 of its rows were money out (issue #67). The file is already
    # in memory; forty was a cost guess, not a requirement.
    money_samples = [str(r.get(c, "")) for r in body for c in money_columns]
    date_samples = [str(r.get(date_column, "")) for r in body] if date_column else []

    date_format, date_warnings = guess_date_format(date_samples)
    warnings.extend(date_warnings)
    if not date_column:
        warnings.append("no date column found -- check the file has a header row, or add one")
    if not money_columns:
        warnings.append("no amount column found -- check the file has a header row, or add one")
    elif amount_column and not (outflow_column or inflow_column):
        signed = [value for value in money_samples if signs.written_negative(value)]
        if money_samples and not signed:
            # Every amount is positive and there is no second column saying
            # which way the money went. Importing this reads a month of
            # spending as income.
            warnings.append(
                "every amount in this file is positive, and there is no second column saying "
                "which rows are money out — check the amounts in the preview, or pick separate "
                "money-out and money-in columns"
            )

    fee_column = _pick(
        headers, _FEE_NEEDLES, taken=(date_column, amount_column, outflow_column, inflow_column)
    )
    balance_column = _pick(headers, _BALANCE_NEEDLES, taken=(date_column,))
    # The fee and the balance are read with the same separator, so they are
    # evidence too: a rent account moving only "-1.500" still states a balance
    # like "3.250,40" (#259). Only after the amounts, which are what matters,
    # and said out loud when they overrule the caller's preference.
    decimal_separator, decimal_warning = decide_decimal_separator(
        money_samples,
        decimal_preference,
        supporting=[str(r.get(c, "")) for c in (fee_column, balance_column) if c for r in body],
    )
    if decimal_warning:
        warnings.append(decimal_warning)

    fmt = Format(
        encoding=encoding,
        delimiter=delimiter,
        date_column=date_column,
        payee_column=_pick(headers, _PAYEE_NEEDLES, taken=(date_column,)),
        memo_column=_pick(
            headers, _MEMO_NEEDLES, taken=(date_column, _pick(headers, _PAYEE_NEEDLES, taken=(date_column,)))
        ),
        amount_column=amount_column,
        outflow_column=outflow_column,
        inflow_column=inflow_column,
        date_format=date_format,
        skip_rows=skip,
        state_column=_pick(headers, _STATE_NEEDLES),
        product_column=_pick(headers, _PRODUCT_NEEDLES),
        currency_column=_pick_currency(
            headers,
            taken=(date_column, amount_column, outflow_column, inflow_column),
            warnings=warnings,
        ),
        fee_column=fee_column,
        balance_column=balance_column,
        decimal_separator=decimal_separator,
    )
    if fmt.balance_column:
        unreadable = [
            text for text in (str(r.get(fmt.balance_column) or "").strip() for r in body) if _unreadable(text, fmt)
        ]
        if unreadable:
            count = "1 row" if len(unreadable) == 1 else f"{len(unreadable)} rows"
            warnings.append(
                f"the {fmt.balance_column} column is not an amount on {count} (such as "
                f"{unreadable[0]!r}) -- those rows import without a running balance"
            )
    return Sniffed(
        format=fmt,
        headers=headers,
        sample_rows=body[:5],
        row_count=len(body),
        warnings=warnings,
    )
