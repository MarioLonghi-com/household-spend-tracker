"""A file whose every amount reads either way, and who breaks the tie (#259).

``1.500`` is fifteen hundred to a Spanish bank and one and a half to a British
one, and a statement of nothing but whole thousands never says which. It used
to fall back to "." silently, so a Spanish rent account imported a thousand
times too small with every row looking well-formed. Now the account's country
breaks the tie, and the preview says the separator was assumed rather than
read. These assert the staged minor-unit amounts and the sentence, through the
real upload route, in two accounts of two countries and two currencies.
"""

from __future__ import annotations

import pytest

from app import countries
from statements import DecimalPreference, parsing, sniffing
from tests.conftest import HEADERS, _setup_owner

#: A rent and a transfer, in whole thousands only.
WHOLE_THOUSANDS = (
    "Fecha;Concepto;Importe\n"
    "05/01/2026;EXAMPLE RENT;-1.500\n"
    "06/01/2026;EXAMPLE TRANSFER IN;2.000\n"
)

#: The same shape, but one amount has cents and so settles it.
SETTLED_BY_ONE_ROW = (
    "Fecha;Concepto;Importe\n"
    "05/01/2026;EXAMPLE RENT;-1.500\n"
    "06/01/2026;EXAMPLE FEE;-2.50\n"
)

ASSUMED = "the decimal separator could not be worked out from this file"


def _house(client) -> dict:
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Doe-Smith"}, headers=HEADERS).json()

    def account(name, currency, country=None):
        body = {"name": name, "type": "checking", "currency": currency}
        if country:
            body["country"] = country
        made = client.post(f"/api/households/{house['id']}/accounts", json=body, headers=HEADERS)
        assert made.status_code == 201, made.text
        return made.json()["id"]

    return {
        "house": house["id"],
        "ES": account("Example Rent EUR", "EUR", "ES"),
        "GB": account("Example Saver GBP", "GBP", "GB"),
        "none": account("Example Unplaced EUR", "EUR"),
    }


def _upload(client, world, key, content: str) -> dict:
    answer = client.post(
        f"/api/households/{world['house']}/imports",
        data={"account_id": world[key]},
        files={"file": ("statement.csv", content.encode(), "text/csv")},
        headers=HEADERS,
    )
    assert answer.status_code == 201, answer.text
    return answer.json()


def _amounts(preview: dict) -> list[int]:
    return [line["parsed"]["amount"] for line in preview["lines"]]


def _assumed(preview: dict) -> list[str]:
    return [w for w in preview["warnings"] if ASSUMED in w]


@pytest.mark.parametrize(
    ("key", "amounts", "separator", "because"),
    [
        ("ES", [-150_000, 200_000], ",", "because this account's country is set to Spain"),
        ("GB", [-150, 200], ".", "because this account's country is set to United Kingdom"),
        ("none", [-150, 200], ".", "because that is the default when nothing says otherwise"),
    ],
    ids=["es-eur-comma", "gb-gbp-dot", "no-country-default"],
)
def test_the_accounts_country_breaks_the_tie_and_the_preview_says_so(
    client, key, amounts, separator, because
):
    world = _house(client)
    preview = _upload(client, world, key, WHOLE_THOUSANDS)

    assert _amounts(preview) == amounts
    assert preview["detected"]["decimal_separator"] == separator
    [warning] = _assumed(preview)
    assert f'so "{separator}" was assumed, {because}' in warning
    assert 'an amount like "-1.500" reads either way' in warning


def test_a_file_that_settles_it_ignores_the_country_and_warns_nothing(client):
    """One ``-2.50`` proves "." in either account; Spain's comma is not consulted."""
    world = _house(client)
    for key in ("ES", "GB"):
        preview = _upload(client, world, key, SETTLED_BY_ONE_ROW)
        assert preview["detected"]["decimal_separator"] == "."
        assert _assumed(preview) == []
    # The GB account is the second upload of the same bytes into a different
    # account; minor units follow each account's currency, both with 2 places.
    assert _amounts(preview) == [-150, -250]


def test_a_reopened_import_still_says_the_separator_was_assumed(client):
    """The person reviewing a queued import tomorrow is the one who needs it.

    The upload response carried the sentence; the batch did not keep it, so
    reopening the preview from the queue said nothing.
    """
    world = _house(client)
    spain = _upload(client, world, "ES", WHOLE_THOUSANDS)
    britain = _upload(client, world, "GB", WHOLE_THOUSANDS)

    for staged, separator, because in (
        (spain, ",", "this account's country is set to Spain"),
        (britain, ".", "this account's country is set to United Kingdom"),
    ):
        reopened = client.get(f"/api/households/{world['house']}/imports/{staged['batch_id']}")
        assert reopened.status_code == 200, reopened.text
        body = reopened.json()
        [warning] = _assumed(body)
        assert f'so "{separator}" was assumed, because {because}' in warning
        assert _assumed(body) == _assumed(staged), "said once, not twice, and the same words"
        assert _amounts(body) == _amounts(staged)
    assert _amounts(spain) == [-150_000, 200_000]
    assert _amounts(britain) == [-150, 200]


#: Whole thousands in the amounts; a balance written with a point.
BALANCE_SAYS_POINT = (
    "Fecha;Concepto;Importe;Saldo\n"
    "05/01/2026;EXAMPLE RENT;-1.500;3250.4\n"
    "06/01/2026;EXAMPLE TRANSFER IN;2.000;5250.4\n"
)

OVERRULED = "was taken from the fee or balance column"


def test_a_balance_that_overrules_the_country_is_followed_and_said(client):
    """The evidence wins over the preference, but not silently.

    In the Spanish account the balance's "3250.4" settles "."; the rent then
    reads 1.50, which may be right or may be one odd balance cell. Either way
    the preview has to say the amounts did not decide it.
    """
    world = _house(client)
    spain = _upload(client, world, "ES", BALANCE_SAYS_POINT)
    assert spain["detected"]["decimal_separator"] == "."
    assert _amounts(spain) == [-150, 200]
    [warning] = [w for w in spain["warnings"] if OVERRULED in w]
    assert 'read either way (like "-1.500")' in warning
    assert 'differs from the "," expected because this account\'s country is set to Spain' in warning
    assert _assumed(spain) == []

    # The British account expected "." anyway: nothing to say.
    britain = _upload(client, world, "GB", BALANCE_SAYS_POINT)
    assert _amounts(britain) == [-150, 200]
    assert not any(OVERRULED in w or ASSUMED in w for w in britain["warnings"])


def test_the_same_file_in_two_accounts_stages_a_thousand_times_apart(client):
    world = _house(client)
    spain = _upload(client, world, "ES", WHOLE_THOUSANDS)
    britain = _upload(client, world, "GB", WHOLE_THOUSANDS)
    assert [a // b for a, b in zip(_amounts(spain), _amounts(britain), strict=True)] == [1000, 1000]


# --------------------------------------------------------------------------- #
# The library, which knows nothing about accounts
# --------------------------------------------------------------------------- #

SPAIN = DecimalPreference(",", "the caller said so")


def test_no_preference_falls_back_to_a_dot_and_says_so():
    sniffed = sniffing.sniff(WHOLE_THOUSANDS.encode())
    assert sniffed.format.decimal_separator == "."
    assert [w for w in sniffed.warnings if ASSUMED in w] == [
        "the decimal separator could not be worked out from this file — an amount like "
        '"-1.500" reads either way — so "." was assumed, because that is the default when '
        "nothing says otherwise. Check the amounts in the preview before importing"
    ]


def test_a_preference_is_used_only_when_nothing_settles_it():
    assert sniffing.guess_decimal_separator(["1.500", "-2.000"], SPAIN) == ","
    assert sniffing.guess_decimal_separator(["1.500", "-2.50"], SPAIN) == "."
    assert sniffing.decide_decimal_separator(["1.500", "-2.50"], SPAIN) == (".", None)
    sniffed = sniffing.sniff(SETTLED_BY_ONE_ROW.encode(), decimal_preference=SPAIN)
    assert sniffed.format.decimal_separator == "."
    assert not any(ASSUMED in w for w in sniffed.warnings)


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        (["1.500.000"], ","),
        (["-2.000.000"], ","),
        (["1,500,000"], "."),
        (["1.500", "1.500.000"], ","),
    ],
)
def test_a_separator_written_twice_is_the_thousands_one(samples, expected):
    """``1.500.000`` cannot have two decimal points; it settles the file."""
    assert sniffing.settle_decimal_separator(samples) == expected
    assert sniffing.decide_decimal_separator(samples) == (expected, None)


def test_millions_read_as_millions_whatever_the_preference():
    raw = b"Fecha;Concepto;Importe\n05/01/2026;EXAMPLE SALE;1.500.000\n"
    for preference in (None, DecimalPreference(".", "the caller said so")):
        sniffed, rows = parsing.read(raw, decimal_preference=preference)
        assert sniffed.format.decimal_separator == ","
        assert [str(row.amount) for row in rows] == ["1500000"]


def test_a_running_balance_with_cents_settles_whole_thousand_amounts():
    """A rent account that only moves round sums still states its balance."""
    raw = (
        b"Fecha;Concepto;Importe;Saldo\n"
        b"05/01/2026;EXAMPLE RENT;-1.500;3.250,40\n"
        b"06/01/2026;EXAMPLE TRANSFER IN;2.000;5.250,40\n"
    )
    sniffed, rows = parsing.read(
        raw, decimal_preference=DecimalPreference(".", "the caller said so")
    )
    assert sniffed.format.decimal_separator == ","
    assert not any(ASSUMED in w for w in sniffed.warnings)
    assert sum(OVERRULED in w for w in sniffed.warnings) == 1, "the caller expected a point"
    assert not any(OVERRULED in w for w in sniffing.sniff(raw).warnings), "no preference, nothing overruled"
    assert [str(row.amount) for row in rows] == ["-1500", "2000"]
    assert [str(row.balance) for row in rows] == ["3250.40", "5250.40"]


def test_amounts_with_no_separator_at_all_need_no_warning():
    """``1500`` reads the same with either separator, so there is nothing to assume."""
    raw = b"Fecha;Concepto;Importe\n05/01/2026;EXAMPLE RENT;-1500\n"
    assert not any(ASSUMED in w for w in sniffing.sniff(raw).warnings)


def test_a_preference_must_be_a_separator():
    with pytest.raises(ValueError, match="not a decimal separator"):
        DecimalPreference(";", "no")


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #


def test_every_country_in_the_table_is_a_real_code_with_one_of_two_separators():
    assert set(countries.DECIMAL_SEPARATORS) <= set(countries.BY_CODE)
    assert set(countries.DECIMAL_SEPARATORS.values()) == {",", "."}
    assert countries.decimal_separator("es") == ","
    assert countries.decimal_separator(" GB ") == "."
    assert countries.decimal_separator("CA") is None, "split: Quebec writes a comma"
    # Euro, but a point: the table is not "the euro area".
    assert (countries.decimal_separator("CY"), countries.decimal_separator("MT")) == (".", ".")
    assert {countries.decimal_separator(c) for c in ("AD", "MC", "SM", "RS")} == {","}
    assert countries.decimal_separator(None) is None
