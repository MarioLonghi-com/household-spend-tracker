"""Reading an amount a person typed into a file, exactly or not at all (#146)."""

from __future__ import annotations

import pytest

from app.errors import DomainError
from app.money import MAX_MINOR, MoneyError, from_milliunits, parse_exact


@pytest.mark.parametrize(
    ("typed", "currency", "minor"),
    [
        ("1234.56", "EUR", 123456),
        ("1234.5", "EUR", 123450),
        ("1234", "EUR", 123400),
        ("-80", "GBP", -8000),
        ("+0.05", "GBP", 5),
        ("  12.30 ", "EUR", 1230),
        ("0", "EUR", 0),
        ("12000", "JPY", 12000),  # yen has no minor unit: the figure is the figure
        ("1.234", "KWD", 1234),  # three places where the currency has three
        ("92233720368547758.07", "EUR", MAX_MINOR),
        ("0" * 5000 + "12.30", "EUR", 1230),  # leading zeros are not size
    ],
)
def test_a_plain_decimal_is_read_in_the_currencys_own_units(typed, currency, minor):
    assert parse_exact(typed, currency) == minor
    assert isinstance(parse_exact(typed, currency), int)


@pytest.mark.parametrize(
    ("typed", "currency", "said"),
    [
        ("12.345", "EUR", r"more decimals than EUR has \(2\)"),
        ("12.5", "JPY", "JPY has none"),
        ("1,234.56", "EUR", "no thousands separators"),
        ("1.234,56", "EUR", "no thousands separators"),
        ("1 234", "EUR", "no thousands separators"),
        ("12,50", "EUR", "no thousands separators"),
        ("€12", "EUR", "not an amount"),
        ("1e3", "EUR", "not an amount"),
        (".5", "EUR", "not an amount"),
        ("5.", "EUR", "not an amount"),
        ("--5", "EUR", "not an amount"),
        ("", "EUR", "not an amount"),
        ("١٢", "EUR", "not an amount"),  # digits, but not ones anybody means here
        ("92233720368547758.08", "EUR", "too large"),
        ("9" * 5000, "EUR", "an amount 5000 digits long is too large"),  # past int()'s own limit
        ("1" + "0" * 17, "EUR", "too large"),  # one digit past the column
    ],
)
def test_anything_else_is_refused_rather_than_rounded_or_guessed(typed, currency, said):
    with pytest.raises(MoneyError, match=said) as refused:
        parse_exact(typed, currency)
    # A domain error, so a row of a file can say it rather than the file 500ing.
    assert isinstance(refused.value, DomainError)


@pytest.mark.parametrize(
    ("milliunits", "currency", "minor"),
    [
        (12340, "GBP", 1234),
        (-12340, "EUR", -1234),
        (0, "EUR", 0),
        (1235000, "JPY", 1235),  # a yen is a thousand thousandths, not ten
        (-6784000, "JPY", -6784),
        (12345, "BHD", 12345),  # three places: the figure is the figure
        (MAX_MINOR * 10, "EUR", MAX_MINOR),
    ],
)
def test_milliunits_become_the_currencys_own_minor_units(milliunits, currency, minor):
    assert from_milliunits(milliunits, currency) == minor
    assert type(from_milliunits(milliunits, currency)) is int


@pytest.mark.parametrize(
    ("milliunits", "currency", "said"),
    [
        (12345, "GBP", "not a whole number of GBP minor units"),
        (1500, "JPY", "not a whole number of JPY minor units"),
        ((MAX_MINOR + 1) * 10, "EUR", "too large"),
        (12.5, "EUR", "not a whole number of thousandths"),
        (True, "EUR", "not a whole number of thousandths"),
    ],
)
def test_milliunits_that_do_not_convert_exactly_are_refused_not_rounded(milliunits, currency, said):
    with pytest.raises(MoneyError, match=said):
        from_milliunits(milliunits, currency)
