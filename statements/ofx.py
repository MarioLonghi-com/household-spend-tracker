"""Reading OFX, which banks hand out in two incompatible shapes.

A CSV has to be sniffed: which column is the date, which way the money went,
what the decimal separator is. **OFX says.** `DTPOSTED` is the date, `TRNAMT` is
signed, `NAME` is the payee, and `FITID` is the bank's own identifier for that
transaction -- which is a far better dedupe key than anything this app can
derive, because the bank guarantees it is stable across downloads.

So there is no guessing here at all, and no `Format`: an OFX file produces rows
directly.

Two shapes, and real files in this project use both:

- **OFX 1.x is SGML**, not XML. A header block of ``KEY:VALUE`` lines, then
  markup whose leaf elements are usually *unclosed* (``<TRNAMT>150.00`` with no
  end tag). An XML parser cannot read it.
- **OFX 2.x is XML**, often on a single line, and wraps everything in
  ``<?OFX ...?>``.

One tolerant reader covers both, which is less code than two and cannot drift
apart. Bank statements arrive under ``<STMTTRNRS>`` and credit cards under
``<CCSTMTTRNRS>``; the transactions inside are identical either way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from . import encodings

#: Everything before this is the OFX/SGML header or the XML declaration.
_BODY = re.compile(r"<OFX>", re.IGNORECASE)
#: Found with two plain searches rather than one ``<STMTTRN>(.*?)</STMTTRN>``.
#: That regex restarts its lazy scan from every opening tag, so a file of
#: opening tags that never close costs the square of its length -- 35 KiB took
#: three seconds, and an upload may be 8 MiB (issue #85).
_OPEN = re.compile(r"<STMTTRN>", re.IGNORECASE)
_CLOSE = re.compile(r"</STMTTRN>", re.IGNORECASE)
#: `<TAG>value</TAG>` and `<TAG>value` both, because 1.x leaves are unclosed.
_LEAF = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)", re.IGNORECASE)
_CURDEF = re.compile(r"<CURDEF>\s*([A-Z]{3})", re.IGNORECASE)
_ACCTID = re.compile(r"<ACCTID>\s*([^<\r\n]+)", re.IGNORECASE)
#: A transaction whose amount is in a currency other than the statement's
#: says so in a `<CURRENCY>` aggregate of its own. `<ORIGCURRENCY>` does not
#: match: that one names where an amount came from, already converted to
#: `CURDEF`. The aggregate is two short leaves, so only this much after its
#: opening tag is looked at -- a bounded read, whatever the block holds (#85).
_TXN_CURRENCY = re.compile(r"<CURRENCY>", re.IGNORECASE)
_TXN_CURRENCY_SPAN = 200
_CURSYM = re.compile(r"<CURSYM>\s*([A-Z]{3})\b", re.IGNORECASE)
_CURRATE = re.compile(r"<CURRATE>\s*([0-9.,]{1,32})", re.IGNORECASE)


#: TRNTYPE values from the OFX specification. HOLD is the one that changes
#: behaviour: it is "only valid in pending transactions" and means an amount is
#: under a hold, not that money moved -- the same thing a REVERTED row means in
#: a Revolut CSV.
TRANSACTION_TYPES = frozenset({
    "CREDIT", "DEBIT", "INT", "DIV", "FEE", "SRVCHG", "DEP", "ATM", "POS",
    "XFER", "CHECK", "PAYMENT", "CASH", "DIRECTDEP", "DIRECTDEBIT",
    "REPEATPMT", "HOLD", "OTHER",
})
PENDING_TYPES = frozenset({"HOLD"})


@dataclass(slots=True)
class OfxTransaction:
    fitid: str | None
    posted: date | None
    amount: Decimal | None
    name: str | None
    memo: str | None
    kind: str | None
    raw: str
    problem: str | None = None
    #: The currency of `amount`: the transaction's own `<CURRENCY>` when it
    #: has one, otherwise the statement's `CURDEF`. None when neither is said.
    currency: str | None = None
    #: The bank's `CURRATE` from that `<CURRENCY>`, as written, when it gave
    #: one. Said back to the person, never used to convert.
    currency_rate: str | None = None
    #: Everything else the bank said about this transaction. Kept whole so the
    #: row can still be explained years later, when the statement it came from
    #: is long gone.
    details: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class OfxStatement:
    currency: str | None
    account: str | None
    transactions: list[OfxTransaction]
    warnings: list[str]


def looks_like_ofx(raw: bytes) -> bool:
    """Cheap enough to run on every upload, before anything else is tried.

    A UTF-16 file with its byte-order mark has a NUL in every other byte, so
    the markers are looked for in its text, not its bytes (#263).
    """
    head = raw[:4096]
    bom = encodings.bom_encoding(head)
    if bom:
        head = head.decode(bom, errors="ignore").encode("utf-8")
    head = head.upper()
    return b"<OFX>" in head or b"OFXHEADER" in head


def _decode(raw: bytes) -> str:
    """OFX 1.x declares its charset in the header; 2.x is XML and is UTF-8.

    Tried in the order that reads cleanly, like the CSV path, rather than
    trusting a declaration that is often wrong. A byte-order mark is believed
    first, from the same table the CSV path uses.
    """
    bom = encodings.bom_encoding(raw)
    if bom:
        return raw.decode(bom, errors="replace")
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "iso-8859-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _transaction_blocks(body: str) -> list[str]:
    """What sits between each ``<STMTTRN>`` and the ``</STMTTRN>`` after it.

    The same blocks the non-greedy regex found, in linear time: each search
    starts where the last one stopped, so no byte is scanned twice. An opening
    tag with no closing tag after it ends the walk, because no later opening
    tag can have one either.
    """
    blocks: list[str] = []
    position = 0
    while True:
        opened = _OPEN.search(body, position)
        if opened is None:
            break
        closed = _CLOSE.search(body, opened.end())
        if closed is None:
            break
        blocks.append(body[opened.end() : closed.start()])
        position = closed.end()
    return blocks


def parse_ofx_date(value: str) -> date | None:
    """OFX dates are ``YYYYMMDD`` with optional time and zone.

    Real values seen here: ``20250908``, ``20260302172121`` and
    ``20260725000000.000[-7:MST]``. Only the date part is kept -- a statement
    line is a day, and keeping a timezone would invite arithmetic nobody wants.
    """
    # Longer than any date with a time and a zone. Cut before the regexes
    # below, which would otherwise scan a megabyte of "[" once per bracket.
    cleaned = (value or "").strip()[:64]
    if not cleaned:
        return None
    # Drop the [offset:ZONE] suffix and any fractional seconds.
    cleaned = re.sub(r"\[.*?\]", "", cleaned).strip()
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 8:
        return None
    try:
        return datetime.strptime(digits[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _amount(value: str) -> Decimal | None:
    cleaned = (value or "").strip().replace(",", "")
    if not cleaned:
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    # NaN, sNaN and Infinity all parse, and none is an amount a bank can move.
    # None becomes "no usable amount in 'NaN'" one step down, which is the
    # one-bad-row contract; a NaN let through made the staging sort raise and
    # the whole upload a 500 (issue #218).
    return amount if amount.is_finite() else None


def _currency_of(block: str) -> tuple[str | None, str | None]:
    """``(currency, rate)`` a transaction states apart from `CURDEF`, if any."""
    aggregate = _TXN_CURRENCY.search(block)
    if aggregate is None:
        return None, None
    inside = block[aggregate.end() : aggregate.end() + _TXN_CURRENCY_SPAN]
    inside = re.split(r"</CURRENCY>", inside, maxsplit=1, flags=re.IGNORECASE)[0]
    symbol = _CURSYM.search(inside)
    if symbol is None:
        return None, None
    rate = _CURRATE.search(inside)
    return symbol.group(1).upper(), rate.group(1) if rate else None


def _leaves(block: str) -> dict[str, str]:
    """Tag -> value for one transaction, last occurrence winning.

    Aggregates (``<STMTTRN>``) match too and yield an empty value, which is
    harmless: nothing reads them.
    """
    found: dict[str, str] = {}
    for tag, value in _LEAF.findall(block):
        text = value.strip()
        if text:
            found[tag.upper()] = text
    return found


def read(raw: bytes) -> OfxStatement:
    """Every transaction in the file, in the order the bank listed them."""
    text = _decode(raw)
    warnings: list[str] = []

    start = _BODY.search(text)
    body = text[start.start() :] if start else text

    currency_match = _CURDEF.search(body)
    account_match = _ACCTID.search(body)
    statement_currency = currency_match.group(1).upper() if currency_match else None

    transactions: list[OfxTransaction] = []
    for block in _transaction_blocks(body):
        fields = _leaves(block)
        own_currency, own_rate = _currency_of(block)
        posted = parse_ofx_date(fields.get("DTPOSTED", ""))
        amount = _amount(fields.get("TRNAMT", ""))
        kind = (fields.get("TRNTYPE") or "").upper() or None
        # Everything the bank said, minus what already has a home of its own.
        # Kept rather than dropped: REFNUM and CHECKNUM are how you find a row
        # again in the bank's own systems, and TRNTYPE is the difference
        # between a fee and a purchase when the name is uninformative.
        carried = {
            key.lower(): value
            for key, value in fields.items()
            if key not in {"DTPOSTED", "TRNAMT", "NAME", "MEMO", "STMTTRN"}
        }
        transaction = OfxTransaction(
            fitid=fields.get("FITID"),
            posted=posted,
            amount=amount,
            name=fields.get("NAME"),
            memo=fields.get("MEMO"),
            kind=kind,
            raw=" ".join(block.split())[:500],
            details=carried,
            currency=own_currency or statement_currency,
            currency_rate=own_rate,
        )
        if posted is None:
            transaction.problem = f"no usable date in {fields.get('DTPOSTED', '')!r}"
        elif amount is None:
            transaction.problem = f"no usable amount in {fields.get('TRNAMT', '')!r}"
        elif kind in PENDING_TYPES:
            # A hold is money the bank has earmarked, not money that moved. It
            # settles later as a real transaction with its own FITID, so
            # importing it now would double the spend.
            transaction.problem = (
                f"the bank marked this {kind}, which is a pending hold rather than a "
                "transaction that has happened"
            )
        elif fields.get("CORRECTACTION", "").upper() == "DELETE":
            corrected = fields.get("CORRECTFITID", "")
            transaction.problem = (
                f"the bank sent this to cancel an earlier transaction ({corrected}), "
                "so there is nothing here to add"
            )
        transactions.append(transaction)
        if fields.get("CORRECTACTION", "").upper() == "REPLACE":
            warnings.append(
                f"transaction {fields.get('FITID')} replaces an earlier one "
                f"({fields.get('CORRECTFITID')}); the earlier one is still in the account "
                "and should be removed by hand"
            )

    if not transactions:
        warnings.append(
            "this looks like an OFX file but has no transactions in it -- some banks export an "
            "empty statement when the date range has nothing in it"
        )
    if currency_match is None:
        warnings.append("this file does not say which currency it is in")

    return OfxStatement(
        currency=statement_currency,
        account=account_match.group(1).strip() if account_match else None,
        transactions=transactions,
        warnings=warnings,
    )
