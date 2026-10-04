"""Changing your own password and your own second factor.

Both are security boundaries rather than features, so every test here asserts
what actually changed: that the old credential stops working, that the new one
starts, and that the sessions and devices standing on the old one are gone.
"""

from __future__ import annotations

import time

import pyotp
import pytest

from tests.conftest import HEADERS, PASSWORD, _setup_owner

NEW_PASSWORD = "an entirely different long password"


def _sign_in(client, password: str, secret: str, clock) -> object:
    """Password then code, returning the final response."""
    first = client.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": password},
        headers=HEADERS,
    )
    if first.status_code != 200:
        return first
    # A trusted browser is signed in on the password alone.
    if not first.json().get("needs_code"):
        return first
    return client.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(secret).at(clock()), "trust_device": False},
        headers=HEADERS,
    )


# --------------------------------------------------------------------------- #
# Password
# --------------------------------------------------------------------------- #


def test_the_password_actually_changes(client, clock):
    world = _setup_owner(client)

    answer = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text

    client.cookies.clear()
    assert _sign_in(client, PASSWORD, world["secret"], clock).status_code == 401, "the old one still works"
    assert _sign_in(client, NEW_PASSWORD, world["secret"], clock).status_code == 200


def test_the_wrong_current_password_changes_nothing(client, clock):
    world = _setup_owner(client)

    answer = client.post(
        "/api/me/password",
        json={"current_password": "not it at all", "new_password": NEW_PASSWORD},
        headers=HEADERS,
    )
    assert answer.status_code == 401
    assert "current password" in answer.json()["detail"]

    client.cookies.clear()
    assert _sign_in(client, PASSWORD, world["secret"], clock).status_code == 200, "unchanged, as it should be"


def test_a_password_change_signs_out_every_other_browser(client, clock):
    """The point of the whole thing: a change made because somebody else knows
    it has to end the session they are sitting in."""
    world = _setup_owner(client)

    # A second browser against the same app: its own cookie jar, same database.
    from fastapi.testclient import TestClient

    other = TestClient(client.app_module.app, base_url="https://testserver")
    assert _sign_in(other, PASSWORD, world["secret"], clock).status_code == 200
    assert other.get("/api/me", headers=HEADERS).status_code == 200, "the second browser is in"

    answer = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=HEADERS,
    )
    assert answer.status_code == 200
    assert answer.json()["other_sessions_ended"] == 1

    assert other.get("/api/me", headers=HEADERS).status_code == 401, "the other browser is out"
    assert client.get("/api/me", headers=HEADERS).status_code == 200, "this one stayed in"


def test_a_password_change_takes_back_trusted_browsers_and_half_finished_sign_ins(
    client, clock
):
    """Browser B is trusted by whoever knew the old password. Once the password
    changes, knowing the new one must not be enough to walk back in from B
    without a code -- which is what a surviving trusted device allowed."""
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.models import PendingSignIn, StepUpGrant, TrustedDevice

    world = _setup_owner(client)
    engine = client.app_module.db_engine
    me = world["user"]["id"]

    def count(model) -> int:
        with Session(engine) as own:
            return own.execute(
                select(func.count()).select_from(model).where(model.user_id == me)
            ).scalar_one()

    browser_b = TestClient(client.app_module.app, base_url="https://testserver")
    browser_b.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    trusted = browser_b.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(world["secret"]).at(clock()), "trust_this_browser": True},
        headers=HEADERS,
    )
    assert trusted.status_code == 200, trusted.text

    # A third browser that got as far as the password, and an unspent grant.
    browser_c = TestClient(client.app_module.app, base_url="https://testserver")
    halfway = browser_c.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert halfway.json()["needs_code"] is True
    granted = client.post(
        "/api/me/step-up",
        json={"password": PASSWORD, "code": pyotp.TOTP(world["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert granted.status_code == 200, granted.text
    assert count(TrustedDevice) == 2 and count(PendingSignIn) == 1 and count(StepUpGrant) == 1

    changed = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=HEADERS,
    )
    assert changed.status_code == 200, changed.text
    # The wizard's browser and browser B.
    assert changed.json()["devices_revoked"] == 2
    assert changed.json()["keys_still_live"] == 0
    assert (count(TrustedDevice), count(PendingSignIn), count(StepUpGrant)) == (0, 0, 0)

    # B's session went with the change; its device cookie is still in the jar.
    assert browser_b.get("/api/me", headers=HEADERS).status_code == 401
    again = browser_b.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": NEW_PASSWORD},
        headers=HEADERS,
    )
    assert again.status_code == 200, again.text
    assert again.json()["needs_code"] is True, "a browser trusted on the old password walked in"
    assert again.json()["authenticated"] is False

    # C's half-finished sign-in is dead, so the code alone no longer gets in.
    finished = browser_c.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(world["secret"]).at(clock()), "trust_this_browser": False},
        headers=HEADERS,
    )
    assert finished.status_code == 401

    # And the grant bought with the old password buys nothing now.
    key = client.post(
        "/api/me/keys",
        json={"step_up_token": granted.json()["token"], "label": "x", "household_id": "nope"},
        headers=HEADERS,
    )
    assert key.status_code == 401


def test_a_password_change_says_how_many_keys_still_work(client, clock):
    """Keys are left alone -- revoking them is a product decision not yet made --
    but the answer must not let "signed out everywhere" read as covering them."""
    world = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    for label in ("one", "two"):
        grant = client.post(
            "/api/me/step-up",
            json={"password": PASSWORD, "code": pyotp.TOTP(world["secret"]).at(clock())},
            headers=HEADERS,
        ).json()
        issued = client.post(
            "/api/me/keys",
            json={"step_up_token": grant["token"], "label": label, "household_id": house["id"]},
            headers=HEADERS,
        )
        assert issued.status_code == 201, issued.text
    revoked = client.post(f"/api/me/keys/{issued.json()['key']['id']}/revoke", headers=HEADERS)
    assert revoked.status_code == 200

    changed = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=HEADERS,
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["keys_still_live"] == 1


def test_a_weak_new_password_is_refused(client):
    _setup_owner(client)
    answer = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": "short"},
        headers=HEADERS,
    )
    assert answer.status_code == 422


def test_the_same_password_again_is_refused(client):
    """Not pedantry: it reports success while ending every other session, so
    somebody would believe they had rotated a password they had not."""
    _setup_owner(client)
    answer = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert "already your password" in answer.json()["detail"]


def test_guessing_the_current_password_is_rate_limited_across_both_routes(client, clock):
    """A stolen session cookie used to be an unlimited password oracle: fifty
    wrong current passwords, fifty 401s. Both routes now spend one budget, the
    step-up's, so alternating between them buys nothing."""
    from app.auth import ratelimit

    world = _setup_owner(client)
    routes = ["/api/me/password", "/api/me/authenticator"]
    for i in range(ratelimit.FREE_ATTEMPTS):
        refused = client.post(
            routes[i % 2],
            json={"current_password": f"guess number {i}", "new_password": NEW_PASSWORD},
            headers=HEADERS,
        )
        assert refused.status_code == 401, refused.text

    # The sixth -- on either route, and even with the RIGHT password -- is a lockout.
    for path in routes:
        locked = client.post(
            path,
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=HEADERS,
        )
        assert locked.status_code == 429, locked.text
        assert int(locked.headers["Retry-After"]) > 0

    # And it is the step-up's budget too, not a third one beside it.
    grant = client.post(
        "/api/me/step-up",
        json={"password": PASSWORD, "code": pyotp.TOTP(world["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert grant.status_code == 429

    # Nothing changed while it was being guessed at.
    client.cookies.clear()
    assert _sign_in(client, PASSWORD, world["secret"], clock).status_code == 200


# --------------------------------------------------------------------------- #
# The authenticator
# --------------------------------------------------------------------------- #


def _confirmation(offer: dict, world: dict, clock) -> dict:
    """A confirm body: the new code, and one from the authenticator it replaces."""
    at = clock()
    return {
        "token": offer["token"],
        "code": pyotp.TOTP(offer["secret"]).at(at),
        "current_code": pyotp.TOTP(world["secret"]).at(at),
    }


def test_a_new_authenticator_replaces_the_old_one(client, clock):
    world = _setup_owner(client)

    offered = client.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    )
    assert offered.status_code == 200, offered.text
    offer = offered.json()
    assert offer["secret"] != world["secret"]

    # Nothing has changed yet -- the old authenticator still signs you in.
    client.cookies.clear()
    assert _sign_in(client, PASSWORD, world["secret"], clock).status_code == 200

    confirmed = client.post(
        "/api/me/authenticator/confirm",
        json=_confirmation(offer, world, clock),
        headers=HEADERS,
    )
    assert confirmed.status_code == 200, confirmed.text

    client.cookies.clear()
    old = _sign_in(client, PASSWORD, world["secret"], clock)
    assert old.status_code == 401, "the old authenticator still works"

    client.cookies.clear()
    assert _sign_in(client, PASSWORD, offer["secret"], clock).status_code == 200


def _stored_secret(client, user_id: str) -> bytes:
    from sqlalchemy.orm import Session

    from app.models import User

    with Session(client.app_module.db_engine) as own:
        return own.get(User, user_id).totp_secret


def test_a_session_and_the_password_cannot_replace_the_authenticator(client, clock):
    """The takeover: a session and the password were enough to swap in your own
    authenticator, pass the step-up with it and mint a year-long write key while
    the owner's phone stopped working. Without the current factor, nothing moves.
    """
    world = _setup_owner(client)
    me = world["user"]["id"]
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    before = _stored_secret(client, me)

    offer = client.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    ).json()
    at = clock()
    mine = pyotp.TOTP(offer["secret"]).at(at)

    missing = client.post(
        "/api/me/authenticator/confirm", json={"token": offer["token"], "code": mine},
        headers=HEADERS,
    )
    assert missing.status_code == 422
    # The attacker's own code offered as the "current" one, and a plain guess.
    for guess in (mine, "123456", "not-a-recovery-code"):
        refused = client.post(
            "/api/me/authenticator/confirm",
            json={"token": offer["token"], "code": mine, "current_code": guess},
            headers=HEADERS,
        )
        assert refused.status_code == 401, refused.text
    assert _stored_secret(client, me) == before, "the second factor was replaced"

    # So the attacker's authenticator buys no step-up, and no key.
    grant = client.post(
        "/api/me/step-up",
        json={"password": PASSWORD, "code": pyotp.TOTP(offer["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert grant.status_code == 401
    key = client.post(
        "/api/me/keys",
        json={"step_up_token": "anything", "label": "x", "household_id": house["id"]},
        headers=HEADERS,
    )
    assert key.status_code == 401

    # And the owner's phone still works.
    client.cookies.clear()
    assert _sign_in(client, PASSWORD, world["secret"], clock).status_code == 200


def test_guessing_the_current_code_is_rate_limited(client, clock):
    """Six digits and three live steps is a million-to-three guess; unlimited
    guesses would make the current-factor check decoration."""
    from app.auth import ratelimit

    world = _setup_owner(client)
    offer = client.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    ).json()
    for i in range(ratelimit.FREE_ATTEMPTS):
        refused = client.post(
            "/api/me/authenticator/confirm",
            json={
                "token": offer["token"],
                "code": pyotp.TOTP(offer["secret"]).at(clock()),
                "current_code": f"{i:06d}",
            },
            headers=HEADERS,
        )
        assert refused.status_code == 401, refused.text

    locked = client.post(
        "/api/me/authenticator/confirm", json=_confirmation(offer, world, clock), headers=HEADERS
    )
    assert locked.status_code == 429, locked.text
    assert int(locked.headers["Retry-After"]) > 0


def test_replacing_the_authenticator_signs_out_every_other_browser(client, clock):
    """Same reasoning as a password change: a factor is replaced because somebody
    else may hold it, and the session they are sitting in has to end."""
    from fastapi.testclient import TestClient

    world = _setup_owner(client)
    other = TestClient(client.app_module.app, base_url="https://testserver")
    assert _sign_in(other, PASSWORD, world["secret"], clock).status_code == 200
    assert other.get("/api/me", headers=HEADERS).status_code == 200

    offer = client.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    ).json()
    done = client.post(
        "/api/me/authenticator/confirm", json=_confirmation(offer, world, clock), headers=HEADERS
    )
    assert done.status_code == 200, done.text
    assert done.json()["other_sessions_ended"] == 1

    assert other.get("/api/me", headers=HEADERS).status_code == 401, "the other browser is in"
    assert client.get("/api/me", headers=HEADERS).status_code == 200, "this one stayed in"
    assert _stored_secret(client, world["user"]["id"]) != b""


def test_a_recovery_code_stands_in_for_a_lost_authenticator_once(client, clock):
    """The screen offers to replace "one you no longer have". A recovery code is
    what proves you held it, and it is spent doing so."""
    world = _setup_owner(client)
    recovery = world["recovery_codes"][0]

    offer = client.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    ).json()
    body = {
        "token": offer["token"],
        "code": pyotp.TOTP(offer["secret"]).at(clock()),
        "current_code": recovery,
    }
    done = client.post("/api/me/authenticator/confirm", json=body, headers=HEADERS)
    assert done.status_code == 200, done.text

    # The same recovery code cannot do it twice.
    offer = client.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    ).json()
    body = {
        "token": offer["token"],
        "code": pyotp.TOTP(offer["secret"]).at(clock()),
        "current_code": recovery,
    }
    again = client.post("/api/me/authenticator/confirm", json=body, headers=HEADERS)
    assert again.status_code == 401, "a recovery code was accepted twice"


def test_the_wrong_code_leaves_the_old_authenticator_alone(client, clock):
    world = _setup_owner(client)
    offer = client.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    ).json()

    answer = client.post(
        "/api/me/authenticator/confirm",
        json={
            "token": offer["token"],
            "code": "000000",
            "current_code": pyotp.TOTP(world["secret"]).at(clock()),
        },
        headers=HEADERS,
    )
    assert answer.status_code == 422

    client.cookies.clear()
    assert _sign_in(client, PASSWORD, world["secret"], clock).status_code == 200, "still the old one"


def test_the_wrong_password_offers_nothing(client):
    _setup_owner(client)
    answer = client.post(
        "/api/me/authenticator", json={"current_password": "nope"}, headers=HEADERS
    )
    assert answer.status_code == 401


def test_replacing_the_authenticator_untrusts_every_browser(client, clock):
    """A device was trusted on the strength of the factor being replaced."""
    world = _setup_owner(client)

    client.cookies.clear()
    client.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    trusted = client.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(world["secret"]).at(clock()), "trust_device": True},
        headers=HEADERS,
    )
    assert trusted.status_code == 200, trusted.text

    offer = client.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    ).json()
    done = client.post(
        "/api/me/authenticator/confirm",
        json=_confirmation(offer, world, clock),
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    # Two: the wizard trusts the browser it ran in, and the sign-in above
    # trusted one as well. Both stood on the authenticator being replaced.
    assert done.json()["devices_revoked"] == 2

    # And the trust is really gone: signing in again asks for a code.
    client.cookies.clear()
    again = client.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    body = again.json()
    assert body["needs_code"] is True, "the browser was still trusted"
    assert body["authenticated"] is False


def test_the_proving_code_is_spent(client, clock):
    """Same promise enrolment makes: a used code is dead."""
    world = _setup_owner(client)
    offer = client.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    ).json()
    code = pyotp.TOTP(offer["secret"]).at(clock())
    assert (
        client.post(
            "/api/me/authenticator/confirm",
            json={
                "token": offer["token"],
                "code": code,
                "current_code": pyotp.TOTP(world["secret"]).at(int(time.time())),
            },
            headers=HEADERS,
        ).status_code
        == 200
    )

    client.cookies.clear()
    client.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    reused = client.post(
        "/api/session/code", json={"code": code, "trust_device": False}, headers=HEADERS
    )
    assert reused.status_code == 401, "the code that proved the pairing was accepted again"


@pytest.mark.parametrize("path", ["/api/me/password", "/api/me/authenticator"])
def test_neither_is_reachable_signed_out(client, path):
    _setup_owner(client)
    client.cookies.clear()
    answer = client.post(path, json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                         headers=HEADERS)
    assert answer.status_code == 401
