"""Money handling.

Rule #1 of this codebase: **money is an integer of minor units, never a float.**
A EUR amount of 12.34 is stored as ``1234``. Sign carries direction: negative is
an outflow, positive an inflow. Minor units of the account's own currency
rather than a fixed precision for every amount: the ledger is multi-currency and
each currency knows its own exponent.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

from .errors import ValidationError

# ISO 4217 exponent for the currencies we care about; anything unlisted gets 2,
# which is right for the overwhelming majority.
_EXPONENTS: Final[dict[str, int]] = {
    "JPY": 0, "KRW": 0, "CLP": 0, "ISK": 0, "VND": 0, "HUF": 0, "TWD": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}

_SYMBOLS: Final[dict[str, str]] = {
    "EUR": "€", "USD": "$", "GBP": "£", "JPY": "¥", "CHF": "CHF", "SEK": "kr",
    "NOK": "kr", "DKK": "kr", "PLN": "zł", "CZK": "Kč", "CAD": "CA$",
    "AUD": "A$", "NZD": "NZ$", "BRL": "R$", "MXN": "MX$", "ARS": "AR$",
    "CLP": "CLP$", "COP": "COL$", "INR": "₹", "CNY": "¥", "KRW": "₩",
    "TRY": "₺", "ZAR": "R", "RUB": "₽", "ILS": "₪", "THB": "฿", "SGD": "S$",
    "HKD": "HK$", "AED": "د.إ", "SAR": "﷼",
}


class MoneyError(ValidationError, ValueError):
    """A value that is not money.

    Parented onto ``DomainError`` (via ``ValidationError``) so it answers 422
    rather than surfacing as a 500, which is what it did in the previous build.
    Still a ``ValueError`` so ordinary numeric handling keeps working.
    """

    """Raised when a value cannot be interpreted as money."""


def exponent(currency: str) -> int:
    """Number of decimal places this currency uses."""
    return _EXPONENTS.get(currency.upper(), 2)


def symbol(currency: str) -> str:
    return _SYMBOLS.get(currency.upper(), currency.upper())


def minor_factor(currency: str) -> int:
    return 10 ** exponent(currency)


#: The widest amount a minor-unit column can hold. `transactions.amount` is a
#: 64-bit integer, and every other money column matches it.
MAX_MINOR = 2**63 - 1
MIN_MINOR = -(2**63)


def to_minor(value: str | int | float | Decimal, currency: str) -> int:
    """Convert a human amount ("12.34", Decimal("12.34")) into minor units.

    Floats are accepted for convenience at the edge of the system (CSV import,
    JSON bodies) but are routed through ``Decimal(str(...))`` so 0.1 + 0.2 style
    error never reaches the ledger.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        dec = Decimal(value)
    elif isinstance(value, Decimal):
        dec = value
    else:
        try:
            dec = Decimal(str(value).strip())
        except (InvalidOperation, AttributeError) as exc:  # pragma: no cover - defensive
            raise MoneyError(
                f"not a monetary value: {value!r}",
                code="money.not_a_value",
                params={"value": str(value)},
            ) from exc
    if not dec.is_finite():
        raise MoneyError(
            f"not a monetary value: {value!r}",
            code="money.not_a_value",
            params={"value": str(value)},
        )
    try:
        scaled = dec * minor_factor(currency)
        minor = int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, OverflowError, ValueError) as exc:
        # An amount too large to represent is one bad row, not a broken file.
        # Raising MoneyError makes it a 422 that the importer can turn into a
        # rejected line, rather than a 500 that loses the whole statement.
        raise MoneyError(
            f"{value!r} is too large to record as money",
            code="money.too_large",
            params={"value": str(value)},
        ) from exc

    # Python integers are unbounded, so nothing above raises -- the failure
    # surfaced later, as an OverflowError out of the SQLite driver, which took
    # the whole import with it *before a single line was staged*. The column is
    # a 64-bit integer, so that is where the limit actually is; say so here,
    # where it is still one rejected row with a reason.
    if not MIN_MINOR <= minor <= MAX_MINOR:
        raise MoneyError(
            f"{value!r} is too large to record as money",
            code="money.too_large",
            params={"value": str(value)},
        )
    return minor


def magnitude_span(value: str, currency: str) -> tuple[int, int] | None:
    """The minor-unit magnitudes a typed amount means in one currency (#123).

    For the register's amount lookup, which searches every money column at
    once and so cannot know which currency the person had in mind. ``value``
    is an unsigned decimal with a ``.`` point -- the client normalises commas
    and signs away before it asks.

    - With decimals, it is one figure: ``"45.20"`` is ``(4520, 4520)`` in EUR.
      A figure the currency cannot hold (``"45.20"`` in JPY, ``"1.2345"`` in
      EUR) is ``None``, not rounded -- rounding would find rows that are not
      the amount typed.
    - Whole, it is the whole unit: ``"45"`` is ``(4500, 4599)`` in EUR, so a
      person who remembers "about forty-five euros" finds 45.20 without
      having to remember the cents. In JPY it is exactly ``(45, 45)``.

    ``None`` too for anything that is not a plain non-negative decimal.
    """
    text = (value or "").strip()
    if not text or not all(ch.isdigit() or ch == "." for ch in text) or text.count(".") > 1:
        return None
    try:
        dec = Decimal(text)
    except InvalidOperation:  # pragma: no cover - the characters are checked above
        return None
    factor = minor_factor(currency)
    scaled = dec * factor
    if scaled != scaled.to_integral_value():
        return None
    low = int(scaled)
    if not 0 <= low <= MAX_MINOR:
        return None
    if "." in text:
        return low, low
    return low, min(low + factor - 1, MAX_MINOR)


def parse_exact(value: str, currency: str) -> int:
    """A typed amount in minor units, or a sentence saying why it is not one (#146).

    For a file a person filled in by hand, where the one wrong answer is a
    quiet one. ``value`` is a signed plain decimal with a ``.`` point:
    ``"1234.56"``, ``"-80"``, ``"+0.5"``. Everything else is refused rather
    than guessed at:

    - More decimals than the currency has -- ``"12.345"`` in EUR, ``"12.5"``
      in JPY. :func:`to_minor` would round; here rounding would store a figure
      nobody typed.
    - Any thousands separator. ``"1,234"`` is a thousand and a bit in one
      locale and one and a bit in another, and there is no telling which from
      the text alone.

    Never a float, and not even a ``Decimal``: the digits are the answer.
    """
    text = (value or "").strip()
    body = text[1:] if text[:1] in "+-" else text
    whole, point, fraction = body.partition(".")
    if not whole.isdigit() or not whole.isascii() or (point and not (fraction.isdigit() and fraction.isascii())):
        raise MoneyError(
            f"{text!r} is not an amount this can read -- write it with a '.' before the "
            "decimals and no thousands separators, like 1234.56 or -80",
            code="money.not_an_amount",
            params={"value": text},
        )
    places = exponent(currency)
    if len(fraction) > places:
        if places == 0:
            raise MoneyError(
                f"{text!r} has decimals, and {currency.upper()} has none",
                code="money.decimals_in_whole_currency",
                params={"value": text, "currency": currency.upper()},
            )
        raise MoneyError(
            f"{text!r} has more decimals than {currency.upper()} has ({places})",
            code="money.too_many_decimals",
            params={"value": text, "currency": currency.upper(), "places": places},
        )
    # Digits, not arithmetic: the figure is the typed digits with the point
    # moved, which is exact at any length and never touches a float.
    digits = (whole + fraction.ljust(places, "0")).lstrip("0") or "0"
    # Counted before `int()` sees them: past 4,300 digits Python refuses the
    # conversion with a bare ValueError, which is a 500 rather than a row with
    # a reason. Anything longer than the column's widest value is too large
    # whatever the digits are, and the sentence does not repeat all of them.
    if len(digits) > len(str(MAX_MINOR)):
        raise MoneyError(
            f"an amount {len(whole)} digits long is too large to record as money",
            code="money.too_many_digits",
            params={"digits": len(whole)},
        )
    minor = int(digits)
    if text.startswith("-"):
        minor = -minor
    if not MIN_MINOR <= minor <= MAX_MINOR:
        raise MoneyError(
            f"{text!r} is too large to record as money",
            code="money.too_large",
            params={"value": text},
        )
    return minor


#: YNAB's API counts money in thousandths of the major unit, whatever the
#: currency: 12.34 GBP is ``12340``, 1,235 JPY is ``1235000``.
MILLIUNIT_EXPONENT: Final = 3


def from_milliunits(milliunits: int, currency: str) -> int:
    """Thousandths of the major unit as minor units of ``currency``, exactly (#266).

    Integer division by the gap between the two exponents, so never a float
    and never rounded: ``12340`` is ``1234`` in GBP, ``1235000`` is ``1235`` in
    JPY, and ``12345`` is ``12345`` in BHD. A figure the currency cannot hold --
    ``12345`` in GBP, ``1500`` in JPY -- is a :class:`MoneyError`, because the
    rounded figure is not the one recorded.
    """
    if isinstance(milliunits, bool) or not isinstance(milliunits, int):
        raise MoneyError(f"{milliunits!r} is not a whole number of thousandths")
    places = exponent(currency)
    if places > MILLIUNIT_EXPONENT:  # pragma: no cover - no ISO currency has more than three
        raise MoneyError(f"{currency.upper()} has more decimals than thousandths can carry")
    step = 10 ** (MILLIUNIT_EXPONENT - places)
    if milliunits % step:
        raise MoneyError(
            f"{milliunits} thousandths is not a whole number of {currency.upper()} minor units",
            code="money.not_whole_minor_units",
            params={"milliunits": milliunits, "currency": currency.upper()},
        )
    minor = milliunits // step
    if not MIN_MINOR <= minor <= MAX_MINOR:
        raise MoneyError(
            "that amount is too large to record as money", code="money.amount_too_large"
        )
    return minor


def to_decimal(minor: int, currency: str) -> Decimal:
    """Inverse of :func:`to_minor`, exact."""
    return (Decimal(minor) / minor_factor(currency)).quantize(
        Decimal(1).scaleb(-exponent(currency))
    )


def format_amount(minor: int, currency: str, *, with_symbol: bool = True) -> str:
    """Render minor units for humans. Deliberately plain — locale formatting is
    a presentation concern owned by the client."""
    dec = to_decimal(minor, currency)
    sign = "-" if dec < 0 else ""
    body = f"{abs(dec):,.{exponent(currency)}f}"
    if not with_symbol:
        return f"{sign}{body}"
    return f"{sign}{symbol(currency)}{body}"
