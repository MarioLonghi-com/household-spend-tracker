"""The second door: what a bearer token gets you, and what it never does.

Three kinds of test here.

*The door* -- one flat refusal for every way a key can be no good, because the
difference is only useful to somebody guessing.

*The floor* -- §1.2 of the spec lists what no key may ever do: delete anything,
undo anything, touch auth, admin, membership, other keys or `/db`. That is not
enforced by a check; it is enforced by those routes asking for `CurrentUser`
and never for `CurrentAgent`. `test_the_capability_floor_...` walks the real
route table and asserts it, so the floor cannot be widened by accident.

*The middleware* -- the one change to the CSRF check, tested in both
directions, because the direction that must still refuse is the one that
matters.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import Request
from sqlalchemy import func, select

from app.api import deps
from app.audit.batch import batch
from app.errors import Forbidden, NotFound, Unauthorized
from app.models import AgentScope, Batch, BatchKind, Change, Transaction, utcnow
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner


def _request(token: str | None, **headers) -> Request:
    """A bare ASGI scope carrying whatever headers the test cares about."""
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    if token is not None:
        raw.append((b"authorization", f"Bearer {token}".encode()))
    return Request({"type": "http", "method": "GET", "headers": raw, "path": "/"})


@pytest.fixture()
def read_key(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        row, token = key_service.issue(
            session, user=owner, household=household, label="the analyst",
            agent_name="Claude Desktop",
        )
    session.commit()
    return row, token


@pytest.fixture()
def write_key(session, member, other_household):
    """The second key the spec asks for: read-write, in the *other* household."""
    with batch(
        session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id
    ):
        row, token = key_service.issue(
            session, user=member, household=other_household, label="the importer",
            scope=AgentScope.write,
        )
    session.commit()
    return row, token


# --------------------------------------------------------------------------- #
# The door
# --------------------------------------------------------------------------- #


def test_a_good_key_opens_the_door_and_carries_both_identities(
    session, owner, household, read_key
):
    row, token = read_key
    agent = deps.current_agent(_request(token), session)

    assert agent.key is row
    assert agent.user is owner, "the human is still who the batch will name"
    assert agent.household is household


@pytest.mark.parametrize(
    "presented",
    ["", "not-prefixed-at-all", "stk_invented", "Bearer stk_"],
    ids=["empty", "wrong-prefix", "unknown", "prefix-only"],
)
def test_every_bad_key_gets_the_same_sentence(session, owner, household, read_key, presented):
    """Every one of these arrived in `Authorization`, and that is the point.

    Once a credential is in the right place, what happened to it is none of the
    caller's business: unknown, malformed, revoked, expired and owner-disabled
    are one answer so a caller cannot probe which. `absent` used to be on this
    list and moved to the test below -- see #38.
    """
    with pytest.raises(Unauthorized) as refused:
        deps.current_agent(_request(presented), session)
    assert str(refused.value) == "that key is not valid"


def test_no_credential_at_all_is_told_what_to_send(session, owner, household, read_key):
    """Not a statement about a key, so it does not weaken the rule above.

    Nothing was presented, so `agent_keys.lookup` is never reached and there is
    no key whose state could leak. Answering "that key is not valid" to a
    caller who sent no key asserted the wrong cause and sent them looking for a
    bad token -- which is exactly what it cost during the 2026-09-22 run.
    """
    with pytest.raises(Unauthorized) as refused:
        deps.current_agent(_request(None), session)

    said = str(refused.value)
    assert said != "that key is not valid", "nothing was presented, so nothing was invalid"
    assert "Authorization" in said and "Bearer" in said, said


def test_a_credential_in_a_header_this_api_does_not_read_names_that_header(
    session, owner, household, read_key
):
    """A valid key in `X-API-Key` was told the key was bad. It was not.

    Which header the caller used is a fact they already hold, so naming it
    tells a prober nothing -- and it saves an honest caller the round trip.
    """
    _row, token = read_key

    with pytest.raises(Unauthorized) as refused:
        deps.current_agent(_request(None, **{"X-API-Key": token}), session)

    said = str(refused.value)
    assert "X-API-Key" in said, said
    assert "Authorization" in said and "Bearer" in said, said
    assert said != "that key is not valid", "the key was valid; the header was not read"


def test_a_revoked_key_is_refused_and_opens_no_batch(session, owner, household, read_key):
    """Spec test 1: the refusal must also leave the ledger completely alone."""
    row, token = read_key
    before = session.execute(select(func.count()).select_from(Batch)).scalar_one()

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        key_service.revoke(session, row, by=owner)
    session.commit()
    after_revoke = session.execute(select(func.count()).select_from(Batch)).scalar_one()

    with pytest.raises(Unauthorized):
        deps.current_agent(_request(token), session)

    assert session.execute(select(func.count()).select_from(Batch)).scalar_one() == after_revoke
    assert after_revoke == before + 1, "only the revocation itself wrote a batch"


def test_an_expired_key_is_refused(session, owner, household, read_key):
    row, token = read_key
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        row.expires_at = utcnow() - timedelta(seconds=1)
    session.flush()

    with pytest.raises(Unauthorized):
        deps.current_agent(_request(token), session)


def test_a_key_whose_owner_is_disabled_is_refused(session, owner, household, read_key):
    """Spec test 6. What `current_user` already does for their sessions."""
    row, token = read_key
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        owner.disabled_at = utcnow()
    session.flush()

    with pytest.raises(Unauthorized):
        deps.current_agent(_request(token), session)


def test_opening_the_door_records_the_use_without_touching_the_log(
    session, owner, household, read_key
):
    row, token = read_key
    before = session.execute(select(func.count()).select_from(Change)).scalar_one()

    deps.current_agent(_request(token), session)
    session.flush()
    session.commit()

    assert row.last_used_at is not None
    assert session.execute(select(func.count()).select_from(Change)).scalar_one() == before


# --------------------------------------------------------------------------- #
# Scope
# --------------------------------------------------------------------------- #


def test_a_read_only_key_is_refused_a_write_and_told_plainly(
    session, owner, household, read_key
):
    """Spec test 5. 403, not 404: they hold a real key, so the door is known."""
    _row, token = read_key
    agent = deps.current_agent(_request(token), session)
    before = session.execute(select(func.count()).select_from(Transaction)).scalar_one()

    with pytest.raises(Forbidden, match="read-only"):
        deps.agent_may_write(agent)

    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == before


def test_a_write_key_passes_and_still_cannot_commit(session, member, other_household, write_key):
    _row, token = write_key
    agent = deps.current_agent(_request(token), session)

    assert deps.agent_may_write(agent) is agent
    with pytest.raises(Forbidden) as refused:
        deps.agent_may_commit(agent)
    # The refusal hands off rather than just failing: the work is not lost.
    assert "preview" in str(refused.value)


# --------------------------------------------------------------------------- #
# Reach: one household, and 404 for everything else
# --------------------------------------------------------------------------- #


def test_a_key_cannot_see_into_another_household_and_cannot_tell_it_is_there(
    session, owner, household, other_household, accounts, read_key
):
    """Spec test 4, and the assertion is that the two refusals are identical."""
    _row, token = read_key
    agent = deps.current_agent(_request(token), session)

    from app.models import Account

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=other_household.id):
        theirs = Account(
            household_id=other_household.id, name="Theirs", currency="GBP",
            type=accounts["checking"].type,
        )
        session.add(theirs)
    session.flush()

    with pytest.raises(NotFound) as for_real_id:
        agent.load(Account, theirs.id)
    with pytest.raises(NotFound) as for_invented:
        agent.load(Account, "no-such-account-at-all")

    assert str(for_real_id.value) == str(for_invented.value), (
        "a real id in another household must be indistinguishable from a made-up one"
    )


def test_a_key_reaches_its_own_household(session, owner, household, accounts, read_key):
    from app.models import Account

    _row, token = read_key
    agent = deps.current_agent(_request(token), session)
    assert agent.load(Account, accounts["checking"].id) is accounts["checking"]


# --------------------------------------------------------------------------- #
# The floor
# --------------------------------------------------------------------------- #


def _api_routes(app) -> list:
    """Every real endpoint, flattened.

    `app.routes` is not a flat list: this FastAPI version keeps an included
    router as a single nested object, so walking the top level finds `/health`
    and little else -- and a floor test written against it would pass by
    looking at almost nothing.
    """
    found: list = []
    stack: list[tuple[object, str]] = [(route, "") for route in app.routes]
    seen: set[tuple[int, str]] = set()
    while stack:
        route, prefix = stack.pop()
        if (id(route), prefix) in seen:
            continue
        seen.add((id(route), prefix))

        # An included router is one opaque object here; its endpoints hang off
        # `original_router.routes` and are stored UNPREFIXED, with the prefix
        # the include applied kept on `include_context`. Carrying it down is
        # what makes `served_path` the URL somebody can actually call rather
        # than the one the router was written with.
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            added = getattr(context, "prefix", "") or ""
            stack.extend((child, prefix + added) for child in inner.routes)
        stack.extend((child, prefix) for child in getattr(route, "routes", None) or [])

        if hasattr(route, "dependant"):
            route.served_path = prefix + route.path
            found.append(route)
    return found


def _dependencies_of(route) -> set:
    """Every dependency callable a route resolves, however deeply nested."""
    found: set = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            found.add(dep.call)
        stack.extend(dep.dependencies)
    return found


#: Path fragments no agent route may carry: auth, admin, membership, keys, the
#: owner-only one-time import and `/db` (#217). `/keys` is its own entry rather
#: than caught by `/me/`, which only matches today by luck of the prefix.
FORBIDDEN_TO_AGENTS = (
    "/me/", "/keys", "/users", "/invitations", "/members", "/admin",
    "/session", "/setup", "/db", "/one-time-import",
)


def test_the_capability_floor_is_structural_and_not_a_check(client):
    """§1.2: no key may delete, undo, or touch auth, admin, membership or /db.

    None of that is enforced by an `if`. It is enforced by those routes asking
    for `current_user` and never for `current_agent`, which means the floor
    cannot be lowered without somebody editing a route signature. This walks the
    real route table so that edit fails here.
    """
    app = client.app_module.app

    # The walk has to actually find things, or this test passes by looking at
    # an empty list -- which is exactly what it would do today, while no route
    # asks for `current_agent` yet. Proving the mechanism against the cookie
    # door is what makes the assertion below real the moment the first agent
    # route lands.
    routes = _api_routes(app)
    cookie_routes = [r for r in routes if deps.current_user in _dependencies_of(r)]
    assert len(cookie_routes) > 20, (
        "the dependency walk found almost nothing, so the floor check below is "
        f"vacuous. Found: {len(cookie_routes)}"
    )

    reachable = [
        route for route in routes if deps.current_agent in _dependencies_of(route)
    ]
    # The floor was vacuous until the first agent route existed. It does now,
    # so assert the list is not empty -- otherwise a future refactor that
    # unmounted the agent router would make every check below pass by finding
    # nothing to check.
    assert reachable, "no route asks for current_agent; the floor check is vacuous"

    # Each fragment has to name something real, or it guards nothing: a route
    # renamed out from under its entry would leave the floor open while this
    # list still read as complete.
    every_path = [route.served_path for route in routes]
    stale = [one for one in FORBIDDEN_TO_AGENTS if not any(one in p for p in every_path)]
    assert not stale, f"these fragments match no route any more: {stale}"
    assert any(
        "POST" in (route.methods or ()) and route.served_path.rstrip("/").endswith("/households")
        for route in cookie_routes
    ), "the household-creation check matches no route any more"

    offenders = []
    for route in reachable:
        path = route.served_path
        methods = set(route.methods or ())
        if "DELETE" in methods:
            offenders.append(f"DELETE {path} -- an agent may never delete")
        if "/undo" in path:
            offenders.append(f"{methods} {path} -- undo is a person's button")
        # Taking a link apart or vouching for one is a person's (#134): an
        # agent may link transfers, and nothing it links counts as history
        # until a person confirms it.
        for forbidden in ("/unlink", "/confirm"):
            if forbidden in path:
                offenders.append(f"{methods} {path} -- {forbidden} is a person's")
        for forbidden in FORBIDDEN_TO_AGENTS:
            if forbidden in path:
                offenders.append(f"{methods} {path} -- {forbidden} is off the floor")
        # Membership and keys are nested under a household, so creating one is
        # the bare collection -- which no fragment above can name (#217).
        if "POST" in methods and path.rstrip("/").endswith("/households"):
            offenders.append(f"POST {path} -- a key does not create households")

    assert not offenders, "the capability floor has been lowered:\n  " + "\n  ".join(offenders)


def test_no_agent_route_also_accepts_a_cookie(client):
    """The two doors stay two.

    A route asking for both would be one a browser could reach with ambient
    credentials *and* one the CSRF middleware waves past on a header -- which
    is the combination the middleware's "only" exists to prevent.
    """
    app = client.app_module.app
    both = [
        route.served_path for route in _api_routes(app)
        if deps.current_agent in _dependencies_of(route)
        and deps.current_user in _dependencies_of(route)
    ]
    assert both == [], f"these routes take both credentials: {both}"


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #


def test_a_bearer_only_write_is_not_refused_by_the_csrf_check(client):
    """Spec test 8, at the level the middleware actually works.

    No Origin, no Sec-Fetch-Site, no cookie -- which is every non-browser
    client there is, and which the check refused outright before this change.
    The path need not exist: the middleware runs before routing, so "not the
    CSRF refusal" is exactly the property under test.
    """
    # The instance has to be set up first: `gate_until_configured` is registered
    # after this check and therefore runs OUTSIDE it, so a fresh instance
    # answers 503 to everything under /api and a test written without this
    # passes while proving nothing at all.
    _setup_owner(client)
    client.cookies.clear()

    answer = client.post(
        "/api/agent/v1/anything",
        json={},
        headers={"authorization": "Bearer stk_whatever"},
    )
    # 405, because the SPA catch-all claims the path for GET and not for POST.
    # Which of the two it is does not matter; that it is a *routing* answer and
    # not the middleware's refusal is the whole assertion.
    assert answer.status_code in (404, 405), (
        f"the middleware should have let this reach routing, got {answer.status_code}: "
        f"{answer.text}"
    )
    assert answer.json().get("detail") != "that request did not come from this app"


def test_a_write_with_a_cookie_is_still_checked_however_it_is_dressed(client, clock):
    """Spec test 9, and the one that matters.

    A page on another origin can add a constant `Authorization` header without
    knowing any key. If the header alone bought an exemption, that page would
    ride the victim's session cookie. So when a cookie is present the origin
    check runs regardless.
    """
    _setup_owner(client)  # leaves this client holding a real session cookie

    refused = client.post(
        "/api/households",
        json={"name": "Theirs"},
        headers={"origin": "https://evil.example", "authorization": "Bearer stk_whatever"},
    )
    assert refused.status_code == 403
    assert "did not come from this app" in refused.json()["detail"]


def test_a_bearer_write_without_the_prefix_is_still_checked(client):
    """The exemption is keyed on our own prefix, not on the word Bearer.

    Anything else in that header is somebody else's scheme, and it buys nothing.
    """
    _setup_owner(client)
    client.cookies.clear()

    refused = client.post(
        "/api/households",
        json={"name": "Theirs"},
        headers={"authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.nope"},
    )
    assert refused.status_code == 403
    assert "did not come from this app" in refused.json()["detail"]


def test_the_existing_cookie_path_is_untouched(client):
    """Spec test 10: the browser tests must pass unmodified.

    Re-asserted here rather than only in test_api.py, because this file is what
    somebody reads when they change the middleware.
    """
    _setup_owner(client)
    made = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS)
    assert made.status_code == 201, made.text

    refused = client.post(
        "/api/households", json={"name": "Theirs"}, headers={"origin": "https://evil.example"}
    )
    assert refused.status_code == 403
