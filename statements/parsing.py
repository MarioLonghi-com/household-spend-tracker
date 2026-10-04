"""Reading a statement file into rows, with nothing else attached.

This is the whole of the format knowledge: give it bytes, get back one
`ParsedRow` per line the file contained, including the lines that could not be
read — those come back carrying a `problem` rather than raising, because one bad
date should cost you one row, not the file.

Amounts stay `Decimal` on the way out. Converting to minor units needs to know
the account's currency, and this library does not know about accounts.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from . import ofx, sniffing
from .errors import UnreadableStatement

#: Rows whose date matched nothing, past which the file is refused rather than
#: read on. A real statement has a handful at most -- a title line, a total
#: -- and every one of these costs every date format there is (#222).
MAX_UNDATED_ROWS = 1_000


@dataclass(slots=True)
class ParsedRow:
    line_no: int
    raw: str
    when: date | None = None
    amount: Decimal | None = None
    payee: str | None = None
    memo: str | None = None
    problem: str | None = None
    #: The bank's own identifier, when the format carries one (OFX). Preferred
    #: over the derived key, because the bank promises it is stable.
    fitid: str | None = None
    #: Whatever else the format said about this row. Kept so the transaction can
    #: still be explained years later, when the file it came from is gone.
    details: dict = dataclasses.field(default_factory=dict)
    #: Which account this row belongs to, in a file that holds several (the
    #: value of the product column). None when the file holds one account.
    product: str | None = None
    #: Charged on top of `amount`, as a positive figure. None when the file
    #: has no fee column; the running balance moves by `amount - fee`.
    fee: Decimal | None = None
    #: The statement's running balance after this row, when it states one.
    balance: Decimal | None = None
    #: The currency the file states for this row's amount, as an upper-case
    #: ISO code: a currency column's value, or an OFX transaction's own
    #: `<CURRENCY>` and otherwise its statement's `CURDEF`. None
    #: when the file does not say, and the account's currency is assumed --
    #: which is all any row was before issue #260.
    currency: str | None = None
    #: Where `currency` came from: ``"column"`` for a table's currency column,
    #: ``"ofx"`` for an OFX file. It matters to the caller: a table may hold
    #: several accounts' rows in several currencies, while an OFX file is one
    #: account (one `ACCTID`), so a row in another currency there is still that
    #: account's money.
    currency_from: str | None = None
    #: The bank's rate for a transaction in another currency, as written (OFX
    #: `CURRATE`). For the person to read; nothing converts with it.
    currency_rate: str | None = None



def file_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()



def _parse_ofx(raw: bytes) -> list[ParsedRow]:
    """OFX needs no sniffing: every field says what it is.

    The bank's own `FITID` is carried through as the import id. It is a better
    key than anything derived here -- the bank guarantees it is stable across
    downloads -- so two statements that overlap dedupe exactly rather than by
    the (amount, date, nth that day) approximation the CSV path has to use.
    """
    statement = ofx.read(raw)
    rows: list[ParsedRow] = []
    for index, transaction in enumerate(statement.transactions, start=1):
        row = ParsedRow(line_no=index, raw=transaction.raw)
        if transaction.problem:
            row.problem = transaction.problem
            rows.append(row)
            continue
        row.when = transaction.posted
        # Left as a Decimal, like the tabular path: `stage()` converts it with
        # the *account's* currency, which is the only place that knows the
        # right exponent.
        row.amount = transaction.amount
        row.payee = transaction.name or transaction.memo
        row.memo = transaction.memo if transaction.name else None
        row.fitid = transaction.fitid
        row.currency = transaction.currency
        row.currency_from = "ofx" if transaction.currency else None
        row.currency_rate = transaction.currency_rate
        row.details = dict(transaction.details)
        rows.append(row)
    return rows


def read(
    raw: bytes, *, decimal_preference: sniffing.DecimalPreference | None = None
) -> tuple[sniffing.Sniffed, list[ParsedRow]]:
    """Sniff and parse a file, unpacking a spreadsheet or PDF once.

    `sniff` and `parse` each call `as_table_bytes` so that they can never
    disagree about what they are reading -- but called one after the other on
    the same upload, a PDF was laid out and a spreadsheet expanded twice, the
    most expensive step in the whole import paid for two times. Here the
    conversion happens once and both are handed its result, which keeps the
    guarantee (they read identical bytes) and drops the second bill.

    ``decimal_preference`` is handed to `sniff`; `parse` reads with whatever
    separator the sniff settled on.
    """
    table = sniffing.as_table_bytes(raw)
    sniffed = sniffing.sniff(table, converted=True, decimal_preference=decimal_preference)
    return sniffed, parse(table, sniffed.format, converted=True)


def parse(raw: bytes, fmt: sniffing.Format, *, converted: bool = False) -> list[ParsedRow]:
    """Read every line, keeping the ones that cannot be read.

    A row that will not parse becomes one rejected line with a reason, and the
    rest of the file still imports. The previous build raised at parse time and
    lost the whole statement to one bad date.
    """
    if fmt.kind == "ofx":
        return _parse_ofx(raw)

    if not converted:
        raw = sniffing.as_table_bytes(raw)

    text, _ = sniffing.decode(raw)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    rows = sniffing.read_rows(lines, fmt.delimiter)
    # The header is wherever the sniffer found it, not row 0. These two used to
    # disagree -- headers from row 0, data from `1 + skip_rows` -- so on any
    # file with a title block above the table every column name was wrong.
    if fmt.skip_rows >= len(rows):
        return []
    headers = [h.strip() for h in rows[fmt.skip_rows]]

    def column(row: dict[str, str], name: str | None) -> str:
        return (row.get(name) or "").strip() if name else ""

    def money(row: dict[str, str], name: str | None) -> Decimal:
        try:
            return sniffing.parse_amount(column(row, name), decimal_separator=fmt.decimal_separator)
        except ValueError as exc:
            raise ValueError(f"{exc}, in the {name} column") from None

    parsed: list[ParsedRow] = []
    undated = 0
    for index, values in enumerate(rows[1 + fmt.skip_rows :], start=2 + fmt.skip_rows):
        record = dict(zip(headers, values, strict=False))
        raw_line = fmt.delimiter.join(values)
        row = ParsedRow(line_no=index, raw=raw_line)

        # A row the bank says did not happen. Importing it invents money
        # movement -- and nothing else in the row distinguishes it.
        state = sniffing.did_not_happen(record, fmt)
        if state:
            row.problem = f"the bank marked this {state}, so no money moved"
            parsed.append(row)
            continue
        state = sniffing.not_yet(record, fmt)
        if state:
            row.problem = (
                f"the bank marked this {state}: it has not settled, and will import from "
                "the next statement once it has"
            )
            parsed.append(row)
            continue

        try:
            row.when = sniffing.parse_date(column(record, fmt.date_column), fmt.date_format)
        except ValueError as exc:
            undated += 1
            if undated > MAX_UNDATED_ROWS:
                raise UnreadableStatement(
                    f"more than {MAX_UNDATED_ROWS:,} rows of this file have no readable date -- "
                    "it is not a statement, or its date column was not found. "
                    "Check the header row."
                ) from None
            row.problem = str(exc)
            parsed.append(row)
            continue

        if fmt.amount_column:
            written = column(record, fmt.amount_column)
            if not written:
                # An empty amount cell is not a zero transaction, it is a row
                # that is not a transaction at all. An Itau statement puts a
                # "SALDO DO DIA" line between every day's entries, with the
                # balance in its own column and nothing in the amount -- which
                # imported as fourteen 0.00 rows sitting in the register
                # looking like real ones. An explicit "0.00" still imports:
                # a zero-interest line is a real entry, and the difference is
                # whether the bank wrote anything there.
                row.problem = "this row has no amount, so it is not a transaction"
                parsed.append(row)
                continue
        elif not column(record, fmt.outflow_column) and not column(record, fmt.inflow_column):
            row.problem = "this row has no amount, so it is not a transaction"
            parsed.append(row)
            continue

        # Every cell that moves money is read before any is used. One the bank
        # wrote and this cannot read makes the row a problem with the cell
        # named, never a zero: a zero is how "12.50-" was skipped as moving no
        # money (#261).
        try:
            if fmt.amount_column:
                value = money(record, fmt.amount_column)
            else:
                outflow = money(record, fmt.outflow_column)
                inflow = money(record, fmt.inflow_column)
                # Some exports already sign the outflow column; abs() reads both.
                value = inflow - abs(outflow)
            fee = money(record, fmt.fee_column) if column(record, fmt.fee_column) else None
        except ValueError as exc:
            row.problem = str(exc)
            parsed.append(row)
            continue
        # The running balance moves no money, so a cell in it that cannot be
        # read costs the row its balance, not the row: it imports as a row
        # with no balance stated, and `sniff` warns about the column.
        try:
            balance = money(record, fmt.balance_column) if column(record, fmt.balance_column) else None
        except ValueError:
            balance = None
        if fmt.invert_amounts:
            value = -value

        row.amount = value
        row.product = column(record, fmt.product_column) or None
        row.currency = sniffing.currency_of(column(record, fmt.currency_column))
        row.currency_from = "column" if row.currency else None
        if fee is not None:
            row.fee = abs(fee)
        row.balance = balance
        row.payee = column(record, fmt.payee_column) or None
        row.memo = column(record, fmt.memo_column) or None
        # Every non-empty cell, under the bank's own column names. The sniffer
        # only uses four of them; the rest are why a row can be explained later.
        row.details = {
            name: text
            for name, text in record.items()
            if name and (text or "").strip()
        }
        parsed.append(row)

    return parsed



def build_import_id(amount: int, when: date, occurrence: int) -> str:
    """``(account, amount, date, nth occurrence that day)``, scoped by the
    account's unique constraint."""
    return f"ST:{amount}:{when.isoformat()}:{occurrence}"


def build_balance_import_id(amount: int, when: date, balance: int, occurrence: int) -> str:
    """``(amount, date, running balance after it, nth such row)``.

    For a statement with no transaction ids but a running balance -- Revolut's
    savings statements. The nth-that-day key above is only stable when every
    download holds the *whole* day in the same order, and a download made at
    noon holds half of one. The balance after a row does not depend on where the
    download window was cut (issue #69). `occurrence` only separates rows that
    agree on all three, which takes money in, out and in again on one day.
    """
    return f"SB:{amount}:{when.isoformat()}:{balance}:{occurrence}"

