"""Key handling, password verification and TOTP replay.

The properties asserted here are the ones the design turns on: an unknown email
costs the same as a wrong password, a used code is dead, and a sealed secret is
bound to the row it belongs to.
"""

from __future__ import annotations

import time

import pytest

from app.auth import crypto, passwords, totp
from app.errors import ValidationError
from app.models import User


def _user(secret: str, **kw) -> User:
    user = User(
        email="jane@example.com",
        email_canonical="jane@example.com",
        display_name="Jane",
        password_hash="x",
        totp_secret=b"placeholder",
        **kw,
    )
    user.totp_secret = crypto.seal_totp_secret(secret, user_id=user.id)
    return user


# --------------------------------------------------------------------------- #
# Sealing
# --------------------------------------------------------------------------- #


def test_a_totp_secret_round_trips():
    secret = totp.new_secret()
    sealed = crypto.seal_totp_secret(secret, user_id="abc123")
    assert sealed != secret.encode()
    assert crypto.open_totp_secret(sealed, user_id="abc123") == secret


def test_a_secret_cannot_be_opened_as_a_different_user():
    """The user id is authenticated associated data, so copying one user's
    sealed secret into another's row fails loudly instead of silently working."""
    sealed = crypto.seal_totp_secret(totp.new_secret(), user_id="user-one")
    with pytest.raises(ValidationError, match="cannot be read"):
        crypto.open_totp_secret(sealed, user_id="user-two")


def test_sealing_the_same_secret_twice_gives_different_ciphertext():
    secret = totp.new_secret()
    first = crypto.seal_totp_secret(secret, user_id="abc")
    second = crypto.seal_totp_secret(secret, user_id="abc")
    assert first != second  # a fresh nonce each time


def test_a_setup_blob_expires():
    import time as _time

    token = crypto.seal_blob('{"step": 2}')
    assert crypto.open_blob(token, max_age_seconds=900) == '{"step": 2}'
    # Fifteen minutes and one second later, the wizard has to start again.
    with pytest.raises(ValidationError, match="expired"):
        crypto.open_blob(token, max_age_seconds=900, at=int(_time.time()) + 901)


def test_a_tampered_blob_is_refused():
    token = crypto.seal_blob('{"step": 2}')
    broken = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(ValidationError):
        crypto.open_blob(broken, max_age_seconds=900)


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #


def test_a_password_verifies_against_its_own_hash():
    stored = passwords.hash_password("correct horse battery staple")
    assert passwords.verify_password(stored, "correct horse battery staple")
    assert not passwords.verify_password(stored, "nearly right")


def test_an_absent_user_verifies_false_without_short_circuiting():
    """A None hash still does the work, so "no such email" and "wrong password"
    cost the same. An early return here is a timing oracle for which addresses
    exist."""
    assert passwords.verify_password(None, "anything") is False


def test_an_unknown_email_and_a_wrong_password_take_similar_time(monkeypatch):
    # At production cost, not the test-grade hasher conftest installs: at a few
    # milliseconds a hash, scheduler noise under `pytest -n auto` swamps the
    # ratio, and what this test protects is the production timing anyway.
    import secrets

    from argon2 import PasswordHasher

    hasher = PasswordHasher()
    monkeypatch.setattr(passwords, "_hasher", hasher)
    monkeypatch.setattr(passwords, "_DUMMY_HASH", hasher.hash(secrets.token_urlsafe(32)))
    stored = passwords.hash_password("the real password")

    def _time(hash_value, attempt):
        samples = []
        for _ in range(8):
            start = time.perf_counter()
            passwords.verify_password(hash_value, attempt)
            samples.append(time.perf_counter() - start)
        samples.sort()
        return samples[len(samples) // 2]

    unknown = _time(None, "guess")
    wrong = _time(stored, "guess")

    # Asserted as a ratio, not a difference in milliseconds. Argon2 is
    # memory-hard, so absolute timings swing with whatever else the machine is
    # doing and an absolute bound flakes under a loaded CI runner. The defect
    # this guards against -- an early `return False` when the user does not
    # exist -- is not 20ms faster, it is thousands of times faster, so a
    # generous ratio catches it and nothing else does.
    ratio = unknown / wrong
    assert 0.25 < ratio < 4.0, (
        f"unknown={unknown * 1000:.1f}ms wrong={wrong * 1000:.1f}ms (ratio {ratio:.3f}). "
        "A large gap means the unknown-email path is short-circuiting, which tells an "
        "attacker which addresses exist."
    )


def test_what_is_wrong_with_a_password_is_said_in_words():
    assert passwords.complaints("short") == ["it needs at least 12 characters"]
    assert passwords.complaints("jane@example.com", email="jane@example.com") == [
        "it cannot be your email address"
    ]
    assert passwords.complaints("a sufficiently long one") == []


# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #


def test_a_code_is_accepted_once_and_then_dead():
    secret = totp.new_secret()
    user = _user(secret)
    now = int(time.time())
    code = __import__("pyotp").TOTP(secret).at(now)

    assert totp.verify_and_consume(user, code, at=now) is True
    assert user.totp_last_counter == now // totp.STEP_SECONDS
    # Same code, same window, seconds later: refused.
    assert totp.verify_and_consume(user, code, at=now + 5) is False


def test_one_step_of_drift_either_side_is_accepted():
    import pyotp

    secret = totp.new_secret()
    now = int(time.time())
    step = totp.STEP_SECONDS

    early = _user(secret)
    assert totp.verify_and_consume(early, pyotp.TOTP(secret).at(now - step), at=now) is True

    late = _user(secret)
    assert totp.verify_and_consume(late, pyotp.TOTP(secret).at(now + step), at=now) is True


def test_two_steps_of_drift_is_not():
    import pyotp

    secret = totp.new_secret()
    user = _user(secret)
    now = int(time.time())
    stale = pyotp.TOTP(secret).at(now - 2 * totp.STEP_SECONDS)
    assert totp.verify_and_consume(user, stale, at=now) is False


def test_a_code_from_an_already_used_step_is_refused_even_when_still_valid():
    """The counter only moves forward.

    Someone who captured the previous window's code cannot spend it, even
    though that code is still inside the drift window.
    """
    import pyotp

    secret = totp.new_secret()
    user = _user(secret)
    now = int(time.time())

    assert totp.verify_and_consume(user, pyotp.TOTP(secret).at(now), at=now) is True
    previous = pyotp.TOTP(secret).at(now - totp.STEP_SECONDS)
    assert totp.verify_and_consume(user, previous, at=now) is False


@pytest.mark.parametrize("junk", ["", "   ", "000000", "abcdef", "12345é", "１２３４５６"])
def test_junk_is_refused(junk):
    user = _user(totp.new_secret())
    assert totp.verify_and_consume(user, junk) is False


def test_the_provisioning_uri_names_the_app_and_the_person():
    uri = totp.provisioning_uri(totp.new_secret(), email="jane@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "Spend%20Tracker" in uri
    assert "jane%40example.com" in uri


# --------------------------------------------------------------------------- #
# Non-ASCII at the door is a refusal, never a 500 (#208)
# --------------------------------------------------------------------------- #


def test_a_non_ascii_setup_token_is_refused_and_the_instance_stays_unclaimed(client):
    from tests.conftest import HEADERS, PASSWORD

    answer = client.post(
        "/api/setup/begin",
        json={"token": "é", "email": "b@gmail.com", "display_name": "B", "password": PASSWORD},
        headers=HEADERS,
    )
    assert answer.status_code == 422, answer.text
    assert client.get("/api/health").json()["setup_required"] is True


def test_a_non_ascii_code_is_refused_and_signs_nobody_in(client, clock):
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.models import StepUpGrant
    from tests.conftest import HEADERS, PASSWORD, _setup_owner

    _setup_owner(client)

    # Step-up, while signed in: 401, and no grant exists.
    step_up = client.post(
        "/api/me/step-up", json={"password": PASSWORD, "code": "12345é"}, headers=HEADERS
    )
    assert step_up.status_code == 401, step_up.text
    with Session(client.app_module.db_engine) as own:
        assert own.execute(select(func.count()).select_from(StepUpGrant)).scalar_one() == 0

    # Signing in from an untrusted browser: the code step refuses it.
    client.delete("/api/session", headers=HEADERS)
    client.cookies.clear()
    first = client.post(
        "/api/session",
        json={"email": "janedoe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert first.json()["needs_code"] is True
    code = client.post(
        "/api/session/code", json={"code": "12345é", "trust_this_browser": False}, headers=HEADERS
    )
    assert code.status_code == 401, code.text
    assert client.get("/api/me").status_code == 401
