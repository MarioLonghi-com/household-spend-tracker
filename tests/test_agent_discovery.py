"""What a program can find out before, during and after it gets a key.

Five issues from the 2026-09-22 import run, and they are one subject: a program
pointed at this app could not learn what it was pointed at. Every guessed path
answered 200 with a web page (#36), the manifest never said how to authenticate
(#37), a valid key in the wrong header was told the key was bad (#38), the
discovery page nobody found had no machine-readable sibling (#39), and no
endpoint declared the shape of its own answer, so a wrong key read as an empty
result rather than as a mistake (#42).

The assertions here are about *content*, because content is the whole point. A
descriptor that returns 200 and names the wrong path is worse than none: an
agent acts on it.
"""

from __future__ import annotations

import json

import pytest
from fastapi import Response

from app.api import discovery
from app.api.routers import agent as agent_router
from app.audit.batch import batch
from app.models import BatchKind
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner

BASE = f"/api/agent/v{agent_router.API_VERSION}"
MANIFEST = f"{BASE}/manifest"


@pytest.fixture()
def keyed(client):
    """One household, one account, and a key that reaches them."""
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
    return {"token": token, "household": household}


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# #36 -- a path that does not exist says so
# --------------------------------------------------------------------------- #


def test_an_unmatched_api_path_is_a_json_404_not_the_app_shell(spa_built, keyed, client):
    """The worst possible answer to a probe is "yes".

    Every guess under `/api/` used to succeed with 200 text/html, so no status
    code carried information and a real endpoint was distinguishable from an
    invented one only by sniffing Content-Type on every call.
    """
    answer = client.get("/api/transactions")  # plausible, and not a route

    assert answer.status_code == 404, answer.text
    assert answer.headers["content-type"].startswith("application/json"), (
        "it is still answering with the app shell"
    )
    detail = answer.json()["detail"]
    assert BASE in detail, f"the refusal should point at the agent API: {detail!r}"
    assert "/llms.txt" in detail, f"and at the discovery page: {detail!r}"


def test_the_refusal_covers_the_paths_the_agents_actually_guessed(spa_built, keyed, client):
    """Every one of these returned a web page during the run."""
    for path in ("/api/accounts", "/api/v1/transactions", "/api/docs", "/api/"):
        answer = client.get(path)
        assert answer.status_code == 404, f"{path} answered {answer.status_code}"
        assert answer.headers["content-type"].startswith("application/json"), path


def test_a_real_api_route_is_untouched(spa_built, keyed, client):
    """The exclusion is about paths that match nothing, not about `/api/`."""
    answer = client.get("/api/health")
    assert answer.status_code == 200, answer.text
    assert answer.json()["status"] == "ok"


def test_client_side_routing_still_falls_through_to_the_shell(spa_built, keyed, client):
    """The catch-all's actual job, which this must not have broken."""
    answer = client.get("/register")

    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"].startswith("text/html")


# --------------------------------------------------------------------------- #
# #38 -- a valid key in the wrong header is not a bad key
# --------------------------------------------------------------------------- #


def test_a_valid_key_in_the_wrong_header_is_told_which_header(client, keyed):
    """It cost a full diagnostic round trip mid-import.

    The message asserted the wrong cause, so the natural next step was to go
    hunting for a bad or expired key rather than to try a different header.
    """
    answer = client.get(MANIFEST, headers={"X-API-Key": keyed["token"]})

    assert answer.status_code == 401, answer.text
    detail = answer.json()["detail"]
    assert "X-API-Key" in detail, f"it should name the header they used: {detail!r}"
    assert "Authorization" in detail and "Bearer" in detail, (
        f"and the one this API reads: {detail!r}"
    )
    assert detail != "that key is not valid", "the key was valid; only the header was wrong"


def test_no_credential_at_all_says_what_to_send(keyed, client):
    answer = client.get(MANIFEST)

    assert answer.status_code == 401, answer.text
    detail = answer.json()["detail"]
    assert "Authorization" in detail and "Bearer" in detail, detail
    assert detail != "that key is not valid", "nothing was presented, so nothing was invalid"


def test_the_uniform_refusal_survives_for_every_case_about_a_key(client, keyed):
    """The property #38 was not allowed to give up.

    Unknown, malformed and revoked must stay indistinguishable: `agent_keys.
    lookup` returns None for all of them so a caller cannot probe which. The
    two sentences added above are about the *request*, not about a key, and
    they are reached before any lookup happens.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models import AgentKey, User

    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        row = own.execute(select(AgentKey)).scalars().one()
        user = own.get(User, row.user_id)
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=row.household_id):
            key_service.revoke(own, key=row, by=user)
        own.commit()

    unknown = client.get(MANIFEST, headers=_bearer("stk_invented"))
    malformed = client.get(MANIFEST, headers={"authorization": "Bearer not-even-close"})
    revoked = client.get(MANIFEST, headers=_bearer(keyed["token"]))

    said = {r.json()["detail"] for r in (unknown, malformed, revoked)}
    assert said == {"that key is not valid"}, (
        f"these must stay indistinguishable, and they said {said}"
    )
    assert {r.status_code for r in (unknown, malformed, revoked)} == {401}


def test_a_key_in_the_right_header_still_works(client, keyed):
    """The control. Everything above is only interesting if this passes."""
    assert client.get(MANIFEST, headers=_bearer(keyed["token"])).status_code == 200


# --------------------------------------------------------------------------- #
# #39 -- a discovery document that can be discovered
# --------------------------------------------------------------------------- #


def test_the_well_known_descriptor_is_served_anonymously(keyed, client):
    answer = client.get(discovery.WELL_KNOWN)

    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"].startswith("application/json")
    body = answer.json()
    assert body["manifest"].endswith(MANIFEST)
    assert body["openapi"].endswith("/api/openapi.json")
    assert body["documentation"].endswith("/llms.txt")
    assert body["auth"]["header"] == "Authorization"
    assert body["auth"]["scheme"] == "Bearer"
    assert body["auth"]["token_prefix"] == key_service.PREFIX


def test_the_descriptor_describes_the_software_and_not_this_deployment(client, keyed):
    """The same line `document` draws, and for the same reason.

    It is served to a caller holding nothing, so a household name, an account
    or an id in it would be a leak -- and the route inventory stays with the
    manifest, which needs a key.
    """
    body = client.get(discovery.WELL_KNOWN).text

    assert "Home" not in body, "it named the household"
    assert keyed["household"]["id"] not in body, "it named a real id"
    assert "endpoints" not in json.loads(body), "the route inventory belongs to the manifest"


def test_the_published_rate_limit_is_the_enforced_one(keyed, client):
    """A number nobody checks is a docstring pretending to be a promise."""
    from app.services import agent_requests

    body = client.get(discovery.WELL_KNOWN).json()
    assert body["rate_limit"]["requests_per_hour"] == agent_requests.PER_HOUR


def test_the_prose_page_points_at_its_json_sibling(keyed, client):
    """Whichever of the two a caller finds, it leads to the other."""
    assert discovery.WELL_KNOWN in client.get("/llms.txt").text


# --------------------------------------------------------------------------- #
# #37 -- the manifest says how to make a call
# --------------------------------------------------------------------------- #


def test_the_manifest_says_how_to_authenticate(client, keyed):
    """It described everything except the one thing needed to ask at all."""
    body = client.get(MANIFEST, headers=_bearer(keyed["token"])).json()

    assert body["auth"]["header"] == "Authorization"
    assert body["auth"]["scheme"] == "Bearer"
    assert body["auth"]["example"].startswith(f"Authorization: Bearer {key_service.PREFIX}")


def test_the_manifest_names_the_spec_and_the_discovery_document(client, keyed):
    """Both were found by guessing, which is the thing it exists to prevent."""
    body = client.get(MANIFEST, headers=_bearer(keyed["token"])).json()

    assert body["openapi"] == "/api/openapi.json"
    assert body["discovery"] == discovery.WELL_KNOWN


def test_the_openapi_path_the_manifest_names_is_reachable_with_this_key(client, keyed):
    """A named path that refuses the caller it was named to is worse than none."""
    body = client.get(MANIFEST, headers=_bearer(keyed["token"])).json()

    answer = client.get(body["openapi"], headers=_bearer(keyed["token"]))
    assert answer.status_code == 200, answer.text
    assert "openapi" in answer.json()


# --------------------------------------------------------------------------- #
# #42 -- every endpoint declares the shape of its own answer
# --------------------------------------------------------------------------- #


def _declared(returns: str) -> set[str]:
    """The top-level keys out of a `returns` string, e.g. `{a, b[], c}`."""
    inner = returns.strip().removeprefix("[").removesuffix("]").strip()
    inner = inner.removeprefix("{").removesuffix("}")
    return {name.strip().removesuffix("[]") for name in inner.split(",") if name.strip()}


def _fields(model) -> set[str]:
    """The top-level field names of a response model, list-wrapped or not."""
    import typing

    if typing.get_origin(model) is list:
        model = typing.get_args(model)[0]
    return set(model.model_fields)


def test_every_endpoint_declares_what_it_returns(client, keyed):
    body = client.get(MANIFEST, headers=_bearer(keyed["token"])).json()

    assert body["endpoints"], "an empty manifest is not a manifest"
    silent = [e["path"] for e in body["endpoints"] if not e.get("returns")]
    assert not silent, f"these say nothing about their answer: {silent}"


def test_the_declared_shape_is_the_one_the_route_actually_returns(client, keyed):
    """The drift guard.

    A `returns` typed by hand is a docstring; one checked against the route's
    own `response_model` is a promise. This is the same discipline that makes
    `test_every_endpoint_it_promises_actually_exists` worth having.
    """
    from tests.test_agent_access import _api_routes

    app = client.app_module.app
    routes = {
        (route.served_path, method): route
        for route in _api_routes(app)
        for method in (route.methods or ())
    }
    assert (MANIFEST, "GET") in routes, "the route walk found nothing, so this is vacuous"

    wrong: list[str] = []
    for entry in agent_router.ENDPOINTS:
        route = routes[(entry.path, entry.method)]
        if entry.returns == agent_router.RETURNS_BYTES:
            # A file, not a JSON shape (#44): held to answering with raw bytes
            # and to saying so in its OpenAPI, which is where a client looks.
            assert route.response_model is None, entry.path
            assert route.response_class is Response, entry.path
            assert route.responses[200]["content"], entry.path
            continue
        model = route.response_model
        assert model is not None, f"{entry.path} declares no response_model"

        # Less what the route leaves out of the answer: a field the screen is
        # sent and an agent is not (#267) is not part of the agent's shape.
        left_out = {name for name, whole in (route.response_model_exclude or {}).items() if whole is True}
        actual, declared = _fields(model) - left_out, _declared(entry.returns)
        if actual != declared:
            wrong.append(
                f"{entry.method} {entry.path}: says {sorted(declared)}, "
                f"returns {sorted(actual)}"
            )

    assert not wrong, "the manifest describes shapes the routes do not produce:\n" + "\n".join(wrong)


def test_a_list_answer_is_declared_as_a_list(client, keyed):
    """`[{...}]` and `{...}` are not the same instruction to a caller."""
    import typing

    from tests.test_agent_access import _api_routes

    app = client.app_module.app
    routes = {
        (route.served_path, method): route
        for route in _api_routes(app)
        for method in (route.methods or ())
    }

    for entry in agent_router.ENDPOINTS:
        model = routes[(entry.path, entry.method)].response_model
        is_list = typing.get_origin(model) is list
        assert entry.returns.strip().startswith("[") is is_list, (
            f"{entry.method} {entry.path} declares {entry.returns!r} "
            f"for a {'list' if is_list else 'single object'}"
        )
