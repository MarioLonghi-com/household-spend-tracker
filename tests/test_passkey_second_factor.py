"""A passkey and an authenticator, interchangeable as the second factor (#456).

Three accounts, each walked in through the real endpoints:

* **Jane**, the owner, set up with a passkey and no authenticator ever;
* **Sam**, a member invited with an authenticator and nothing else -- the
  account every instance had before this change;
* **Robin**, a member invited with a passkey, who then adds an authenticator
  too, so has both.

Every assertion is made by `tests/soft_authenticator.py` against the real
endpoints, and each test asserts what changed: the rows, the grant's factor,
the session the browser got, the History sentence.
"""

from __future__ import annotations

import time

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountReset, Batch, Passkey, PendingSignIn, RecoveryCode, TrustedDevice, User
from tests.conftest import HEADERS, PASSWORD
from tests.passkey_world import ORIGIN
from tests.soft_authenticator import SoftAuthenticator


def _browser(client) -> TestClient:
    return TestClient(client.app_module.app, base_url=ORIGIN)


def _db(client) -> Session:
    return Session(client.app_module.db_engine)


def _user(client, user_id: str) -> User:
    with _db(client) as own:
        user = own.get(User, user_id)
        own.expunge(user)
        return user


def _passkeys_of(client, user_id: str) -> list[Passkey]:
    with _db(client) as own:
        return list(own.execute(select(Passkey).where(Passkey.user_id == user_id)).scalars())


def _codes_of(client, user_id: str) -> int:
    with _db(client) as own:
        return len(list(own.execute(select(RecoveryCode).where(RecoveryCode.user_id == user_id)).scalars()))


def _setup_with_passkey(client, authenticator: SoftAuthenticator) -> dict:
    """The first-run wizard with a passkey as the second step."""
    import app.auth.setup as setup_service

    started = client.post(
        "/api/setup/begin",
        json={
            "token": setup_service.current_setup_token(),
            "email": "jane@example.com",
            "display_name": "Jane",
            "password": PASSWORD,
        },
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    offer = client.post(
        "/api/setup/passkey/options", json={"blob": started.json()["blob"]}, headers=HEADERS
    )
    assert offer.status_code == 200, offer.text
    made = authenticator.create(offer.json()["options"], origin=ORIGIN)
    done = client.post(
        "/api/setup/complete",
        json={
            "blob": offer.json()["blob"],
            "codes_saved": True,
            "second_step": "passkey",
            "credential": made,
        },
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    return {"user": done.json(), "recovery_codes": started.json()["recovery_codes"], "client": client}


def _invitation(owner_client, *, role: str = "member", step_up_token: str | None = None) -> str:
    made = owner_client.post(
        "/api/admin/invitations",
        json={"role": role, "email": None, "household_ids": [], "step_up_token": step_up_token},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    return made.json()["link"].rsplit("/", 1)[-1]


def _begin_invited(browser, token: str, email: str, name: str) -> dict:
    started = browser.post(
        "/api/invite/begin",
        json={"token": token, "email": email, "display_name": name, "password": PASSWORD},
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    return started.json()


def _accept_with_code(client, owner_client, email: str, name: str) -> dict:
    browser = _browser(client)
    body = _begin_invited(browser, _invitation(owner_client), email, name)
    enrolled = browser.post(
        "/api/invite/enrol",
        json={"blob": body["blob"], "code": pyotp.TOTP(body["secret"]).at(int(time.time()))},
        headers=HEADERS,
    )
    assert enrolled.status_code == 200, enrolled.text
    done = browser.post(
        "/api/invite/complete", json={"blob": enrolled.json()["blob"], "codes_saved": True}, headers=HEADERS
    )
    assert done.status_code == 200, done.text
    return {"user": done.json(), "secret": body["secret"], "client": browser, "recovery_codes": body["recovery_codes"]}


def _passkey_offer(browser, blob: str) -> dict:
    offer = browser.post("/api/invite/passkey/options", json={"blob": blob}, headers=HEADERS)
    assert offer.status_code == 200, offer.text
    return offer.json()


def _accept_with_passkey(client, owner_client, email: str, name: str, authenticator) -> dict:
    browser = _browser(client)
    body = _begin_invited(browser, _invitation(owner_client), email, name)
    offer = _passkey_offer(browser, body["blob"])
    made = authenticator.create(offer["options"], origin=ORIGIN)
    done = browser.post(
        "/api/invite/complete",
        json={"blob": offer["blob"], "codes_saved": True, "second_step": "passkey", "credential": made},
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    return {"user": done.json(), "client": browser, "recovery_codes": body["recovery_codes"]}


def _passkey_proof(browser, authenticator, *, password: str = PASSWORD):
    offered = browser.post("/api/me/step-up/options", headers=HEADERS)
    assert offered.status_code == 200, offered.text
    asserted = authenticator.get(offered.json(), origin=ORIGIN)
    return browser.post(
        "/api/me/step-up", json={"password": password, "credential": asserted}, headers=HEADERS
    )


def _passkey_grant(browser, authenticator) -> str:
    answer = _passkey_proof(browser, authenticator)
    assert answer.status_code == 200, answer.text
    return answer.json()["token"]


def _code_grant(browser, secret: str, clock) -> str:
    answer = browser.post(
        "/api/me/step-up",
        json={"password": PASSWORD, "code": pyotp.TOTP(secret).at(clock())},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["token"]


def _add_authenticator(browser, *, step_up_token: str) -> str:
    offer = browser.post(
        "/api/me/authenticator", json={"current_password": PASSWORD}, headers=HEADERS
    )
    assert offer.status_code == 200, offer.text
    secret = offer.json()["secret"]
    done = browser.post(
        "/api/me/authenticator/confirm",
        json={
            "token": offer.json()["token"],
            "code": pyotp.TOTP(secret).at(int(time.time())),
            "step_up_token": step_up_token,
        },
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    return secret


def _password_step(browser, email: str) -> dict:
    answer = browser.post("/api/session", json={"email": email, "password": PASSWORD}, headers=HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _second_step_with_passkey(browser, authenticator, *, trust: bool = False):
    offered = browser.post("/api/session/second-step/options", headers=HEADERS)
    assert offered.status_code == 200, offered.text
    asserted = authenticator.get(offered.json(), origin=ORIGIN)
    return browser.post(
        "/api/session/second-step",
        json={"credential": asserted, "trust_this_browser": trust},
        headers=HEADERS,
    )


@pytest.fixture()
def world(client, clock, passkeys_on):
    jane_key = SoftAuthenticator()
    jane = _setup_with_passkey(client, jane_key)
    jane["key"] = jane_key
    sam = _accept_with_code(client, client, "sam@example.com", "Sam")
    robin_key = SoftAuthenticator()
    robin = _accept_with_passkey(client, client, "robin@example.com", "Robin", robin_key)
    robin["key"] = robin_key
    clock()
    robin["secret"] = _add_authenticator(robin["client"], step_up_token=_passkey_grant(robin["client"], robin_key))
    clock()
    return {"jane": jane, "sam": sam, "robin": robin}


# --------------------------------------------------------------------------- #
# Setup and invitations
# --------------------------------------------------------------------------- #


def test_setup_and_an_invitation_with_a_passkey_make_a_passkey_only_account(client, world):
    for who in ("jane", "robin"):
        user_id = world[who]["user"]["id"]
        [passkey] = _passkeys_of(client, user_id)
        assert passkey.rp_id == "testserver"
        assert _user(client, user_id).webauthn_user_handle == world[who]["key"].credentials[0].user_handle
        # Recovery codes belong to the account, whichever second step it chose.
        assert _codes_of(client, user_id) == 10
    assert _user(client, world["jane"]["user"]["id"]).totp_secret is None
    # Robin added an authenticator afterwards; Sam never had a passkey.
    assert _user(client, world["robin"]["user"]["id"]).totp_secret is not None
    assert _passkeys_of(client, world["sam"]["user"]["id"]) == []

    status = client.get("/api/me/authenticator", headers=HEADERS).json()
    assert status == {"enrolled": False, "locked_by_key": False, "passkeys": 1, "cleared": False}


def test_an_attestation_that_is_forged_or_replayed_creates_nobody(client, world):
    browser = _browser(client)
    body = _begin_invited(browser, _invitation(client), "alex@example.com", "Alex")
    offer = _passkey_offer(browser, body["blob"])
    key = SoftAuthenticator()
    made = key.create(offer["options"], origin=ORIGIN)

    # Forged: an attestation made for another origin fails verification.
    forged = SoftAuthenticator().create(offer["options"], origin="https://elsewhere.example")
    refused = browser.post(
        "/api/invite/complete",
        json={"blob": offer["blob"], "codes_saved": True, "second_step": "passkey", "credential": forged},
        headers=HEADERS,
    )
    assert (refused.status_code, refused.json()["code"]) == (422, "passkey.not_registered")
    with _db(client) as own:
        assert own.execute(select(User).where(User.email == "alex@example.com")).first() is None

    # The forged one spent the challenge, so the genuine answer to it is now
    # a replay too; a fresh offer is needed.
    replayed = browser.post(
        "/api/invite/complete",
        json={"blob": offer["blob"], "codes_saved": True, "second_step": "passkey", "credential": made},
        headers=HEADERS,
    )
    assert (replayed.status_code, replayed.json()["code"]) == (422, "passkey.not_registered")

    # An answer to one blob's challenge is no use with another blob.
    other = _passkey_offer(browser, body["blob"])
    crossed = browser.post(
        "/api/invite/complete",
        json={"blob": other["blob"], "codes_saved": True, "second_step": "passkey", "credential": made},
        headers=HEADERS,
    )
    assert (crossed.status_code, crossed.json()["code"]) == (422, "passkey.not_registered")
    with _db(client) as own:
        assert own.execute(select(User).where(User.email == "alex@example.com")).first() is None

    fresh = _passkey_offer(browser, body["blob"])
    good = browser.post(
        "/api/invite/complete",
        json={
            "blob": fresh["blob"],
            "codes_saved": True,
            "second_step": "passkey",
            "credential": key.create(fresh["options"], origin=ORIGIN),
        },
        headers=HEADERS,
    )
    assert good.status_code == 200, good.text
    assert len(_passkeys_of(client, good.json()["id"])) == 1


def test_setup_without_a_passkey_or_a_code_does_not_finish(client, clock, passkeys_on):
    import app.auth.setup as setup_service

    started = client.post(
        "/api/setup/begin",
        json={
            "token": setup_service.current_setup_token(),
            "email": "jane@example.com",
            "display_name": "Jane",
            "password": PASSWORD,
        },
        headers=HEADERS,
    ).json()
    # "totp" without an enrolled code, and "passkey" with no attestation.
    for extra in ({}, {"second_step": "passkey"}):
        answer = client.post(
            "/api/setup/complete", json={"blob": started["blob"], "codes_saved": True, **extra}, headers=HEADERS
        )
        assert answer.status_code == 422, answer.text
    assert client.get("/api/setup/state").json()["setup_required"] is True


def test_where_passkeys_cannot_work_setup_offers_only_the_authenticator(client):
    import app.auth.setup as setup_service

    started = client.post(
        "/api/setup/begin",
        json={
            "token": setup_service.current_setup_token(),
            "email": "jane@example.com",
            "display_name": "Jane",
            "password": PASSWORD,
        },
        headers=HEADERS,
    ).json()
    refused = client.post("/api/setup/passkey/options", json={"blob": started["blob"]}, headers=HEADERS)
    assert (refused.status_code, refused.json()["code"]) == (409, "passkey.unavailable")
    assert started["secret"]


# --------------------------------------------------------------------------- #
# Step-up
# --------------------------------------------------------------------------- #


def test_a_passkey_only_owner_passes_each_gated_act_with_a_passkey(client, world, monkeypatch):
    jane = world["jane"]
    browser = client

    # An agent key, and History names the passkey that paid for it.
    house = browser.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    issued = browser.post(
        "/api/me/keys",
        json={
            "label": "receipt filer",
            "agent_name": "filer",
            "household_id": house["id"],
            "scope": "read",
            "step_up_token": _passkey_grant(browser, jane["key"]),
        },
        headers=HEADERS,
    )
    assert issued.status_code == 201, issued.text
    with _db(client) as own:
        batch = own.execute(
            select(Batch).where(Batch.household_id == house["id"]).order_by(Batch.started_at.desc())
        ).scalars().first()
        assert batch.source == {"step_up": "passkey"}
    history = browser.get(f"/api/households/{house['id']}/batches", headers=HEADERS)
    assert "Issued the key “receipt filer”, confirmed with a passkey" in history.text

    # A second passkey.
    before = len(_passkeys_of(client, jane["user"]["id"]))
    second = SoftAuthenticator()
    options = browser.post(
        "/api/me/passkeys/options",
        json={"step_up_token": _passkey_grant(browser, jane["key"])},
        headers=HEADERS,
    )
    assert options.status_code == 200, options.text
    added = browser.post(
        "/api/me/passkeys", json={"credential": second.create(options.json(), origin=ORIGIN)}, headers=HEADERS
    )
    assert added.status_code == 201, added.text
    assert len(_passkeys_of(client, jane["user"]["id"])) == before + 1

    # An owner-role invitation.
    token = _invitation(browser, role="owner", step_up_token=_passkey_grant(browser, jane["key"]))
    assert token

    # Making somebody an owner.
    promoted = browser.post(
        f"/api/admin/users/{world['sam']['user']['id']}/role",
        json={"role": "owner", "step_up_token": _passkey_grant(browser, jane["key"])},
        headers=HEADERS,
    )
    assert promoted.status_code == 200, promoted.text
    assert _user(client, world["sam"]["user"]["id"]).role.value == "owner"

    # A backup with `secret.key` in it.
    from app import config
    from app.auth import crypto

    monkeypatch.setattr(crypto, "settings", config.settings)
    made = browser.post("/api/admin/application/backups", headers=HEADERS)
    assert made.status_code == 201, made.text
    download = browser.post(
        f"/api/admin/application/backups/{made.json()['name']}/download",
        json={"include_key": True, "step_up_token": _passkey_grant(browser, jane["key"])},
        headers=HEADERS,
    )
    assert download.status_code == 200, download.text
    assert download.headers["content-type"] == "application/zip"


def test_a_passkey_grant_is_single_use_and_marked(client, world):
    token = _passkey_grant(client, world["jane"]["key"])
    assert token.startswith("pk.")
    from app import db
    from app.auth import stepup

    assert stepup.require(db.engine, token, user_id=world["jane"]["user"]["id"]) == "passkey"
    assert stepup.claim(db.engine, token, user_id=world["jane"]["user"]["id"]) is False


def test_a_passkey_step_up_still_needs_the_password(client, world):
    answer = _passkey_proof(client, world["jane"]["key"], password="not the password at all")
    assert (answer.status_code, answer.json()["code"]) == (401, "stepup.refused")


def test_a_passkey_only_owner_cannot_step_up_with_a_code(client, world, clock):
    answer = client.post(
        "/api/me/step-up", json={"password": PASSWORD, "code": "123456"}, headers=HEADERS
    )
    assert (answer.status_code, answer.json()["code"]) == (401, "stepup.refused")


def test_somebody_elses_passkey_does_not_pass_my_step_up(client, world):
    # Robin's browser asks for options, and Jane's passkey answers them.
    robin = world["robin"]["client"]
    offered = robin.post("/api/me/step-up/options", headers=HEADERS).json()
    jane_key = world["jane"]["key"]
    asserted = jane_key.get({**offered, "rpId": offered["rpId"]}, origin=ORIGIN)
    answer = robin.post(
        "/api/me/step-up", json={"password": PASSWORD, "credential": asserted}, headers=HEADERS
    )
    assert (answer.status_code, answer.json()["code"]) == (401, "stepup.refused")


def test_an_authenticator_only_member_steps_up_as_today(client, world, clock):
    sam = world["sam"]
    token = _code_grant(sam["client"], sam["secret"], clock)
    assert token.startswith("cd.")
    # Sam has no passkey, so there is nothing to offer.
    refused = sam["client"].post("/api/me/step-up/options", headers=HEADERS)
    assert (refused.status_code, refused.json()["code"]) == (409, "passkey.none_here")


def test_an_account_with_both_steps_up_with_either(client, world, clock):
    robin = world["robin"]
    assert _passkey_grant(robin["client"], robin["key"]).startswith("pk.")
    assert _code_grant(robin["client"], robin["secret"], clock).startswith("cd.")


# --------------------------------------------------------------------------- #
# Signing in with the password, then the second step
# --------------------------------------------------------------------------- #


def test_a_passkey_only_account_signs_in_with_password_and_passkey_never_a_code(client, world):
    browser = _browser(client)
    first = _password_step(browser, "jane@example.com")
    assert first["needs_code"] is True
    assert first["second_steps"] == ["passkey"]

    # A code is refused with its own sentence -- not "cleared by a reset" --
    # and before the half-finished sign-in is spent.
    code = browser.post("/api/session/code", json={"code": "123456"}, headers=HEADERS)
    assert (code.status_code, code.json()["code"]) == (401, "auth.no_authenticator")
    assert len(_rows_pending(client)) == 1

    done = _second_step_with_passkey(browser, world["jane"]["key"])
    assert done.status_code == 200, done.text
    assert done.json()["authenticated"] is True
    assert browser.get("/api/me", headers=HEADERS).json()["id"] == world["jane"]["user"]["id"]
    assert _rows_pending(client) == []


def _rows_pending(client) -> list:
    with _db(client) as own:
        return list(own.execute(select(PendingSignIn)).scalars())


def test_the_second_step_takes_only_the_named_members_passkey(client, world):
    browser = _browser(client)
    _password_step(browser, "robin@example.com")
    offered = browser.post("/api/session/second-step/options", headers=HEADERS).json()
    asserted = world["jane"]["key"].get(offered, origin=ORIGIN)
    answer = browser.post("/api/session/second-step", json={"credential": asserted}, headers=HEADERS)
    assert answer.status_code == 401
    assert browser.get("/api/me", headers=HEADERS).status_code == 401


def test_an_account_with_both_is_offered_both_and_a_passkey_can_trust_the_browser(client, world):
    browser = _browser(client)
    assert _password_step(browser, "robin@example.com")["second_steps"] == ["passkey", "code"]
    devices = len(_trusted(client, world["robin"]["user"]["id"]))
    done = _second_step_with_passkey(browser, world["robin"]["key"], trust=True)
    assert done.status_code == 200, done.text
    assert len(_trusted(client, world["robin"]["user"]["id"])) == devices + 1
    # The trusted browser skips the second step next time, as it does today.
    browser.delete("/api/session", headers=HEADERS)
    again = _password_step(browser, "robin@example.com")
    assert again["authenticated"] is True


def _trusted(client, user_id: str) -> list:
    with _db(client) as own:
        return list(own.execute(select(TrustedDevice).where(TrustedDevice.user_id == user_id)).scalars())


def test_an_authenticator_only_member_signs_in_as_today(client, world, clock):
    browser = _browser(client)
    first = _password_step(browser, "sam@example.com")
    assert first["second_steps"] == ["code"]
    clock()
    done = browser.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(world["sam"]["secret"]).at(int(time.time())), "trust_this_browser": False},
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    assert done.json()["user"]["id"] == world["sam"]["user"]["id"]


def test_a_passkey_only_account_redeems_a_recovery_code(client, world):
    browser = _browser(client)
    _password_step(browser, "jane@example.com")
    code = world["jane"]["recovery_codes"][0]
    done = browser.post("/api/session/recovery", json={"code": code}, headers=HEADERS)
    assert done.status_code == 200, done.text
    assert done.json()["passkeys_live"] == 1
    assert _codes_of(client, world["jane"]["user"]["id"]) == 10  # used, still listed


# --------------------------------------------------------------------------- #
# Removing second factors
# --------------------------------------------------------------------------- #


def test_the_last_passkey_of_a_passkey_only_account_cannot_be_removed(client, world):
    [only] = _passkeys_of(client, world["jane"]["user"]["id"])
    refused = client.delete(f"/api/me/passkeys/{only.id}", headers=HEADERS)
    assert (refused.status_code, refused.json()["code"]) == (409, "profile.last_second_factor")
    assert [one.id for one in _passkeys_of(client, world["jane"]["user"]["id"])] == [only.id]
    # Nor is there an authenticator to remove.
    nothing = client.delete("/api/me/authenticator", headers=HEADERS)
    assert (nothing.status_code, nothing.json()["code"]) == (422, "profile.no_authenticator")


def test_an_authenticator_only_member_cannot_remove_the_authenticator(client, world):
    sam = world["sam"]
    refused = sam["client"].delete("/api/me/authenticator", headers=HEADERS)
    assert (refused.status_code, refused.json()["code"]) == (409, "profile.last_second_factor")
    assert _user(client, sam["user"]["id"]).totp_secret is not None


def test_an_account_with_both_removes_either_but_not_both(client, world):
    robin = world["robin"]
    user_id = robin["user"]["id"]
    # Removing the authenticator: the passkey remains, and so do the codes.
    codes = _codes_of(client, user_id)
    removed = robin["client"].delete("/api/me/authenticator", headers=HEADERS)
    assert removed.status_code == 200, removed.text
    assert _user(client, user_id).totp_secret is None
    assert _codes_of(client, user_id) == codes
    status = robin["client"].get("/api/me/authenticator", headers=HEADERS).json()
    assert (status["enrolled"], status["cleared"], status["passkeys"]) == (False, False, 1)
    # ...and now the passkey is the last one.
    [only] = _passkeys_of(client, user_id)
    refused = robin["client"].delete(f"/api/me/passkeys/{only.id}", headers=HEADERS)
    assert (refused.status_code, refused.json()["code"]) == (409, "profile.last_second_factor")
    assert len(_passkeys_of(client, user_id)) == 1


def test_an_account_with_both_may_remove_its_passkey_and_keep_the_authenticator(client, world):
    robin = world["robin"]
    [only] = _passkeys_of(client, robin["user"]["id"])
    removed = robin["client"].delete(f"/api/me/passkeys/{only.id}", headers=HEADERS)
    assert removed.status_code == 204, removed.text
    assert _passkeys_of(client, robin["user"]["id"]) == []
    assert _user(client, robin["user"]["id"]).totp_secret is not None


def test_a_passkey_only_account_makes_new_recovery_codes_with_a_step_up(client, world):
    user_id = world["jane"]["user"]["id"]
    with _db(client) as own:
        before = {row.code_hash for row in own.execute(select(RecoveryCode).where(RecoveryCode.user_id == user_id)).scalars()}
    answer = client.post(
        "/api/me/recovery-codes",
        json={"password": "", "step_up_token": _passkey_grant(client, world["jane"]["key"])},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    assert len(answer.json()["codes"]) == 10
    with _db(client) as own:
        after = {row.code_hash for row in own.execute(select(RecoveryCode).where(RecoveryCode.user_id == user_id)).scalars()}
    assert len(after) == 10 and not (before & after)


# --------------------------------------------------------------------------- #
# Resets (#284) and a replaced key (#287)
# --------------------------------------------------------------------------- #


def _reset(client, user_id: str, *, password: bool, authenticator: bool) -> str:
    answer = client.post(
        f"/api/admin/users/{user_id}/reset",
        json={"password": password, "authenticator": authenticator},
        headers=HEADERS,
    )
    assert answer.status_code in (200, 201), answer.text
    return answer.json()["link"].rsplit("/", 1)[-1]


def test_a_reset_shuts_every_way_in_and_reads_as_cleared_not_passkey_only(client, world):
    robin_id = world["robin"]["user"]["id"]
    _reset(client, robin_id, password=False, authenticator=True)
    user = _user(client, robin_id)
    assert user.totp_secret is None
    assert _passkeys_of(client, robin_id) == []
    assert _codes_of(client, robin_id) == 0

    browser = _browser(client)
    first = _password_step(browser, "robin@example.com")
    assert (first["needs_code"], first["second_steps"]) == (True, [])
    code = browser.post("/api/session/code", json={"code": "123456"}, headers=HEADERS)
    assert (code.status_code, code.json()["code"]) == (401, "auth.authenticator_cleared")
    _password_step(browser, "robin@example.com")
    recovery = browser.post(
        "/api/session/recovery", json={"code": world["robin"]["recovery_codes"][1]}, headers=HEADERS
    )
    assert (recovery.status_code, recovery.json()["code"]) == (401, "auth.authenticator_cleared")


def test_a_password_only_reset_of_a_passkey_only_account_covers_its_second_step(client, world):
    # Jane cannot reset herself, so a second owner does it: Sam, promoted.
    sam = world["sam"]
    jane_id = world["jane"]["user"]["id"]
    promoted = client.post(
        f"/api/admin/users/{sam['user']['id']}/role",
        json={"role": "owner", "step_up_token": _passkey_grant(client, world["jane"]["key"])},
        headers=HEADERS,
    )
    assert promoted.status_code == 200, promoted.text
    token = _reset(sam["client"], jane_id, password=True, authenticator=False)
    with _db(client) as own:
        [row] = own.execute(select(AccountReset).where(AccountReset.user_id == jane_id)).scalars()
        assert (row.password, row.authenticator) == (True, True)
    assert _passkeys_of(client, jane_id) == []

    # The link lets her enrol a passkey in place of the authenticator.
    browser = _browser(client)
    offered = browser.post(f"/api/reset/{token}/passkey", headers=HEADERS)
    assert offered.status_code == 200, offered.text
    key = SoftAuthenticator()
    made = key.create(offered.json()["options"], origin=ORIGIN)
    handle_before = _user(client, jane_id).webauthn_user_handle
    done = browser.post(
        f"/api/reset/{token}",
        json={
            "password": PASSWORD + " again",
            "second_step": "passkey",
            "blob": offered.json()["blob"],
            "credential": made,
        },
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    assert len(done.json()["recovery_codes"]) == 10
    [passkey] = _passkeys_of(client, jane_id)
    assert passkey.credential_id == key.credentials[0].id
    # The same user handle as before the reset.
    assert _user(client, jane_id).webauthn_user_handle == handle_before
    assert _user(client, jane_id).totp_secret is None


def test_a_replaced_key_does_not_lock_a_passkey_only_account(client, world, monkeypatch):
    from app.auth import crypto

    monkeypatch.setattr(crypto, "opens_totp_secret", lambda *_a, **_k: False)
    # Jane: never asked for a recovery code.
    browser = _browser(client)
    first = _password_step(browser, "jane@example.com")
    assert (first["key_replaced"], first["second_steps"]) == (False, ["passkey"])
    assert _second_step_with_passkey(browser, world["jane"]["key"]).status_code == 200
    assert _passkey_grant(client, world["jane"]["key"]).startswith("pk.")

    # Robin, with both: in recovery mode for the authenticator, still in with the passkey.
    other = _browser(client)
    robin = _password_step(other, "robin@example.com")
    assert (robin["key_replaced"], robin["second_steps"]) == (True, ["passkey"])
    assert _second_step_with_passkey(other, world["robin"]["key"]).status_code == 200
    # And a step-up with the passkey still works, where a code would be refused.
    assert _passkey_grant(world["robin"]["client"], world["robin"]["key"]).startswith("pk.")
