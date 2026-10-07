"""Signing in with a passkey, and what resets, recovery codes, password
changes and disabling do to passkeys (#121).

Every assertion here is made by `tests/soft_authenticator.py` against the
real endpoints. The tests assert what changed: the passkey's counter and
last-used time, the session the browser was given (and the trusted-device and
pending rows it was not), the rows a reset removed and a recovery code left.

Two members, two passkeys each, as in `test_passkeys_register.py`.
"""

from __future__ import annotations

import sqlite3
import time

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Change, LoginAttempt, Passkey, PendingSignIn, TrustedDevice, WebSession
from tests.conftest import HEADERS, PASSWORD
from tests.passkey_world import OLD_HOST, ORIGIN, _ledger, _rows
from tests.soft_authenticator import SoftAuthenticator, b64url
from tests.test_recovery_codes_regenerate import _two_members

REFUSED = "that passkey cannot sign in here"


def _browser(client) -> TestClient:
    """A browser nobody has signed in on."""
    return TestClient(client.app_module.app, base_url=ORIGIN)


def _sign_in(browser, authenticator: SoftAuthenticator, *, credential=None, spoil=None):
    offered = browser.post("/api/session/passkey/options", headers=HEADERS)
    assert offered.status_code == 200, offered.text
    asserted = authenticator.get(offered.json(), origin=ORIGIN, credential=credential)
    if spoil:
        spoil(asserted)
    return browser.post("/api/session/passkey", json={"credential": asserted}, headers=HEADERS), asserted


def _passkey(client, passkey_id: str) -> Passkey:
    [row] = _rows(client, Passkey, Passkey.id == passkey_id)
    return row


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #


def test_a_passkey_is_both_factors_and_moves_its_counter(client, passkey_world):
    owner = passkey_world["owner"]
    first = owner["passkeys"][0]["id"]
    before = _passkey(client, first)
    assert (before.sign_count, before.last_used_at) == (0, None)
    devices_before = len(_rows(client, TrustedDevice))
    browser = _browser(client)

    answer, _ = _sign_in(browser, owner["synced"])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["authenticated"] is True and body["needs_code"] is False
    assert body["user"]["id"] == owner["user"]["id"]
    assert body["passkey_id"] == first
    # Signed in: this browser now holds a session that works.
    assert browser.get("/api/me", headers=HEADERS).json()["id"] == owner["user"]["id"]

    after = _passkey(client, first)
    assert after.sign_count == 1
    assert after.last_used_at is not None
    # No code step, so no half-finished sign-in; and no trusted-device row.
    assert _rows(client, PendingSignIn) == []
    assert len(_rows(client, TrustedDevice)) == devices_before

    again, _ = _sign_in(_browser(client), owner["synced"])
    assert again.status_code == 200
    assert _passkey(client, first).sign_count == 2
    assert _passkey(client, first).last_used_at >= after.last_used_at


def test_a_sign_in_writes_nothing_to_history(client, passkey_world):
    """The counter, the time and the synced flag are redacted, so a sign-in
    needs no batch and leaves no change row."""
    changes = len(_rows(client, Change, Change.table_name == "passkeys"))
    answer, _ = _sign_in(_browser(client), passkey_world["member"]["synced"])
    assert answer.status_code == 200
    assert len(_rows(client, Change, Change.table_name == "passkeys")) == changes


def test_a_replayed_assertion_is_refused(client, passkey_world):
    browser = _browser(client)
    answer, asserted = _sign_in(browser, passkey_world["owner"]["synced"])
    assert answer.status_code == 200
    sessions = len(_rows(client, WebSession))

    replay = _browser(client).post("/api/session/passkey", json={"credential": asserted}, headers=HEADERS)
    assert replay.status_code == 401
    assert replay.json()["detail"] == REFUSED
    assert len(_rows(client, WebSession)) == sessions
    assert _passkey(client, passkey_world["owner"]["passkeys"][0]["id"]).sign_count == 1


def test_member_as_credential_is_refused_for_member_b(client, passkey_world):
    """Two ways to try: A's assertion claiming B's user handle, and B's
    authenticator signing for A's credential id. Neither signs anybody in."""
    owner, member = passkey_world["owner"], passkey_world["member"]
    with Session(client.app_module.db_engine) as own:
        from app.models import User

        their_handle = own.get(User, member["user"]["id"]).webauthn_user_handle

    def claim_bs_handle(asserted):
        asserted["response"]["userHandle"] = b64url(their_handle)

    def as_owners_credential(asserted):
        asserted["id"] = asserted["rawId"] = owner["synced"].credentials[0].id

    for attempt in (
        lambda: _sign_in(_browser(client), owner["synced"], spoil=claim_bs_handle),
        lambda: _sign_in(_browser(client), member["synced"], spoil=as_owners_credential),
    ):
        answer, _ = attempt()
        assert answer.status_code == 401
        assert answer.json()["detail"] == REFUSED

    assert _rows(client, WebSession, WebSession.user_id == member["user"]["id"]) != []  # their own browser
    assert _passkey(client, owner["passkeys"][0]["id"]).sign_count == 0
    assert _passkey(client, member["passkeys"][0]["id"]).sign_count == 0


def test_unknown_disabled_and_wrong_host_passkeys_all_get_one_sentence(client, passkey_world):
    owner, member = passkey_world["owner"], passkey_world["member"]
    stranger = SoftAuthenticator()
    stranger.credentials = []
    stranger.create({"rp": {"id": "testserver"}, "user": {"id": b64url(b"x" * 32)}, "challenge": b64url(b"y" * 32)},
                    origin=ORIGIN)
    unknown, _ = _sign_in(_browser(client), stranger)

    # The device-bound passkey the fixture moved to the old host name.
    old = owner["bound"].credentials[0]
    old.rp_id = "testserver"  # the authenticator signs for this host; the row says another
    wrong_host, _ = _sign_in(_browser(client), owner["bound"], credential=old)

    disabled = client.post(f"/api/admin/users/{member['user']['id']}/disabled", json={"disabled": True}, headers=HEADERS)
    assert disabled.status_code == 200, disabled.text
    off, _ = _sign_in(_browser(client), member["synced"])

    for answer in (unknown, wrong_host, off):
        assert (answer.status_code, answer.json()["detail"]) == (401, REFUSED)
    assert _passkey(client, member["passkeys"][0]["id"]).sign_count == 0
    assert _passkey(client, owner["passkeys"][1]["id"]).rp_id == OLD_HOST


def test_without_user_verification_a_passkey_is_not_both_factors(client, passkey_world):
    authenticator = passkey_world["owner"]["synced"]
    authenticator.user_verifies = False
    answer, _ = _sign_in(_browser(client), authenticator)
    assert (answer.status_code, answer.json()["detail"]) == (401, REFUSED)
    assert _passkey(client, passkey_world["owner"]["passkeys"][0]["id"]).sign_count == 0


def test_failures_have_their_own_rate_limit(client, passkey_world):
    """`passkey` is its own counter: refusals here are counted, and they are
    not counted against the password."""
    browser = _browser(client)

    def bad_signature(asserted):
        asserted["response"]["signature"] = b64url(b"\x30\x06\x02\x01\x01\x02\x01\x01")

    for _ in range(3):
        answer, _ = _sign_in(browser, passkey_world["owner"]["synced"], spoil=bad_signature)
        assert answer.status_code == 401
    kinds = [row.kind for row in _rows(client, LoginAttempt, LoginAttempt.ok.is_(False))]
    assert kinds.count("passkey") == 3 and "password" not in kinds

    # Past the budget for this account, the limiter answers before the signature is looked at.
    for _ in range(5):
        last, _ = _sign_in(browser, passkey_world["owner"]["synced"], spoil=bad_signature)
    assert last.status_code == 429


def test_nothing_is_offered_where_passkeys_are_unavailable(client, passkey_world, monkeypatch):
    import app.config as config

    monkeypatch.delenv("SPENDTRACKER_PUBLIC_URL")
    monkeypatch.setattr(config, "settings", config.Settings.from_env())
    answer = _browser(client).post("/api/session/passkey/options", headers=HEADERS)
    assert answer.status_code == 409
    from app.models import WebAuthnChallenge

    assert [row.purpose for row in _rows(client, WebAuthnChallenge)] == []


# --------------------------------------------------------------------------- #
# Recovery codes, resets, password changes
# --------------------------------------------------------------------------- #


def test_a_recovery_code_keeps_passkeys_and_says_how_many(client, passkey_world, clock):
    owner = passkey_world["owner"]
    browser = _browser(client)
    signed = browser.post("/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD}, headers=HEADERS)
    assert signed.json()["needs_code"] is True
    answer = browser.post("/api/session/recovery", json={"code": owner["recovery_codes"][0]}, headers=HEADERS)
    assert answer.status_code == 200, answer.text
    # Two passkeys, one of them for the old host: one still works here.
    assert answer.json()["passkeys_live"] == 1
    assert {row.id for row in _rows(client, Passkey, Passkey.user_id == owner["user"]["id"])} == {
        one["id"] for one in owner["passkeys"]
    }
    # And it still signs in.
    again, _ = _sign_in(_browser(client), owner["synced"])
    assert again.status_code == 200


def test_a_password_change_leaves_passkeys_alone(client, passkey_world):
    changed = client.post(
        "/api/me/password",
        json={"current_password": PASSWORD, "new_password": "another sufficiently long password"},
        headers=HEADERS,
    )
    assert changed.status_code == 200, changed.text
    assert len(_rows(client, Passkey, Passkey.user_id == passkey_world["owner"]["user"]["id"])) == 2
    answer, _ = _sign_in(_browser(client), passkey_world["owner"]["synced"])
    assert answer.status_code == 200


@pytest.mark.parametrize("what", [{"password": True}, {"authenticator": True}])
def test_an_owners_reset_link_removes_the_members_passkeys(client, passkey_world, what):
    member_id = passkey_world["member"]["user"]["id"]
    issued = client.post(
        f"/api/admin/users/{member_id}/reset",
        json={"password": False, "authenticator": False, **what},
        headers=HEADERS,
    )
    assert issued.status_code == 201, issued.text
    assert _rows(client, Passkey, Passkey.user_id == member_id) == []
    # Logged, one delete per passkey, and the owner's are untouched.
    removed = _rows(client, Change, Change.table_name == "passkeys", Change.op == "delete")
    assert {c.row_id for c in removed} == {one["id"] for one in passkey_world["member"]["passkeys"]}
    assert len(_rows(client, Passkey, Passkey.user_id == passkey_world["owner"]["user"]["id"])) == 2
    answer, _ = _sign_in(_browser(client), passkey_world["member"]["synced"])
    assert (answer.status_code, answer.json()["detail"]) == (401, REFUSED)


def test_the_operators_reset_scripts_remove_passkeys(client, passkey_world, capsys):
    """`scripts.reset_authenticator` and `scripts.reset_account` both go
    through `shut_every_door`."""
    from scripts import reset_account, reset_authenticator

    secret = pyotp.random_base32()
    asked = iter([pyotp.TOTP(secret).at(int(time.time()))])  # the clock fixture moved time.time
    import app.auth.totp as totp

    original = totp.new_secret
    totp.new_secret = lambda: secret
    try:
        assert reset_authenticator.main(["sam@example.com"], ask=lambda _: next(asked)) == 0
    finally:
        totp.new_secret = original
    out = capsys.readouterr().out
    assert "passkeys removed    2" in out
    assert _rows(client, Passkey, Passkey.user_id == passkey_world["member"]["user"]["id"]) == []

    assert reset_account.main(["Jane.Doe@gmail.com", "--password", "--yes"]) == 0
    assert _rows(client, Passkey) == []


def test_a_reset_on_an_account_with_no_passkeys_removes_none(client, clock, passkeys_on):
    from app.audit.batch import batch
    from app.models import BatchKind, User
    from app.services import profile

    people = _two_members(client)
    with Session(client.app_module.db_engine) as own:
        user = own.get(User, people["member"]["user"]["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id):
            shut = profile.shut_every_door(own, user)
        own.commit()
    assert shut.passkeys_removed == 0


def test_the_ledger_file_agrees(client, passkey_world):
    """The rows the API reports are the rows on disk."""
    with sqlite3.connect(_ledger()) as conn:
        assert conn.execute("SELECT count(*) FROM passkeys").fetchone()[0] == 4
    with Session(client.app_module.db_engine) as own:
        assert len(list(own.execute(select(Passkey)).scalars())) == 4
