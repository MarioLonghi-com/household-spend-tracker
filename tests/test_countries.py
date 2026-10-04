"""The country an account is held in, and the flag it is shown as.

A country is a fact about the account, not about its currency: a euro account
can sit in any of twenty countries, and a Brazilian card can be billed in USD.
So it is stored separately, it is optional, and nothing is ever inferred from
the currency -- a guess would be wrong often enough to matter and would look
exactly like a stated fact once it was in the column.
"""

from __future__ import annotations

import pytest

from app import countries
from app.audit.batch import batch
from app.errors import ValidationError
from app.models import AccountType, BatchKind
from app.services import accounts as account_service

from .test_api import HEADERS, _setup_owner

# --------------------------------------------------------------------------- #
# The list itself
# --------------------------------------------------------------------------- #


def test_every_code_is_two_upper_case_letters_and_appears_once():
    codes = [code for code, _ in countries.COUNTRIES]
    assert len(codes) == len(set(codes)), "a duplicate code would give the picker two same rows"
    assert all(len(code) == 2 and code.isascii() and code.isupper() for code in codes)
    assert len(codes) > 200, "ISO 3166-1 has ~249 assigned codes; a short list means a truncation"


def test_no_two_countries_share_a_name():
    names = [name for _, name in countries.COUNTRIES]
    assert len(names) == len(set(names))


def test_a_flag_is_the_two_regional_indicators_for_the_code():
    """The flag is derived, not stored, so there is no table to fall behind."""
    assert countries.flag("ES") == "\U0001f1ea\U0001f1f8"
    assert countries.flag("BR") == "\U0001f1e7\U0001f1f7"
    assert countries.flag("GB") == "\U0001f1ec\U0001f1e7"


def test_an_account_with_no_country_still_gets_a_flag():
    """The column must never have a hole in it: an empty cell reads as a
    rendering fault, and a missing glyph is indistinguishable from a bug."""
    un = countries.flag(countries.UNKNOWN)
    assert countries.flag(None) == un
    assert countries.flag("") == un
    assert countries.flag("XX") == un, "not an assigned code"
    assert un == "\U0001f1fa\U0001f1f3"


def test_a_code_is_checked_against_the_list_and_normalised():
    assert countries.check("es") == "ES"
    assert countries.check(" br ") == "BR"
    assert countries.check(None) is None
    assert countries.check("   ") is None
    with pytest.raises(ValidationError):
        countries.check("XX")
    with pytest.raises(ValidationError):
        countries.check("ESP")  # alpha-3 is a different standard


# --------------------------------------------------------------------------- #
# On an account
# --------------------------------------------------------------------------- #


def test_an_account_records_the_country_it_is_held_in(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session,
            household=household,
            name="Santander",
            type=AccountType.checking,
            country="es",
        )
    assert account.country == "ES", "normalised on the way in, not on the way out"


def test_an_account_refuses_a_country_that_is_not_one(session, owner, household):
    with (
        pytest.raises(ValidationError),
        batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id),
    ):
        account_service.create_account(
            session,
            household=household,
            name="Nowhere",
            type=AccountType.checking,
            country="ZZ",
        )


def test_an_account_need_not_say(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session, household=household, name="Cash", type=AccountType.cash
        )
    assert account.country is None


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def test_the_picker_offers_exactly_what_the_server_will_take(client):
    """The list is sent rather than bundled so the two cannot drift. This is
    the assertion that makes that true rather than merely intended."""
    _setup_owner(client)
    offered = client.get("/api/countries").json()

    assert len(offered) == len(countries.COUNTRIES)
    for one in offered:
        assert countries.check(one["code"]) == one["code"]
        assert one["flag"] == countries.flag(one["code"])
        assert one["name"]


def test_a_country_round_trips_through_the_api(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Doe"}, headers=HEADERS).json()

    made = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "country": "ES"},
        headers=HEADERS,
    ).json()
    assert made["country"] == "ES"
    assert made["flag"] == "\U0001f1ea\U0001f1f8"

    listed = client.get(f"/api/households/{house['id']}/accounts").json()
    assert listed[0]["country"] == "ES"
    assert listed[0]["flag"] == "\U0001f1ea\U0001f1f8", "the listing carries it, not just the row"


def test_an_account_that_never_said_is_listed_under_the_un(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Doe"}, headers=HEADERS).json()
    made = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Cash", "type": "cash"},
        headers=HEADERS,
    ).json()

    assert made["country"] is None
    assert made["flag"] == "\U0001f1fa\U0001f1f3"


def test_the_country_can_be_changed_and_cleared(client):
    """Null on a PATCH means "leave it alone", so clearing needs its own word.
    Without `clear_country` a country set by mistake could never be taken back
    out -- only replaced with a different wrong one."""
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Doe"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "country": "ES"},
        headers=HEADERS,
    ).json()

    moved = client.patch(
        f"/api/accounts/{account['id']}", json={"country": "PT"}, headers=HEADERS
    ).json()
    assert moved["country"] == "PT"
    assert moved["flag"] == "\U0001f1f5\U0001f1f9"

    untouched = client.patch(
        f"/api/accounts/{account['id']}", json={"note": "still in Portugal"}, headers=HEADERS
    ).json()
    assert untouched["country"] == "PT", "a PATCH that does not mention it must not clear it"

    cleared = client.patch(
        f"/api/accounts/{account['id']}", json={"clear_country": True}, headers=HEADERS
    ).json()
    assert cleared["country"] is None
    assert cleared["flag"] == "\U0001f1fa\U0001f1f3"


def test_the_api_refuses_a_country_that_is_not_one(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Doe"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking"},
        headers=HEADERS,
    ).json()

    refused = client.patch(
        f"/api/accounts/{account['id']}", json={"country": "ZZ"}, headers=HEADERS
    )
    assert refused.status_code == 422, refused.text
    assert client.get(f"/api/accounts/{account['id']}").json()["country"] is None
