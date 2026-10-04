"""The manifest: one call in place of a dozen exploratory ones.

What is asserted here is mostly *content*, because that is what the endpoint is
for. A manifest that returns 200 and describes the wrong household, or promises
an endpoint nobody built, is worse than no manifest at all -- an agent acts on
it.
"""

from __future__ import annotations

import pytest

from app.api.routers import agent as agent_router
from app.audit.batch import batch
from app.models import AgentScope, BatchKind
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner

MANIFEST = f"/api/agent/v{agent_router.API_VERSION}/manifest"


@pytest.fixture()
def keyed(client):
    """A real instance, a real household, and a key that reaches it.

    Goes through the HTTP surface rather than the ORM fixtures, because the
    manifest is a route and the thing being tested is what a program holding a
    token actually receives.
    """
    world = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS)
    assert house.status_code == 201, house.text
    household = house.json()

    made = client.post(
        f"/api/households/{household['id']}/accounts",
        json={"name": "Santander current", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    yen = client.post(
        f"/api/households/{household['id']}/accounts",
        json={"name": "Tokyo cash", "type": "cash", "currency": "JPY"},
        headers=HEADERS,
    )
    assert yen.status_code == 201, yen.text

    # A second household the same person owns, with an account of its own. It
    # exists so the manifest has something it must NOT show: a key is narrower
    # than the person holding it, and proving that needs a household the owner
    # can reach and the key cannot.
    elsewhere = client.post("/api/households", json={"name": "Elsewhere"}, headers=HEADERS)
    assert elsewhere.status_code == 201, elsewhere.text
    theirs = client.post(
        f"/api/households/{elsewhere.json()['id']}/accounts",
        json={"name": "Not yours", "type": "checking", "currency": "GBP"},
        headers=HEADERS,
    )
    assert theirs.status_code == 201, theirs.text

    # The key itself is minted through the service, since the profile screen's
    # route is the next slice. What it produces is identical either way.
    from sqlalchemy.orm import Session

    from app.models import Household, User

    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, world["user"]["id"])
        house_row = own.get(Household, household["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=house_row.id):
            _row, token = key_service.issue(
                own, user=user, household=house_row, label="the analyst",
                agent_name="Claude Desktop",
            )
        own.commit()

    client.cookies.clear()
    return {
        "token": token,
        "household": household,
        "accounts": [made.json(), yen.json()],
        "elsewhere": elsewhere.json(),
    }


def _get(client, token: str):
    return client.get(MANIFEST, headers={"authorization": f"Bearer {token}"})


# --------------------------------------------------------------------------- #
# The door, through a real route at last
# --------------------------------------------------------------------------- #


def test_a_key_with_no_cookie_and_no_origin_is_served(client, keyed):
    """Which is every non-browser client there is."""
    answer = _get(client, keyed["token"])
    assert answer.status_code == 200, answer.text


def test_every_refusal_about_a_key_is_the_same_sentence(client, keyed):
    """Unknown and malformed must stay indistinguishable.

    Absent used to be on this list and no longer is: since #38 a request that
    presented nothing gets told what to send, which is a fact about the request
    rather than about any key. `test_agent_discovery.py` owns that split and
    the rest of the property; this keeps the manifest's own door honest.
    """
    invented = _get(client, "stk_invented")
    malformed = client.get(MANIFEST, headers={"authorization": "Bearer not-even-close"})

    assert invented.status_code == malformed.status_code == 401
    assert invented.json()["detail"] == malformed.json()["detail"] == "that key is not valid"


# --------------------------------------------------------------------------- #
# What it says
# --------------------------------------------------------------------------- #


def test_it_describes_this_household_and_this_key(client, keyed):
    body = _get(client, keyed["token"]).json()

    assert body["household"]["id"] == keyed["household"]["id"]
    assert body["household"]["name"] == "Home"
    assert body["key"]["label"] == "the analyst"
    assert body["key"]["agent_name"] == "Claude Desktop"
    assert body["key"]["may_commit"] is False


def test_a_read_key_is_shown_read_and_a_write_key_both(client, keyed):
    """The implication is computed, not stored. This is where an agent reads it."""
    body = _get(client, keyed["token"]).json()
    assert body["key"]["scopes"] == ["read"]
    assert AgentScope.write.granted == ["read", "write"]


def test_every_account_carries_its_own_currency_and_exponent(client, keyed):
    """So an agent never guesses an exponent for a currency it has not met.

    The two-currency fixture earning its keep: JPY has no minor unit, and an
    agent that assumed two decimals everywhere would be wrong by a factor of a
    hundred on every yen amount.
    """
    body = _get(client, keyed["token"]).json()
    by_currency = {a["currency"]: a for a in body["accounts"]}

    assert by_currency["EUR"]["minor_exponent"] == 2
    assert by_currency["JPY"]["minor_exponent"] == 0
    assert by_currency["EUR"]["name"] == "Santander current"


def test_categories_are_given_by_the_name_a_model_should_use(client, keyed):
    body = _get(client, keyed["token"]).json()
    assert body["categories"], "a fresh household is seeded with a tree"
    assert all(": " in c["full_name"] for c in body["categories"]), (
        "the full name is what reads outside the picker, and what a model should write"
    )


def test_the_conventions_state_the_three_rules_a_model_breaks(client, keyed):
    """This block is the reason the endpoint pays for itself."""
    conventions = _get(client, keyed["token"]).json()["conventions"]

    assert "minor units" in conventions["money"]
    assert "Negative is money leaving" in conventions["money"]
    assert "never converts" in conventions["currency"]

    # `amount_decimal` was deliberately ABSENT until a write existed to
    # describe -- publishing it earlier would have been the same fault as
    # publishing an unenforced rate limit. Slice 2 built the write, so it
    # appears, and this now asserts it says what the schema actually does.
    assert "REFUSED" in conventions["amount_decimal"], "the float rule, stated plainly"
    assert "decimal" in conventions["amount_decimal"].lower()
    assert "Idempotency-Key" in conventions["idempotency"]


def test_it_shows_nothing_from_another_household(client, keyed):
    """A key is narrower than the person holding it.

    The owner of this key can reach "Elsewhere" perfectly well with their
    cookie. The key cannot, and the manifest must not mention that it is there
    -- not the household, not its account, not its currency.
    """
    body = _get(client, keyed["token"]).json()

    assert body["household"]["name"] == "Home"
    assert body["household"]["id"] != keyed["elsewhere"]["id"]
    assert {a["name"] for a in body["accounts"]} == {"Santander current", "Tokyo cash"}
    assert "GBP" not in {a["currency"] for a in body["accounts"]}
    # And nothing anywhere in the document names it, however it got there.
    assert keyed["elsewhere"]["id"] not in _get(client, keyed["token"]).text


# --------------------------------------------------------------------------- #
# Honesty about itself
# --------------------------------------------------------------------------- #


def test_every_endpoint_it_promises_actually_exists(client, keyed):
    """An endpoint listed here that was never built is a lie an agent acts on.

    So the list grows with the slices, and this walks the real route table to
    prove it has not run ahead of them.
    """
    from tests.test_agent_access import _api_routes

    app = client.app_module.app
    real = {
        (route.served_path, method)
        for route in _api_routes(app)
        for method in (route.methods or ())
    }
    assert (MANIFEST, "GET") in real, (
        "the route walk is not reporting served paths, so the check below is vacuous"
    )

    body = _get(client, keyed["token"]).json()
    assert body["endpoints"], "an empty manifest is not a manifest"
    missing = [
        f"{e['method']} {e['path']}"
        for e in body["endpoints"]
        if (e["path"], e["method"]) not in real
    ]
    assert not missing, f"the manifest promises endpoints that do not exist: {missing}"


def test_the_rate_limit_it_publishes_is_the_one_that_is_enforced(client, keyed):
    """It was absent until the limiter existed, which was the point.

    `A retention window in a docstring is not a retention window` -- so a
    published ceiling has to be the same number the code refuses at, read from
    the same constant rather than typed twice.
    """
    from app.services import agent_requests

    limit = _get(client, keyed["token"]).json()["conventions"]["rate_limit"]
    assert limit["requests_per_hour"] == agent_requests.PER_HOUR
    assert "Retry-After" in limit["on_refusal"]


def test_the_version_is_in_the_url_as_well_as_the_body(client, keyed):
    body = _get(client, keyed["token"]).json()
    assert body["api_version"] == str(agent_router.API_VERSION)
    assert f"/v{agent_router.API_VERSION}/" in MANIFEST
    assert body["app_version"]


# --------------------------------------------------------------------------- #
# The schema, to key holders only
# --------------------------------------------------------------------------- #


def test_the_schema_is_served_to_a_key_and_to_nobody_else(client, keyed):
    """The anonymous inventory stays shut, which is the property that mattered.

    Implemented as a route rather than by flipping `openapi_url`, so this is
    two different answers to the same URL rather than one switch.
    """
    anonymous = client.get("/api/openapi.json")
    assert anonymous.status_code == 401

    keyed_answer = client.get(
        "/api/openapi.json", headers={"authorization": f"Bearer {keyed['token']}"}
    )
    assert keyed_answer.status_code == 200, keyed_answer.text
    assert "paths" in keyed_answer.json()
