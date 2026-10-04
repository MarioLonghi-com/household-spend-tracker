"""Recovery mode: the server's key does not open a member's authenticator (#287).

Computed, never stored. Two members, so a member who re-enrols is seen to leave
the other exactly as they were -- the other's sealed secret byte for byte, so
that putting the original key back is what ends recovery mode for them. Each
test asserts what changed in the rows, not only what the answer said.
"""

from __future__ import annotations

import dataclasses
import secrets
import sqlite3
import time
from base64 import urlsafe_b64encode
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

from tests.conftest import HEADERS, PASSWORD, _setup_owner

OWNER = "Jane.Doe@gmail.com"
PARTNER = "partner@example.com"
PARTNER_PASSWORD = "another long password"
PARTNER_CODES = ("aaaaa11111", "bbbbb22222")


@pytest.fixture(autouse=True)
def _one_key(monkeypatch, client):
    """Seal with the key on disk, as a real instance does (test_operator_scripts)."""
    from app import config
    from app.auth import crypto

    monkeypatch.setattr(crypto, "settings", config.settings)


def _replace_the_key(monkeypatch) -> object:
    """What a lost `secret.key` does at the next boot: a new one, and the
    original kept by the test to be put back. Returns the original."""
    from app.auth import crypto

    original = crypto.settings
    fresh = urlsafe_b64encode(secrets.token_bytes(32)).decode()
    monkeypatch.setattr(crypto, "settings", dataclasses.replace(original, secret_key=fresh))
    return original


def _put_the_key_back(monkeypatch, original) -> None:
    from app.auth import crypto

    monkeypatch.setattr(crypto, "settings", original)


def _rows(sql: str, *args) -> list[tuple]:
    from app import config

    with sqlite3.connect(Path(config.settings.database_url.split("///", 1)[-1])) as conn:
        return conn.execute(sql, args).fetchall()


def _sealed(user_id: str) -> list[tuple]:
    return _rows("SELECT totp_secret, totp_last_counter FROM users WHERE id = ?", user_id)


def _secret(user_id: str) -> bytes:
    """The sealed authenticator secret alone, without the counter a used code
    moves."""
    return _sealed(user_id)[0][0]


def _unused_codes(user_id: str) -> int:
    return _rows(
        "SELECT count(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL", user_id
    )[0][0]


def _two_members(client) -> dict:
    """The owner from the real wizard (ten recovery codes), and a member with an
    authenticator and two recovery codes of their own."""
    import app.db as db
    from app.audit.batch import batch
    from app.auth import crypto, passwords, totp
    from app.models import BatchKind, RecoveryCode, Role, User

    world = _setup_owner(client)
    owner_id = world["user"]["id"]
    partner_secret = totp.new_secret()
    with db.session_scope() as session:
        with batch(session, kind=BatchKind.admin, actor_id=owner_id):
            partner = User(
                email=PARTNER,
                email_canonical=PARTNER,
                display_name="Partner",
                password_hash=passwords.hash_password(PARTNER_PASSWORD),
                role=Role.member,
                totp_secret=b"",
            )
            session.add(partner)
            session.flush()
            partner.totp_secret = crypto.seal_totp_secret(partner_secret, user_id=partner.id)
            for code in PARTNER_CODES:
                session.add(RecoveryCode(user_id=partner.id, code_hash=passwords.hash_password(code)))
        partner_id = partner.id
    return {
        **world,
        "owner_id": owner_id,
        "owner_secret": world["secret"],
        "partner_id": partner_id,
        "partner_secret": partner_secret,
    }


def _browser(client) -> TestClient:
    """Another browser on the same app: its own cookie jar, the same ledger."""
    return TestClient(client.app_module.app, base_url="https://testserver")


def _password(browser, email: str, password: str):
    answer = browser.post("/api/session", json={"email": email, "password": password}, headers=HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _code(browser, secret: str, *, trust: bool = False):
    return browser.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(secret).at(int(time.time())), "trust_this_browser": trust},
        headers=HEADERS,
    )


def _recover(browser, email: str, password: str, code: str) -> dict:
    """Password, then a recovery code: a session, and in recovery mode a grant."""
    assert _password(browser, email, password)["needs_code"] is True
    answer = browser.post("/api/session/recovery", json={"code": code}, headers=HEADERS)
    assert answer.status_code == 200 and answer.json()["authenticated"], answer.text
    return answer.json()


def _reenrol_with(browser, password: str, *, grant: str):
    """A new authenticator, with the grant standing in for the current one."""
    offer = browser.post(
        "/api/me/authenticator", json={"current_password": password}, headers=HEADERS
    )
    assert offer.status_code == 200, offer.text
    offer = offer.json()
    done = browser.post(
        "/api/me/authenticator/confirm",
        json={
            "token": offer["token"],
            "code": pyotp.TOTP(offer["secret"]).at(int(time.time())),
            "grant": grant,
        },
        headers=HEADERS,
    )
    return done, offer["secret"]


def _banner(browser) -> dict:
    """What the owners' banner is told."""
    answer = browser.get("/api/admin/recovery-mode")
    assert answer.status_code == 200, answer.text
    return answer.json()


def _counts(enrolled: int, locked: int, *, from_env: bool = False) -> dict:
    return {"enrolled": enrolled, "locked": locked, "key_from_environment": from_env}


def _locked(user_id: str) -> bool:
    import app.db as db
    from app.auth import keycheck
    from app.models import User

    with db.session_scope() as session:
        return keycheck.locked_by_key(session.get(User, user_id))


# --------------------------------------------------------------------------- #
# The whole of it, end to end
# --------------------------------------------------------------------------- #


def test_one_recovery_code_re_enrols_one_member_and_the_original_key_ends_it_for_the_other(
    client, monkeypatch, clock
):
    world = _two_members(client)
    owner_id, partner_id = world["owner_id"], world["partner_id"]
    partner_before = _sealed(partner_id)
    assert _unused_codes(owner_id) == 10

    original = _replace_the_key(monkeypatch)
    assert _locked(owner_id) and _locked(partner_id)

    # The owner gives the right password and a code from the old authenticator:
    # refused, with the flag the screen acts on, and no session.
    owner = _browser(client)
    assert _password(owner, OWNER, PASSWORD)["needs_code"] is True
    refused = _code(owner, world["owner_secret"])
    assert refused.status_code == 401, refused.text
    assert refused.json()["key_replaced"] is True
    assert "recovery code" in refused.json()["detail"]
    assert owner.get("/api/me").status_code == 401

    # The refusal did not spend the half-finished sign-in: ONE recovery code
    # goes straight on from it, and the answer carries the grant.
    back = owner.post(
        "/api/session/recovery", json={"code": world["recovery_codes"][0]}, headers=HEADERS
    )
    assert back.status_code == 200, back.text
    grant = back.json()["reenrolment_grant"]
    assert grant
    assert _unused_codes(owner_id) == 9

    # The grant stands in for the old authenticator: no second recovery code.
    done, new_secret = _reenrol_with(owner, PASSWORD, grant=grant)
    assert done.status_code == 200, done.text
    assert _unused_codes(owner_id) == 9, "exactly one recovery code, for sign-in and re-enrolment both"
    assert not _locked(owner_id), "the new key opens the re-enrolled member"

    # The partner is still in recovery mode, and their attempt at the code
    # step wrote nothing.
    partner = _browser(client)
    _password(partner, PARTNER, PARTNER_PASSWORD)
    assert _code(partner, world["partner_secret"]).json()["key_replaced"] is True
    assert _locked(partner_id)
    assert _sealed(partner_id) == partner_before, "the old ciphertext must survive byte for byte"
    assert _unused_codes(partner_id) == len(PARTNER_CODES)

    # The owner's banner counts the one left.
    assert _banner(owner) == _counts(2, 1)

    # The original key back: the partner, who never re-enrolled, signs in with
    # the authenticator they always had. The owner, re-enrolled under the
    # replacement, is now the one it cannot open.
    _put_the_key_back(monkeypatch, original)
    clock(1)
    assert not _locked(partner_id)
    assert _locked(owner_id)

    partner = _browser(client)
    assert _password(partner, PARTNER, PARTNER_PASSWORD)["needs_code"] is True
    signed = _code(partner, world["partner_secret"])
    assert signed.status_code == 200, signed.text
    assert signed.json()["user"]["id"] == partner_id

    again = _browser(client)
    _password(again, OWNER, PASSWORD)
    stale = _code(again, new_secret)
    assert stale.status_code == 401 and stale.json()["key_replaced"] is True
    assert _banner(owner) == _counts(2, 1)


# --------------------------------------------------------------------------- #
# Sign-in
# --------------------------------------------------------------------------- #


def test_a_trusted_browser_does_not_skip_the_code_in_recovery_mode(client, monkeypatch):
    world = _two_members(client)
    partner = _browser(client)
    _password(partner, PARTNER, PARTNER_PASSWORD)
    assert _code(partner, world["partner_secret"], trust=True).status_code == 200
    partner.delete("/api/session", headers=HEADERS)

    # Trusted: the password alone signs this browser in, while the key opens it.
    assert _password(partner, PARTNER, PARTNER_PASSWORD)["authenticated"] is True
    partner.delete("/api/session", headers=HEADERS)

    _replace_the_key(monkeypatch)
    state = _password(partner, PARTNER, PARTNER_PASSWORD)
    assert (state["authenticated"], state["needs_code"]) == (False, True)
    assert partner.get("/api/me").status_code == 401, "password alone never yields a session"
    assert _code(partner, world["partner_secret"]).json()["key_replaced"] is True


def _a_household_and_a_key_each(client, world) -> dict[str, str]:
    """A household both members are in, and a live agent key for each: the
    tokens, by member id. Made through the service rather than a step-up --
    what is under test is what a recovery code does to them, not how they
    were issued."""
    import app.db as db
    from app.audit.batch import batch
    from app.models import BatchKind, Household, HouseholdMember, User, utcnow
    from app.services import agent_keys

    made = client.post("/api/households", json={"name": "Home"}, headers=HEADERS)
    assert made.status_code == 201, made.text
    tokens = {}
    with db.session_scope() as session:
        home = session.get(Household, made.json()["id"])
        with batch(session, kind=BatchKind.admin, actor_id=world["owner_id"]):
            session.add(
                HouseholdMember(
                    household_id=home.id,
                    user_id=world["partner_id"],
                    added_by_id=world["owner_id"],
                    added_at=utcnow(),
                )
            )
            session.flush()
            for user_id in (world["owner_id"], world["partner_id"]):
                _, token = agent_keys.issue(
                    session, user=session.get(User, user_id), household=home, label="filer"
                )
                tokens[user_id] = token
    return tokens


def _key_works(client, token: str) -> bool:
    answer = _browser(client).get(
        "/api/agent/v1/manifest", headers={"authorization": f"Bearer {token}"}
    )
    assert answer.status_code in (200, 401), answer.text
    return answer.status_code == 200


def _trusted(user_id: str) -> int:
    return _rows("SELECT count(*) FROM trusted_devices WHERE user_id = ?", user_id)[0][0]


def test_the_recovery_code_a_trusted_browser_is_sent_to_costs_its_trust_and_keys_for_good(
    client, monkeypatch, clock
):
    """What a lost key costs, as SECURITY.md and the README now say it.

    The key going signs nobody out: a session and both agent keys work beside
    the new one. But a trusted browser no longer skips the code in recovery
    mode, so the member who signs in there spends a recovery code, and that
    revokes their sessions, trusted browsers and agent keys as a recovery
    code always has. Putting the original key back ends recovery mode and
    returns none of it. The partner, who did not sign in meanwhile, keeps all
    of theirs -- including the trusted browser skipping the code again.
    """
    world = _two_members(client)
    owner_id, partner_id = world["owner_id"], world["partner_id"]
    keys = _a_household_and_a_key_each(client, world)

    owner = _browser(client)
    _password(owner, OWNER, PASSWORD)
    clock(1)
    assert _code(owner, world["owner_secret"], trust=True).status_code == 200
    partner = _browser(client)
    _password(partner, PARTNER, PARTNER_PASSWORD)
    assert _code(partner, world["partner_secret"], trust=True).status_code == 200
    for browser in (owner, partner):
        browser.delete("/api/session", headers=HEADERS)
    # The owner's two: the wizard's browser, and this one.
    assert (_trusted(owner_id), _trusted(partner_id)) == (2, 1)

    original = _replace_the_key(monkeypatch)
    assert client.get("/api/me").status_code == 200, "a lost key signs nobody out"
    assert _key_works(client, keys[owner_id]) and _key_works(client, keys[partner_id])

    # The owner's trusted browser is sent to a recovery code, and it costs.
    state = _password(owner, OWNER, PASSWORD)
    assert (state["authenticated"], state["key_replaced"]) == (False, True)
    back = owner.post(
        "/api/session/recovery", json={"code": world["recovery_codes"][0]}, headers=HEADERS
    )
    assert back.status_code == 200 and back.json()["keys_revoked"] == 1, back.text
    assert not _key_works(client, keys[owner_id])
    assert _trusted(owner_id) == 0
    assert client.get("/api/me").status_code == 401, "signed out everywhere"
    assert _key_works(client, keys[partner_id]) and _trusted(partner_id) == 1

    # The original key back: recovery mode is over, and nothing comes back.
    _put_the_key_back(monkeypatch, original)
    clock(1)
    assert not _locked(owner_id) and not _locked(partner_id)
    assert not _key_works(client, keys[owner_id]), "the original key does not unrevoke a key"
    owner.delete("/api/session", headers=HEADERS)
    again = _password(owner, OWNER, PASSWORD)
    assert (again["authenticated"], again["needs_code"], again["key_replaced"]) == (
        False,
        True,
        False,
    ), "the trust is gone, not merely suspended"

    assert _password(partner, PARTNER, PARTNER_PASSWORD)["authenticated"] is True
    assert _key_works(client, keys[partner_id]) and _trusted(partner_id) == 1


def test_a_cleared_authenticator_is_not_recovery_mode():
    """NULL is a reset (#284), with its own refusal; nothing to open is not a
    key that cannot open it."""
    from app.auth import keycheck
    from app.models import User

    assert keycheck.locked_by_key(User(id="someone", totp_secret=None)) is False


def test_a_cleared_authenticator_beside_a_replaced_key_is_its_own_state_everywhere(
    client, monkeypatch, clock
):
    """The partner's authenticator cleared by a reset link (#284), then the key
    replaced: the owner is in recovery mode, the partner is not. The password
    step does not flag the partner, the code step gives them the reset's
    sentence, and the banner and the boot check count only the owner --
    answering, not failing: a NULL secret handed to the key is a TypeError,
    and the banner's filter on it was guarded by nothing but an unsaved
    `User` above. The partner's row stays NULL throughout."""
    import app.db as db
    from app import config
    from app.audit.batch import batch
    from app.auth import crypto, keycheck, service
    from app.models import BatchKind, User
    from app.services import account_resets

    world = _two_members(client)
    owner_id, partner_id = world["owner_id"], world["partner_id"]
    with (
        db.session_scope() as session,
        batch(session, kind=BatchKind.admin, actor_id=owner_id),
    ):
        account_resets.issue(
            session,
            session.get(User, partner_id),
            password=False,
            authenticator=True,
            by=session.get(User, owner_id),
        )
    assert _sealed(partner_id) == [(None, None)]
    assert _banner(client) == _counts(1, 0)

    _replace_the_key(monkeypatch)
    assert _locked(owner_id) and not _locked(partner_id)
    assert _banner(client) == _counts(1, 1)
    ledger = Path(config.settings.database_url.split("///", 1)[-1])
    booted = keycheck.check(ledger, crypto.settings.secret_key)
    assert (booted.enrolled, booted.refused) == (1, (OWNER,))

    partner = _browser(client)
    state = _password(partner, PARTNER, PARTNER_PASSWORD)
    assert (state["needs_code"], state["key_replaced"], state["detail"]) == (True, False, None)
    cleared = _code(partner, world["partner_secret"])
    assert cleared.status_code == 401
    assert cleared.json() == {"detail": service.AUTHENTICATOR_CLEARED}, "no key_replaced"
    assert partner.get("/api/me").status_code == 401

    # The owner re-enrols; the banner empties, and the partner is still not in it.
    owner = _browser(client)
    grant = _recover(owner, OWNER, PASSWORD, world["recovery_codes"][0])["reenrolment_grant"]
    done, _ = _reenrol_with(owner, PASSWORD, grant=grant)
    assert done.status_code == 200, done.text
    clock(1)
    assert _banner(owner) == _counts(1, 0)
    assert _sealed(partner_id) == [(None, None)]


def _one_locked_one_not(client, monkeypatch, clock) -> dict:
    """The key replaced, and the owner already re-enrolled under the new one:
    the owner is opened by the key, the partner is not."""
    world = _two_members(client)
    _replace_the_key(monkeypatch)
    owner = _browser(client)
    grant = _recover(owner, OWNER, PASSWORD, world["recovery_codes"][0])["reenrolment_grant"]
    done, new_secret = _reenrol_with(owner, PASSWORD, grant=grant)
    assert done.status_code == 200, done.text
    clock(1)
    assert not _locked(world["owner_id"]) and _locked(world["partner_id"])
    return {**world, "owner": owner, "owner_new_secret": new_secret}


def test_the_password_step_already_says_the_key_was_replaced_and_only_for_that_member(
    client, monkeypatch, clock
):
    """Step one flags a member the key cannot open -- once the password is
    right, and never for one it opens -- and the recovery code goes on from
    that same half-finished sign-in without the code step ever being tried."""
    from app.auth import service

    world = _one_locked_one_not(client, monkeypatch, clock)
    owner_sealed = _secret(world["owner_id"])
    partner_before = _sealed(world["partner_id"])

    opened = _password(_browser(client), OWNER, PASSWORD)
    assert (opened["needs_code"], opened["key_replaced"], opened["detail"]) == (True, False, None)

    partner = _browser(client)
    wrong = partner.post(
        "/api/session", json={"email": PARTNER, "password": "not the password"}, headers=HEADERS
    )
    assert wrong.status_code == 401
    assert "key_replaced" not in wrong.json(), "nothing is said before the password is proven"

    flagged = _password(partner, PARTNER, PARTNER_PASSWORD)
    assert (flagged["authenticated"], flagged["needs_code"]) == (False, True)
    assert flagged["key_replaced"] is True
    assert flagged["detail"] == service.KEY_REPLACED
    assert partner.get("/api/me").status_code == 401, "the flag is not a session"

    back = partner.post("/api/session/recovery", json={"code": PARTNER_CODES[0]}, headers=HEADERS)
    assert back.status_code == 200, back.text
    assert back.json()["user"]["id"] == world["partner_id"]
    assert back.json()["reenrolment_grant"]
    assert _unused_codes(world["partner_id"]) == len(PARTNER_CODES) - 1
    assert _sealed(world["partner_id"]) == partner_before
    assert _secret(world["owner_id"]) == owner_sealed


def test_a_step_up_by_a_member_the_key_cannot_open_says_why_and_keeps_the_session(
    client, monkeypatch, clock
):
    """It used to be a 422 about "this server key". Now it is a refused proof
    with the flag -- the same answer whatever password came with it, so it is
    no oracle for one -- and nothing is counted, granted or written. The member
    the key opens steps up as always.

    Its own sentence, not the sign-in's: a step-up takes no recovery code, so
    the sign-in's "use one of your recovery codes" sent the member to type
    one, and the recovery code got that same refusal back, unspent. The
    sentence says the profile is the way on, and the unused recovery code is
    still unused after being tried here."""
    from app.auth import service

    world = _one_locked_one_not(client, monkeypatch, clock)
    owner_sealed = _secret(world["owner_id"])
    partner = _browser(client)
    _recover(partner, PARTNER, PARTNER_PASSWORD, PARTNER_CODES[0])
    partner_before = _sealed(world["partner_id"])
    attempts = _rows("SELECT count(*) FROM login_attempts WHERE kind = 'stepup'")[0][0]
    unused = _unused_codes(world["partner_id"])

    old_code = pyotp.TOTP(world["partner_secret"]).at(int(time.time()))
    answers = [
        partner.post(
            "/api/me/step-up", json={"password": password, "code": code}, headers=HEADERS
        )
        for password, code in (
            (PARTNER_PASSWORD, old_code),
            ("not the password", old_code),
            # What the sign-in's sentence told them to do.
            (PARTNER_PASSWORD, PARTNER_CODES[1]),
        )
    ]
    for refused in answers:
        assert refused.status_code == 401, refused.text
        assert refused.headers.get("x-refused") == "proof"
        assert refused.json() == {"detail": service.KEY_REPLACED_STEP_UP, "key_replaced": True}
    said = answers[-1].json()["detail"]
    assert "Use one of your recovery codes" not in said
    assert "recovery code does not stand in" in said and "Set up a new authenticator" in said
    assert partner.get("/api/me").status_code == 200, "a refused proof is not a signed-out session"
    assert _rows(
        "SELECT count(*) FROM step_up_grants WHERE user_id = ?", world["partner_id"]
    ) == [(0,)]
    assert _rows("SELECT count(*) FROM login_attempts WHERE kind = 'stepup'")[0][0] == attempts
    assert _unused_codes(world["partner_id"]) == unused == 1, "a step-up spends no recovery code"
    assert _sealed(world["partner_id"]) == partner_before

    stepped = world["owner"].post(
        "/api/me/step-up",
        json={
            "password": PASSWORD,
            "code": pyotp.TOTP(world["owner_new_secret"]).at(int(time.time())),
        },
        headers=HEADERS,
    )
    assert stepped.status_code == 200, stepped.text
    assert _rows(
        "SELECT count(*) FROM step_up_grants WHERE user_id = ?", world["owner_id"]
    ) == [(1,)]
    assert _secret(world["owner_id"]) == owner_sealed


def test_without_the_grant_the_profile_takes_a_recovery_code_and_says_why_not_six_digits(
    client, monkeypatch, clock
):
    """The member who lost the grant -- another browser, or a day on -- still
    re-enrols from the profile, with a recovery code. Six digits from the old
    authenticator get a sentence asking for a recovery code in their place,
    not a 422, and spend nothing.

    "Nothing" includes the step-up's rate-limit budget, which the password
    re-checks share: the refusal comes before the limiter, so no attempt row
    is left. Were it after, a few tries from the old authenticator would lock
    the member out of changing their password too -- so the rows are counted
    over more tries than the budget allows, and the password re-check after
    them still works."""
    from app.auth import ratelimit, service

    world = _one_locked_one_not(client, monkeypatch, clock)
    owner_sealed = _secret(world["owner_id"])
    partner = _browser(client)
    _recover(partner, PARTNER, PARTNER_PASSWORD, PARTNER_CODES[0])
    partner_before = _sealed(world["partner_id"])
    counted = "SELECT count(*) FROM login_attempts WHERE kind = 'stepup'"
    attempts = _rows(counted)[0][0]

    def reenrol(current_code: str):
        # The password re-check: what a spent budget would refuse first.
        offer = partner.post(
            "/api/me/authenticator", json={"current_password": PARTNER_PASSWORD}, headers=HEADERS
        )
        assert offer.status_code == 200, offer.text
        offer = offer.json()
        return partner.post(
            "/api/me/authenticator/confirm",
            json={
                "token": offer["token"],
                "code": pyotp.TOTP(offer["secret"]).at(int(time.time())),
                "current_code": current_code,
            },
            headers=HEADERS,
        )

    old_code = pyotp.TOTP(world["partner_secret"]).at(int(time.time()))
    for _ in range(ratelimit.FREE_ATTEMPTS + 1):
        six = reenrol(old_code)
        assert six.status_code == 401 and six.json()["key_replaced"] is True, six.text
        assert six.json()["detail"] == service.KEY_REPLACED_REENROL
        assert six.headers.get("x-refused") == "proof"
    assert _rows(counted)[0][0] == attempts, "refused before the limiter: nothing counted"
    assert _sealed(world["partner_id"]) == partner_before
    assert _unused_codes(world["partner_id"]) == len(PARTNER_CODES) - 1

    clock(1)
    done = reenrol(PARTNER_CODES[1])
    assert done.status_code == 200, done.text
    assert _unused_codes(world["partner_id"]) == 0, "the second code paid for it"
    assert not _locked(world["partner_id"])
    assert _secret(world["owner_id"]) == owner_sealed


# --------------------------------------------------------------------------- #
# The grant
# --------------------------------------------------------------------------- #


def test_a_grant_is_refused_for_another_member_and_another_session(client, monkeypatch):
    world = _two_members(client)
    partner_before = _sealed(world["partner_id"])
    _replace_the_key(monkeypatch)

    owner = _browser(client)
    owner_grant = _recover(owner, OWNER, PASSWORD, world["recovery_codes"][0])["reenrolment_grant"]
    partner = _browser(client)
    _recover(partner, PARTNER, PARTNER_PASSWORD, PARTNER_CODES[0])

    # The owner's grant, in the partner's session -- and one naming the owner
    # but bound to the partner's session, so the member check is what refuses
    # it rather than the session check.
    theirs, _ = _reenrol_with(partner, PARTNER_PASSWORD, grant=owner_grant)
    assert theirs.status_code == 401 and theirs.headers.get("x-refused") == "proof", theirs.text
    named, _ = _reenrol_with(
        partner, PARTNER_PASSWORD, grant=_grant_for(world["owner_id"], partner)
    )
    assert named.status_code == 401, named.text
    assert _sealed(world["partner_id"]) == partner_before
    assert partner.get("/api/me").status_code == 200, "a refused proof is not a signed-out session"

    # The owner's own grant, in a later session of the owner's.
    later = _browser(client)
    _recover(later, OWNER, PASSWORD, world["recovery_codes"][1])
    owner_before = _sealed(world["owner_id"])
    elsewhere, _ = _reenrol_with(later, PASSWORD, grant=owner_grant)
    assert elsewhere.status_code == 401, elsewhere.text
    assert _sealed(world["owner_id"]) == owner_before
    assert _locked(world["owner_id"])


def test_a_grant_outlives_the_old_fifteen_minutes_and_not_a_day(client, monkeypatch, clock):
    """Two grants sealed at the same moment: the owner's is spent a minute
    short of a day -- far past the fifteen minutes it used to have -- and the
    partner's thirty seconds past the day is refused, writing nothing."""
    from app.services import profile

    assert profile.KEY_RECOVERY_SECONDS == 24 * 60 * 60
    world = _two_members(client)
    _replace_the_key(monkeypatch)
    owner = _browser(client)
    owner_grant = _recover(owner, OWNER, PASSWORD, world["recovery_codes"][0])["reenrolment_grant"]
    partner = _browser(client)
    partner_grant = _recover(partner, PARTNER, PARTNER_PASSWORD, PARTNER_CODES[0])[
        "reenrolment_grant"
    ]
    owner_before, partner_before = _sealed(world["owner_id"]), _sealed(world["partner_id"])

    clock(2 * 60 * 24 - 2)  # 23 hours 59 minutes
    kept, _ = _reenrol_with(owner, PASSWORD, grant=owner_grant)
    assert kept.status_code == 200, kept.text
    assert _secret(world["owner_id"]) != owner_before[0][0], "a new authenticator was sealed"
    assert not _locked(world["owner_id"])
    assert _unused_codes(world["owner_id"]) == 9, "still the one recovery code"

    clock(3)  # 24 hours 30 seconds
    late, _ = _reenrol_with(partner, PARTNER_PASSWORD, grant=partner_grant)
    assert late.status_code == 401 and "expired" in late.json()["detail"], late.text
    assert late.headers.get("x-refused") == "proof", "a refused grant leaves the session"
    assert partner.get("/api/me").status_code == 200
    assert _sealed(world["partner_id"]) == partner_before
    assert _locked(world["partner_id"])
    assert _unused_codes(world["partner_id"]) == len(PARTNER_CODES) - 1


def test_a_grant_put_off_at_sign_in_is_spent_later_from_the_profile(client, monkeypatch, clock):
    """"Not now" on the sign-in screen, and an hour later the profile's "Set up
    a new authenticator" spends the same grant: still one recovery code.

    `GET /me/authenticator` is what tells the profile to send the grant, and
    it describes the caller only -- the two members are told different things
    once one has re-enrolled.
    """
    world = _two_members(client)
    partner_before = _sealed(world["partner_id"])
    _replace_the_key(monkeypatch)
    owner = _browser(client)
    grant = _recover(owner, OWNER, PASSWORD, world["recovery_codes"][0])["reenrolment_grant"]
    partner = _browser(client)
    _recover(partner, PARTNER, PARTNER_PASSWORD, PARTNER_CODES[0])
    both = {"enrolled": True, "locked_by_key": True}
    assert owner.get("/api/me/authenticator").json() == both
    assert partner.get("/api/me/authenticator").json() == both

    clock(120)  # an hour on: the sign-in screen is long gone
    done, _ = _reenrol_with(owner, PASSWORD, grant=grant)
    assert done.status_code == 200, done.text
    assert _unused_codes(world["owner_id"]) == 9

    assert owner.get("/api/me/authenticator").json() == {"enrolled": True, "locked_by_key": False}
    assert partner.get("/api/me/authenticator").json() == both
    assert _sealed(world["partner_id"]) == partner_before
    assert _unused_codes(world["partner_id"]) == len(PARTNER_CODES) - 1


def test_the_authenticator_status_is_only_for_somebody_signed_in(client):
    _setup_owner(client)
    assert client.get("/api/me/authenticator").json() == {
        "enrolled": True,
        "locked_by_key": False,
    }
    client.cookies.clear()
    assert client.get("/api/me/authenticator").status_code == 401


def test_a_grant_is_refused_once_the_key_opens_the_member(client, monkeypatch, clock):
    """Spent by what it paid for: after the re-enrolment the key opens the
    member, and the same grant buys nothing more. Nor does one sealed for a
    member the key opens, in their own session."""
    world = _two_members(client)
    original = _replace_the_key(monkeypatch)
    owner = _browser(client)
    grant = _recover(owner, OWNER, PASSWORD, world["recovery_codes"][0])["reenrolment_grant"]
    first, _ = _reenrol_with(owner, PASSWORD, grant=grant)
    assert first.status_code == 200, first.text
    enrolled = _sealed(world["owner_id"])

    clock(1)
    second, _ = _reenrol_with(owner, PASSWORD, grant=grant)
    assert second.status_code == 401, second.text
    assert "can be checked again" in second.json()["detail"]
    assert _sealed(world["owner_id"]) == enrolled

    _put_the_key_back(monkeypatch, original)
    partner = _browser(client)
    _password(partner, PARTNER, PARTNER_PASSWORD)
    assert _code(partner, world["partner_secret"]).status_code == 200
    before = _sealed(world["partner_id"])
    unlocked, _ = _reenrol_with(
        partner, PARTNER_PASSWORD, grant=_grant_for(world["partner_id"], partner)
    )
    assert unlocked.status_code == 401, unlocked.text
    assert _sealed(world["partner_id"]) == before


def _grant_for(user_id: str, browser) -> str:
    """A grant sealed the way the recovery sign-in seals one, for this
    browser's session -- for a member the sign-in would never have given one."""
    import app.db as db
    from app.auth import cookies
    from app.models import User
    from app.services import profile

    value = browser.cookies.get(cookies.session_name())
    assert value
    with db.session_scope() as session:
        return profile.grant_key_recovery(session.get(User, user_id), session_value=value)


# --------------------------------------------------------------------------- #
# The owners' banner
# --------------------------------------------------------------------------- #


def test_the_banner_counts_the_members_the_key_does_not_open_and_is_the_owners(
    client, monkeypatch
):
    world = _two_members(client)
    assert _banner(client) == _counts(2, 0)

    partner = _browser(client)
    _password(partner, PARTNER, PARTNER_PASSWORD)
    assert _code(partner, world["partner_secret"]).status_code == 200
    assert partner.get("/api/admin/recovery-mode").status_code == 403

    _replace_the_key(monkeypatch)
    assert _banner(client) == _counts(2, 2)


def test_the_banner_says_when_the_key_came_from_the_environment(client, monkeypatch):
    """With `SPENDTRACKER_SECRET_KEY` set, `secret.key` is never read, so the
    banner has to name the variable: the payload says where the key came
    from. Two members, both locked out by a wrong value in it."""
    from app.auth import crypto

    _two_members(client)
    original = _replace_the_key(monkeypatch)
    assert _banner(client) == _counts(2, 2, from_env=False)

    monkeypatch.setattr(
        crypto,
        "settings",
        dataclasses.replace(crypto.settings, secret_key_from_env=True),
    )
    assert _banner(client) == _counts(2, 2, from_env=True)

    # The original key, from the variable: nobody is locked, and it still says where.
    monkeypatch.setattr(
        crypto, "settings", dataclasses.replace(original, secret_key_from_env=True)
    )
    assert _banner(client) == _counts(2, 0, from_env=True)


def test_a_disabled_member_is_not_counted_until_they_are_enabled_again(client, monkeypatch):
    """The banner and the boot log say each member they count "will be asked
    for a recovery code". A disabled member cannot sign in, so never will be,
    and could never re-enrol to leave the count: with a former member in the
    ledger the banner stayed up after everybody else had re-enrolled. Not
    counted while disabled -- by the banner or the boot log's check -- and
    counted again once enabled, when they are asked like anybody else. Their
    sealed secret is untouched throughout."""
    from app import config
    from app.auth import crypto, keycheck

    world = _two_members(client)
    off = client.post(
        f"/api/admin/users/{world['partner_id']}/disabled", json={"disabled": True}, headers=HEADERS
    )
    assert off.status_code == 200, off.text
    partner_before = _sealed(world["partner_id"])
    ledger = Path(config.settings.database_url.split("///", 1)[-1])

    _replace_the_key(monkeypatch)
    assert _locked(world["partner_id"]), "still sealed with the original key"
    assert _banner(client) == _counts(1, 1)
    booted = keycheck.check(ledger, crypto.settings.secret_key)
    assert (booted.enrolled, booted.refused) == (1, (OWNER,))

    # Every member who can sign in re-enrols; the disabled one cannot try.
    owner = _browser(client)
    grant = _recover(owner, OWNER, PASSWORD, world["recovery_codes"][0])["reenrolment_grant"]
    done, _ = _reenrol_with(owner, PASSWORD, grant=grant)
    assert done.status_code == 200, done.text
    refused = _browser(client).post(
        "/api/session", json={"email": PARTNER, "password": PARTNER_PASSWORD}, headers=HEADERS
    )
    assert refused.status_code == 401

    assert _banner(owner) == _counts(1, 0)
    booted = keycheck.check(ledger, crypto.settings.secret_key)
    assert (booted.enrolled, booted.refused) == (1, ())
    assert _sealed(world["partner_id"]) == partner_before

    # Enabled again: counted, and asked for a recovery code at sign-in.
    on = owner.post(
        f"/api/admin/users/{world['partner_id']}/disabled", json={"disabled": False}, headers=HEADERS
    )
    assert on.status_code == 200, on.text
    assert _banner(owner) == _counts(2, 1)
    booted = keycheck.check(ledger, crypto.settings.secret_key)
    assert (booted.enrolled, booted.refused) == (2, (PARTNER,))
    assert _password(_browser(client), PARTNER, PARTNER_PASSWORD)["key_replaced"] is True
