"""The one page served to somebody holding nothing.

Two properties, and they pull against each other, which is why both are
asserted here rather than assumed:

*It has to be useful* -- an agent handed a bare URL should learn what this is,
that it needs a key, and how a person gives it one.

*It must not describe this instance* -- no household, no account, no id, and
above all no route inventory. `/api/openapi.json` stays shut to the same
caller, and the difference between the two is the whole argument in
`app/api/discovery.py`.
"""

from __future__ import annotations

import pytest

from tests.conftest import HEADERS, _setup_owner


@pytest.fixture()
def page(client) -> str:
    return client.get("/llms.txt").text


# --------------------------------------------------------------------------- #
# Served to anybody
# --------------------------------------------------------------------------- #


def test_it_is_served_without_any_credential_at_all(client):
    answer = client.get("/llms.txt")
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/plain")


def test_it_is_served_before_the_instance_is_even_set_up(client):
    """`gate_until_configured` answers 503 to everything under /api.

    This lives outside /api precisely so a fresh instance can still say what it
    is -- the moment somebody is most likely to be pointing a tool at it.
    """
    assert client.get("/api/agent/v1/manifest").status_code == 503
    assert client.get("/llms.txt").status_code == 200


def test_the_well_known_spelling_is_the_same_page(client):
    assert client.get("/.well-known/llms.txt").text == client.get("/llms.txt").text


# --------------------------------------------------------------------------- #
# Useful
# --------------------------------------------------------------------------- #


def test_it_says_what_a_key_looks_like_and_who_can_issue_one(page):
    assert "stk_" in page
    assert "Keys for programs" in page, "the words on the actual screen"
    assert "cannot issue one for yourself" in page


def test_it_states_the_rules_a_model_breaks(page):
    """The same three the manifest carries, for a reader who has no key yet."""
    assert "minor units" in page
    assert "Negative is money leaving" in page
    assert "never converts" in page


def test_the_numbers_in_it_are_the_ones_that_are_enforced(client, page):
    """Rendered from the constants, never typed twice.

    A published ceiling that nobody checks is a docstring pretending to be a
    promise -- which is the rule `CLAUDE.md` states about retention windows.
    """
    from app.services import agent_requests

    assert str(agent_requests.PER_HOUR) in page
    assert client.app_module.__version__ in page


def test_it_names_the_floor_a_key_can_never_cross(page):
    for promise in ("delete anything", "undo anything", "revoke a key"):
        assert promise in page


def test_it_names_where_the_source_is(page):
    """#111: the one line a program needs for the AGPL's source offer."""
    from app.services.platform import REPOSITORY

    assert f"Source: {REPOSITORY} (AGPL-3.0-or-later)." in page.splitlines()
    assert REPOSITORY == "https://github.com/MarioLonghi-com/household-spend-tracker"


def test_the_urls_in_it_point_at_this_instance(client, page):
    assert "/api/agent/v1/manifest" in page
    assert "testserver" in page, "built from the request's own origin"


# --------------------------------------------------------------------------- #
# And says nothing about this instance
# --------------------------------------------------------------------------- #


def test_it_names_no_household_no_account_and_no_id(client):
    """The property that makes serving it anonymously a different decision
    from serving the schema anonymously."""
    world = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()

    client.cookies.clear()
    page = client.get("/llms.txt").text

    for secret in (house["id"], house["name"], account["id"], account["name"],
                   world["user"]["email"], world["user"]["display_name"]):
        assert secret not in page, f"{secret!r} leaked into the anonymous page"


def test_it_is_not_a_route_inventory(client):
    """It names the handful of routes it teaches, and no more.

    The test that matters: a route nobody chose to document must not appear
    just because it exists. A generated document could not pass this.
    """
    import re

    # Asked the other way round on purpose: not "which routes appear?" -- `/`
    # appears in any prose -- but "which API paths does this page name?". The
    # answer has to be a short, chosen list, and a generated document could not
    # keep it short.
    page = client.get("/llms.txt").text
    named = set(re.findall(r"/api/[A-Za-z0-9/_.{}-]+", page))

    assert named == {
        "/api/agent/v1/manifest",
        "/api/openapi.json",
    }, f"the anonymous page is naming API paths it was not written to teach: {named}"

    # And the handful it does name really exist, so it is not teaching fiction.
    from tests.test_agent_access import _api_routes

    real = {route.served_path for route in _api_routes(client.app_module.app)}
    assert named <= real, f"the page names paths that do not exist: {named - real}"


def test_the_schema_is_still_shut_to_the_same_caller(client):
    _setup_owner(client)
    client.cookies.clear()
    assert client.get("/llms.txt").status_code == 200
    assert client.get("/api/openapi.json").status_code == 401


# --------------------------------------------------------------------------- #
# The refusal that points at it
# --------------------------------------------------------------------------- #


def test_a_refused_agent_request_says_which_credential_it_wanted(client):
    _setup_owner(client)
    client.cookies.clear()

    refused = client.get("/api/agent/v1/manifest")
    assert refused.status_code == 401
    challenge = refused.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert "/llms.txt" in challenge, "a request that arrives wrong should teach"


def test_every_way_a_key_can_fail_gives_a_byte_identical_refusal(client):
    """The pointer must not become a place to say which check failed.

    `agent_keys.lookup` answers None for absent, malformed, unknown, revoked,
    expired and owner-disabled alike, and this is what stops a helpful sentence
    quietly undoing that.
    """
    _setup_owner(client)
    client.cookies.clear()

    answers = [
        client.get("/api/agent/v1/manifest", headers={"authorization": "Bearer stk_nope"}),
        client.get("/api/agent/v1/manifest", headers={"authorization": "Bearer other"}),
        client.get("/api/agent/v1/manifest", headers={"authorization": "nonsense"}),
    ]
    assert {a.status_code for a in answers} == {401}
    assert len({a.text for a in answers}) == 1, "the body must not say which check failed"
    assert len({a.headers["WWW-Authenticate"] for a in answers}) == 1

    # Sending nothing is not one of the ways a *key* can fail, so since #38 it
    # gets a sentence naming the header to use. It still has to leak nothing:
    # same status, same pointer, and no claim about any credential.
    nothing = client.get("/api/agent/v1/manifest")
    assert nothing.status_code == 401
    assert nothing.headers["WWW-Authenticate"] == answers[0].headers["WWW-Authenticate"]
    assert nothing.text != answers[0].text, "it should say what to send"
