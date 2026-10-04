"""Issuing, listing and revoking keys from the profile screen.

The bar for issuing is the point of this file: a session cookie is not enough,
because a key is the only thing here whose effect outlives the session that
made it. So the tests that matter are the ones asserting a key was NOT created.
"""

from __future__ import annotations

import pyotp
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AgentKey
from tests.conftest import HEADERS, PASSWORD, _setup_owner


@pytest.fixture()
def home(client):
    """A signed-in owner with a household to point a key at."""
    world = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS)
    assert house.status_code == 201, house.text
    return {"world": world, "household": house.json()}


def _count(client) -> int:
    with Session(client.app_module.db_engine) as own:
        return own.execute(select(func.count()).select_from(AgentKey)).scalar_one()


def _step_up(client, secret: str, clock) -> str:
    answer = client.post(
        "/api/me/step-up",
        json={"password": PASSWORD, "code": pyotp.TOTP(secret).at(clock())},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["token"]


def _issue(client, home, token, **overrides):
    body = {
        "step_up_token": token,
        "label": "receipt filer",
        "agent_name": "Claude Desktop",
        "household_id": home["household"]["id"],
    }
    body.update(overrides)
    return client.post("/api/me/keys", json=body, headers=HEADERS)


# --------------------------------------------------------------------------- #
# Issuing
# --------------------------------------------------------------------------- #


def test_a_key_needs_both_factors_and_not_just_the_cookie(client, home, clock):
    """The whole reason the step-up exists."""
    refused = _issue(client, home, "not-a-real-grant")
    assert refused.status_code == 401
    assert _count(client) == 0, "nothing may be written without a fresh proof"

    token = _step_up(client, home["world"]["secret"], clock)
    made = _issue(client, home, token)
    assert made.status_code == 201, made.text
    assert _count(client) == 1


def test_the_token_is_shown_once_and_then_never_again(client, home, clock):
    token = _step_up(client, home["world"]["secret"], clock)
    issued = _issue(client, home, token).json()

    assert issued["token"].startswith("stk_")
    assert "token" not in issued["key"], "the row itself must never carry it"

    listed = client.get("/api/me/keys", headers=HEADERS).json()
    assert len(listed) == 1
    assert "token" not in listed[0]
    # And nothing anywhere in the listing is the value.
    assert issued["token"] not in client.get("/api/me/keys", headers=HEADERS).text


def test_a_grant_buys_exactly_one_key(client, home, clock):
    """Single use, asserted where it actually costs something."""
    token = _step_up(client, home["world"]["secret"], clock)
    assert _issue(client, home, token).status_code == 201

    again = _issue(client, home, token, label="a second one")
    assert again.status_code == 401
    assert _count(client) == 1


def test_a_failed_issue_still_spends_the_grant(client, home, clock):
    """Otherwise a grant is worth as many attempts as you like."""
    token = _step_up(client, home["world"]["secret"], clock)
    refused = _issue(client, home, token, label="   ")
    assert refused.status_code == 422
    assert _count(client) == 0

    # The grant is gone, so a correct request with it is refused too.
    assert _issue(client, home, token).status_code == 401
    assert _count(client) == 0


def test_a_household_you_are_not_in_reads_like_one_that_never_existed(
    client, home, clock
):
    token = _step_up(client, home["world"]["secret"], clock)
    refused = _issue(client, home, token, household_id="not-a-household")
    assert refused.status_code == 404
    assert _count(client) == 0


def test_what_was_chosen_is_what_is_stored(client, home, clock):
    token = _step_up(client, home["world"]["secret"], clock)
    key = _issue(
        client, home, token, scope="write", may_commit=True, days=30
    ).json()["key"]

    assert key["scopes"] == ["read", "write"], "write implies read, computed not stored"
    assert key["may_commit"] is True
    assert key["live"] is True
    assert key["agent_name"] == "Claude Desktop"


def test_a_read_only_key_that_may_commit_is_refused(client, home, clock):
    token = _step_up(client, home["world"]["secret"], clock)
    refused = _issue(client, home, token, scope="read", may_commit=True)
    assert refused.status_code == 422
    assert "nothing to commit" in refused.json()["detail"]
    assert _count(client) == 0


def test_issuing_lands_in_the_household_s_history(client, home, clock):
    """A credential handed out is an act, and History is where acts go."""
    token = _step_up(client, home["world"]["secret"], clock)
    _issue(client, home, token)

    batches = client.get(
        f"/api/households/{home['household']['id']}/batches", headers=HEADERS
    ).json()
    assert any(b["kind"] == "admin" for b in batches)


# --------------------------------------------------------------------------- #
# Revoking
# --------------------------------------------------------------------------- #


def test_revoking_takes_effect_on_the_next_request(client, home, clock):
    token = _step_up(client, home["world"]["secret"], clock)
    issued = _issue(client, home, token).json()

    manifest = client.get(
        "/api/agent/v1/manifest", headers={"authorization": f"Bearer {issued['token']}"}
    )
    assert manifest.status_code == 200, "the key works before it is revoked"

    revoked = client.post(f"/api/me/keys/{issued['key']['id']}/revoke", headers=HEADERS)
    assert revoked.status_code == 200
    assert revoked.json()["live"] is False

    after = client.get(
        "/api/agent/v1/manifest", headers={"authorization": f"Bearer {issued['token']}"}
    )
    assert after.status_code == 401, "revocation is a row read, so it is instant"


def test_undoing_a_revocation_never_brings_the_key_back(client, home, clock):
    """`revoked_at` is an ordinary captured column, so the revoke batch's
    before-image holds NULL -- and replaying it resurrected the key. The undo
    route asks only for membership, so any member could do it. Neither the
    holder nor anyone else in the household can now."""
    from fastapi.testclient import TestClient

    from tests.test_invitations import _accept, _invite

    house_id = home["household"]["id"]
    token = _step_up(client, home["world"]["secret"], clock)
    issued = _issue(client, home, token).json()
    bearer = {"authorization": f"Bearer {issued['token']}"}
    revoked = client.post(f"/api/me/keys/{issued['key']['id']}/revoke", headers=HEADERS)
    assert revoked.status_code == 200
    assert client.get("/api/agent/v1/manifest", headers=bearer).status_code == 401

    history = client.get(
        f"/api/households/{house_id}/batches?include_single_edits=true", headers=HEADERS
    ).json()
    revoke_batch, issue_batch = history[0], history[1]
    # History says what these were, rather than "Setup change".
    assert revoke_batch["headline"] == "Agent key"
    assert "Revoked" in revoke_batch["detail"] and "receipt filer" in revoke_batch["detail"]
    assert "Issued" in issue_batch["detail"]

    # Another member of the same household.
    invited = _invite(client, households=[house_id])
    partner = TestClient(client.app_module.app, base_url="https://testserver")
    _accept(partner, invited["token"], email="partner@example.com", name="Partner")

    for who in (client, partner):
        for target in (revoke_batch, issue_batch):
            refused = who.post(
                f"/api/households/{house_id}/batches/{target['id']}/undo", headers=HEADERS
            )
            assert refused.status_code == 409, refused.text

    assert client.get("/api/agent/v1/manifest", headers=bearer).status_code == 401, (
        "a revoked key came back"
    )
    listed = client.get("/api/me/keys", headers=HEADERS).json()
    assert [one["live"] for one in listed] == [False]
    assert listed[0]["revoked_at"] is not None


def test_revoking_needs_no_step_up(client, home, clock):
    """Taking a credential away is the safe direction.

    A bar high enough to slow somebody down in the moment they realise a key
    has leaked is a bar in the wrong place.
    """
    token = _step_up(client, home["world"]["secret"], clock)
    issued = _issue(client, home, token).json()

    # No fresh grant, just the cookie.
    assert client.post(
        f"/api/me/keys/{issued['key']['id']}/revoke", headers=HEADERS
    ).status_code == 200


def test_a_key_that_is_not_yours_reads_like_one_that_never_existed(client, home, clock):
    refused = client.post("/api/me/keys/invented/revoke", headers=HEADERS)
    assert refused.status_code == 404
    assert refused.json()["detail"] == "no such key"


def test_revoking_twice_is_not_an_error_and_does_not_move_the_clock(client, home, clock):
    token = _step_up(client, home["world"]["secret"], clock)
    issued = _issue(client, home, token).json()
    path = f"/api/me/keys/{issued['key']['id']}/revoke"

    first = client.post(path, headers=HEADERS).json()["revoked_at"]
    second = client.post(path, headers=HEADERS).json()["revoked_at"]
    assert first == second, "the moment it stopped working is a fact, not a timestamp"


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def test_the_list_keeps_dead_keys_because_that_is_the_question(client, home, clock):
    token = _step_up(client, home["world"]["secret"], clock)
    issued = _issue(client, home, token).json()
    client.post(f"/api/me/keys/{issued['key']['id']}/revoke", headers=HEADERS)

    listed = client.get("/api/me/keys", headers=HEADERS).json()
    assert len(listed) == 1, "a revoked key is still something you gave out"
    assert listed[0]["live"] is False


def test_a_signed_out_caller_sees_no_keys(client, home):
    client.cookies.clear()
    assert client.get("/api/me/keys").status_code == 401


def test_using_a_key_shows_up_as_last_used(client, home, clock):
    token = _step_up(client, home["world"]["secret"], clock)
    issued = _issue(client, home, token).json()
    assert issued["key"]["last_used_at"] is None

    client.get(
        "/api/agent/v1/manifest", headers={"authorization": f"Bearer {issued['token']}"}
    )

    listed = client.get("/api/me/keys", headers=HEADERS).json()
    assert listed[0]["last_used_at"] is not None


# --------------------------------------------------------------------------- #
# A recovery code takes the keys back too (#210)
# --------------------------------------------------------------------------- #


def _works(client, token: str) -> bool:
    answer = client.get("/api/agent/v1/manifest", headers={"authorization": f"Bearer {token}"})
    return answer.status_code == 200


def test_redeeming_a_recovery_code_revokes_every_live_key_and_says_how_many(
    client, home, clock
):
    """The phone is gone, so the browsers are in doubt -- and so is every key
    minted from one of them, which is the quiet credential."""
    from app.auth import cookies

    world = home["world"]
    away = client.post("/api/households", json={"name": "Away"}, headers=HEADERS).json()

    here = _issue(client, home, _step_up(client, world["secret"], clock)).json()
    clock()
    there = _issue(
        client, home, _step_up(client, world["secret"], clock), household_id=away["id"]
    ).json()
    clock()
    already = _issue(client, home, _step_up(client, world["secret"], clock)).json()
    assert client.post(f"/api/me/keys/{already['key']['id']}/revoke", headers=HEADERS).status_code == 200
    assert _works(client, here["token"]) and _works(client, there["token"])

    # Lost phone: an untrusted browser, the password, then a recovery code.
    client.delete("/api/session", headers=HEADERS)
    client.cookies.delete(cookies.device_name())
    first = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert first.json()["needs_code"] is True
    back = client.post(
        "/api/session/recovery", json={"code": world["recovery_codes"][0]}, headers=HEADERS
    )
    assert back.status_code == 200, back.text
    # Two were live; the one revoked by hand is not counted again.
    assert back.json()["keys_revoked"] == 2

    assert not _works(client, here["token"])
    assert not _works(client, there["token"])
    listed = {key["id"]: key for key in client.get("/api/me/keys").json()}
    assert [listed[k["key"]["id"]]["live"] for k in (here, there, already)] == [False] * 3


def test_an_ordinary_sign_in_says_no_keys_were_revoked(client, home, clock):
    token = _issue(client, home, _step_up(client, home["world"]["secret"], clock)).json()["token"]
    client.delete("/api/session", headers=HEADERS)
    again = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert again.status_code == 200 and again.json()["keys_revoked"] == 0
    assert _works(client, token), "only a recovery code takes the keys back"
