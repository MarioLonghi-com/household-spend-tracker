"""Which ledger row is this piece of evidence about? (#134, stories 1 and 3)

A receipt, a line on a corporate expenses portal, a row in somebody's
spreadsheet: each is a claim that money moved -- roughly this much, around
this day, to somebody called roughly this. This module turns one such claim
into a short ranked list of the rows it could describe, and says why each one
is on the list. It reads; it never writes.

What it matches on, in order of weight:

* **The amount, exactly, in minor units** -- but only against an account in
  the evidence's own currency. A USD receipt paid on a EUR card carries a
  figure the card never saw, and this ledger never converts, so for that
  account the amount is not compared at all.
* **The merchant's words**, against the row's payee, its memo and the bank's
  own description. Normalised the way the transfer matcher normalises
  (`transfers._plain_words`), with accents folded so "Café" finds "CAFE".
* **The date, within a window**, as a tiebreak and a fence rather than as
  evidence of its own.

What it deliberately does not do:

* **Convert.** A row in another currency is offered on date and merchant, and
  its reason says so in a fixed sentence an agent can pass on unchanged.
* **Exclude reconciled rows.** A portal expense can be months old and long
  since locked, and attaching evidence to a locked row changes no figure --
  the UI allows it for the same reason. `cleared` is reported instead.
* **Read `Receipt.extracted` as truth.** The receipt route reads it narrowly,
  as numbers and words to *search for*; what is found is the ledger's.
"""

from __future__ import annotations

import re
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import date as Date
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import money
from ..errors import ValidationError
from ..models import Account, Category, Receipt, Transaction
from .importing import MATCH_WINDOW_DAYS
from .receipts import elsewhere_in_household
from .transfers import _plain_words

#: One request answers at most this many pieces of evidence. A trip's receipts
#: or a month of portal lines fits; a whole year is the register again.
MAX_QUERIES = 50
#: The widest window a caller may ask for. Past a fortnight, "around this
#: day" stops meaning anything and an amount like 9.99 matches every month.
MAX_WINDOW_DAYS = 14
DEFAULT_LIMIT = 5
MAX_LIMIT = 20

#: The fixed sentence for a row in another currency. Fixed so an agent can
#: recognise it and a person reading the agent's summary can too.
DIFFERENT_CURRENCY = "different currency: matched on date and merchant, not amount"

#: Words that say nothing about who was paid. Short words (< 3 letters) and
#: bare numbers are dropped as well, which covers "SA", "DE" and card suffixes.
_GENERIC = frozenset(
    {
        "THE", "AND", "FOR", "WITH", "LTD", "INC", "LLC", "GMBH", "PLC", "SLU",
        "CARD", "TARJETA", "COMPRA", "PAGO", "PAYMENT", "PURCHASE", "POS",
        "CON", "DEL", "LOS", "LAS", "WWW", "COM",
    }
)

_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


# --------------------------------------------------------------------------- #
# The claim, and what came of it
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Evidence:
    """One piece of evidence, as the matcher reads it.

    ``amount_minor`` is a **magnitude**: a receipt total is unsigned and a
    portal line may be either, so direction is its own field and the sign of
    whatever the caller sent is not trusted to say it.
    """

    date: Date
    amount_minor: int | None
    currency: str | None = None
    window_days: int = MATCH_WINDOW_DAYS
    text: str | None = None
    account_id: str | None = None
    #: "out" (money left the account, the default), "in", or "any".
    direction: str = "out"


@dataclass(frozen=True, slots=True)
class Match:
    transaction: Transaction
    #: Signed: row date minus evidence date. A card settles after the day it
    #: was used, and "posted two days later" is a different fact from before.
    days_apart: int
    amount_matched: bool
    #: False only for a row in an account whose currency differs from the
    #: evidence's; True when the evidence named no currency.
    same_currency: bool
    words: tuple[str, ...]
    reason: str


# --------------------------------------------------------------------------- #
# Words
# --------------------------------------------------------------------------- #


def _folded(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def words_of(*texts: str | None) -> tuple[str, ...]:
    """The words that could name a merchant, upper-cased, accents folded, once each."""
    found: list[str] = []
    for text in texts:
        for word in _plain_words(_folded(text or "")):
            if len(word) < 3 or word.isdigit() or word in _GENERIC or word in found:
                continue
            found.append(word)
    return tuple(found)


def _same_word(ours: str, theirs: str) -> bool:
    """Equal, or one starts the other and the shorter is a real word.

    A bank glues things to a name -- "MERCADONA8123", "AMAZONMKTPLACE" -- and
    the evidence may carry the longer form of a name the bank shortened.
    """
    if ours == theirs:
        return True
    short, long_ = sorted((ours, theirs), key=len)
    return len(short) >= 4 and long_.startswith(short)


def overlap(ours: tuple[str, ...], theirs: tuple[str, ...]) -> tuple[str, ...]:
    """Which of the evidence's words the row also says, in the evidence's order."""
    return tuple(word for word in ours if any(_same_word(word, t) for t in theirs))


# --------------------------------------------------------------------------- #
# Reading money off the caller
# --------------------------------------------------------------------------- #


def currency_or_none(value: str | None) -> str | None:
    """An ISO code, upper-cased, or None for anything that is not three letters."""
    if isinstance(value, str) and _CURRENCY.match(value.strip()):
        return value.strip().upper()
    return None


def decimal_to_minor(value: str, currency: str) -> int:
    """A decimal STRING in major units, as minor units of ``currency``.

    Through `money` in both directions, as imports do, and refused rather than
    rounded when it carries more places than the currency has: a search for
    12.505 EUR that quietly became 12.51 would find a row nobody asked about.
    """
    try:
        given = Decimal(value.strip())
    except InvalidOperation:
        raise ValidationError(
            f'"{value}" is not a decimal amount. Send a string like "12.50", '
            "or amount_minor as an integer."
        ) from None
    try:
        minor = money.to_minor(given, currency)
    except money.MoneyError as exc:
        raise ValidationError(str(exc)) from None
    if money.to_decimal(minor, currency) != given:
        places = money.exponent(currency)
        raise ValidationError(
            f'"{value}" has more decimal places than {currency} uses ({places}). '
            "Send the amount exactly as it was charged."
        )
    return minor


# --------------------------------------------------------------------------- #
# The search
# --------------------------------------------------------------------------- #


def find(
    session: Session,
    household_id: str,
    evidence: Evidence,
    *,
    accounts: dict[str, Account],
    text_admits: bool = True,
    exclude: frozenset[str] | set[str] = frozenset(),
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[Match], str | None]:
    """Ranked rows this evidence could describe, and a note when there is one to make.

    ``accounts`` is the household's, keyed by id, read once by the caller for
    a whole batch of evidence -- and any ``account_id`` in the evidence must
    already have been checked against it (a stranger's id is a 404 upstream).

    ``text_admits`` says whether shared words alone may put a same-currency
    row on the list. `/match` says yes -- a portal line whose figure includes
    a tip still names the restaurant. Receipt `/candidates` says no, because
    there the merchant is the agent's own reading and may only break ties.
    A row in *another* currency is admitted on words either way: they are all
    there is to go on.

    Rank: an exact amount first, then more shared words, then the nearer
    date, then the row id so the order is stable.
    """
    pool = (
        [accounts[evidence.account_id]]
        if evidence.account_id
        else sorted(accounts.values(), key=lambda a: a.id)
    )
    currency = evidence.currency
    ours = words_of(evidence.text)

    same = [a for a in pool if currency is None or a.currency == currency]
    other = [a for a in pool if currency is not None and a.currency != currency]

    note = None
    if currency is not None and not same:
        note = (
            f"no account {'there' if evidence.account_id else 'here'} is in {currency}, "
            "so amounts could not be compared"
            + (" and, with no text given, nothing could be matched" if not ours else "")
            + "."
        )
    if not ours:
        other = []
    if evidence.amount_minor is None and not (text_admits and ours):
        same = []
    searched = [a.id for a in same + other]
    if not searched:
        return [], note

    window = timedelta(days=evidence.window_days)
    stmt = (
        select(Transaction)
        .options(
            selectinload(Transaction.payee),
            selectinload(Transaction.account),
            selectinload(Transaction.category).selectinload(Category.group),
        )
        .where(
            Transaction.household_id == household_id,
            Transaction.account_id.in_(searched),
            Transaction.date >= evidence.date - window,
            Transaction.date <= evidence.date + window,
        )
    )
    if evidence.direction == "out":
        stmt = stmt.where(Transaction.amount < 0)
    elif evidence.direction == "in":
        stmt = stmt.where(Transaction.amount > 0)
    if not ours and evidence.amount_minor is not None:
        # Nothing but the amount can admit a row, so let the index say which.
        stmt = stmt.where(
            Transaction.amount.in_([evidence.amount_minor, -evidence.amount_minor])
        )

    found: list[Match] = []
    for row in session.execute(stmt).scalars():
        if row.id in exclude:
            continue
        in_currency = currency is None or row.account.currency == currency
        shared = (
            overlap(ours, words_of(row.payee.name if row.payee else None, row.memo,
                                   row.import_payee_original))
            if ours
            else ()
        )
        exact = (
            in_currency
            and evidence.amount_minor is not None
            and abs(row.amount) == evidence.amount_minor
        )
        if in_currency and not exact and not (text_admits and shared):
            continue
        if not in_currency and not shared:
            continue
        apart = (row.date - evidence.date).days
        found.append(
            Match(
                transaction=row,
                days_apart=apart,
                amount_matched=exact,
                same_currency=in_currency,
                words=shared,
                reason=_reason(row, exact=exact, in_currency=in_currency,
                               shared=shared, apart=apart),
            )
        )

    found.sort(
        key=lambda m: (not m.amount_matched, -len(m.words), abs(m.days_apart), m.transaction.id)
    )
    return found[:limit], note


def _when(apart: int) -> str:
    if apart == 0:
        return "same day"
    if apart > 0:
        return f"posted {apart} day{'s' if apart > 1 else ''} later"
    return f"dated {-apart} day{'s' if apart < -1 else ''} earlier"


def _reason(row: Transaction, *, exact: bool, in_currency: bool, shared, apart: int) -> str:
    """One line per candidate. An agent passes it on; a person reads it."""
    words = f"merchant words {', '.join(shared)}" if shared else ""
    if not in_currency:
        return f"{DIFFERENT_CURRENCY} ({row.account.currency}; {words}; {_when(apart)})"
    if exact:
        return "same amount, " + _when(apart) + (f", {words}" if words else "")
    figure = money.format_amount(row.amount, row.account.currency, with_symbol=False)
    return f"amount differs ({figure} {row.account.currency} here), {words}, {_when(apart)}"


# --------------------------------------------------------------------------- #
# What goes with each row in the answer
# --------------------------------------------------------------------------- #


def receipt_counts(session: Session, ids: list[str]) -> dict[str, int]:
    """How many receipts each of these rows carries, in one grouped read."""
    if not ids:
        return {}
    return {
        tid: count
        for tid, count in session.execute(
            select(Receipt.transaction_id, func.count())
            .where(Receipt.transaction_id.in_(ids))
            .group_by(Receipt.transaction_id)
        ).all()
    }


def reimbursement_state(row: Transaction) -> str | None:
    """None, "expected", "settled" or "written_off" -- the Reimbursements report's words.

    Settled is not stored (it is ``reimbursed_by_id IS NOT NULL``), so it is
    worked out here rather than read.
    """
    if row.reimbursement is None:
        return None
    if row.reimbursed_by_id is not None:
        return "settled"
    return row.reimbursement.value


# --------------------------------------------------------------------------- #
# A receipt, read as evidence
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReceiptSearch:
    evidence: Evidence | None
    #: Which date was used: "given", "extracted", "captured" or "uploaded".
    date_from: str | None
    currency: str | None
    merchant: str | None
    #: Rows this receipt must not be offered for: the one it is on, and any
    #: already carrying the same bytes.
    exclude: frozenset[str]


def receipt_search(
    session: Session,
    receipt: Receipt,
    *,
    total_minor: int | None,
    given_date: Date | None,
    given_currency: str | None,
) -> ReceiptSearch:
    """What to search for on this receipt's behalf.

    Explicit parameters win. After them, the agent's own `extracted` claim is
    read **narrowly** -- `date` only as an ISO string, `currency` only as three
    letters, `merchant` only as a string -- the same way `total_minor` is: as
    something to search for. Anything else in there is ignored, not coerced.
    Then the camera's `captured_at`, then the upload date.
    """
    claim = receipt.extracted if isinstance(receipt.extracted, dict) else {}

    when, source = given_date, "given" if given_date else None
    if when is None:
        claimed = claim.get("date")
        if isinstance(claimed, str) and len(claimed) == 10:
            with suppress(ValueError):
                when, source = Date.fromisoformat(claimed), "extracted"
    if when is None and receipt.captured_at is not None:
        when, source = receipt.captured_at.date(), "captured"
    if when is None and receipt.created_at is not None:
        when, source = receipt.created_at.date(), "uploaded"

    currency = currency_or_none(given_currency) or currency_or_none(claim.get("currency"))
    merchant = claim.get("merchant")
    merchant = merchant[:200] if isinstance(merchant, str) and merchant.strip() else None

    exclude = {
        row.transaction_id
        for row in elsewhere_in_household(
            session, household_id=receipt.household_id, sha256=receipt.content_sha256
        )
    }
    if receipt.transaction_id:
        exclude.add(receipt.transaction_id)

    evidence = (
        Evidence(
            date=when,
            amount_minor=abs(total_minor) if total_minor is not None else None,
            currency=currency,
            text=merchant,
            direction="out",
        )
        if when is not None
        else None
    )
    return ReceiptSearch(evidence, source, currency, merchant, frozenset(exclude))
