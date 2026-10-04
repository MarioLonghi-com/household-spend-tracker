"""Signing in, end to end through the service layer.

The brief: a password, then a code -- unless this browser passed one recently,
in which case do not bother the user again for thirty days.
"""

from __future__ import annotations

import time

import pyotp
import pytest
from sqlalchemy import select

from app.auth import crypto, devices, passwords, service, sessions, totp
from app.errors import TooManyAttempts, Unauthorized
from app.models import Role, User

PASSWORD = "a sufficiently long password"


@pytest.fixture()
def signed_up(session):
    """A real owner with a real password and a real authenticator."""
    secret = totp.new_secret()
    user = User(
        email="Jane.Doe@gmail.com",
        email_canonical="janedoe@gmail.com",
        display_name="Jane",
        password_hash=passwords.hash_password(PASSWORD),
        role=Role.owner,
        totp_secret=b"",
    )
    user.totp_secret = crypto.seal_totp_secret(secret, user_id=user.id)

    from sqlalchemy import text

    from app.audit.batch import batch
    from app.models.enums import BatchKind

    session.execute(text("PRAGMA defer_foreign_keys=ON"))
    with batch(session, kind=BatchKind.setup, actor_id=user.id, own_transaction=False):
        session.add(user)
    session.commit()
    return user, secret


def _code(secret: str) -> str:
    return pyotp.TOTP(secret).at(int(time.time()))


def test_the_happy_path(session, engine, signed_up):
    user, secret = signed_up

    user_again = service.check_password(
        session, engine, email="janedoe@gmail.com", password=PASSWORD, ip="100.64.0.2"
    )
    assert user_again.id == user.id

    service.check_code(session, engine, user_again, _code(secret), ip="100.64.0.2")
    result = service.complete_sign_in(
        session, user_again, trust_this_browser=True, device_cookie=None, user_agent="Firefox"
    )
    session.flush()

    assert sessions.lookup(session, result.session_value) is not None
    assert devices.is_trusted(session, user, result.device_value) is not None


def test_a_dotted_or_plussed_address_finds_the_same_account(session, engine, signed_up):
    user, _ = signed_up
    for spelling in ("jane.doe@gmail.com", "JaneDoe+bills@Gmail.com", "j.a.nedoe@gmail.com"):
        assert service.check_password(
            session, engine, email=spelling, password=PASSWORD, ip=None
        ).id == user.id


def test_a_wrong_password_and_an_unknown_email_say_the_same_thing(session, engine, signed_up):
    with pytest.raises(Unauthorized) as wrong:
        service.check_password(session, engine, email="janedoe@gmail.com", password="nope", ip=None)
    with pytest.raises(Unauthorized) as unknown:
        service.check_password(session, engine, email="nobody@gmail.com", password="nope", ip=None)
    assert str(wrong.value) == str(unknown.value)


def test_a_reused_code_is_refused(session, engine, signed_up):
    user, secret = signed_up
    code = _code(secret)
    service.check_code(session, engine, user, code, ip=None)
    with pytest.raises(Unauthorized, match="already been used"):
        service.check_code(session, engine, user, code, ip=None)


def test_guessing_the_password_locks_out_and_says_how_long(session, engine, signed_up):
    for _ in range(5):
        with pytest.raises(Unauthorized):
            service.check_password(
                session, engine, email="janedoe@gmail.com", password="wrong", ip="203.0.113.9"
            )
    with pytest.raises(TooManyAttempts) as caught:
        service.check_password(
            session, engine, email="janedoe@gmail.com", password=PASSWORD, ip="203.0.113.9"
        )
    assert caught.value.retry_after > 0


def test_signing_in_clears_the_failures(session, engine, signed_up):
    user, secret = signed_up
    for _ in range(3):
        with pytest.raises(Unauthorized):
            service.check_password(
                session, engine, email="janedoe@gmail.com", password="wrong", ip="100.64.0.2"
            )

    found = service.check_password(
        session, engine, email="janedoe@gmail.com", password=PASSWORD, ip="100.64.0.2"
    )
    service.check_code(session, engine, found, _code(secret), ip="100.64.0.2")
    service.complete_sign_in(session, found, trust_this_browser=False, device_cookie=None)
    session.flush()

    from app.models import LoginAttempt

    remaining = session.execute(
        select(LoginAttempt).where(LoginAttempt.ok.is_(False))
    ).scalars().all()
    assert remaining == []


def test_not_trusting_the_browser_leaves_no_device(session, engine, signed_up):
    user, secret = signed_up
    result = service.complete_sign_in(
        session, user, trust_this_browser=False, device_cookie=None
    )
    assert result.device_value is None


def test_trusting_a_browser_keeps_the_other_persons_trust(session, engine, signed_up, member):
    """Two people, one laptop: the second must not evict the first."""
    user, _ = signed_up
    theirs = devices.issue(session, member)
    session.flush()

    result = service.complete_sign_in(
        session, user, trust_this_browser=True, device_cookie=theirs
    )
    session.flush()

    assert devices.is_trusted(session, user, result.device_value) is not None
    assert devices.is_trusted(session, member, result.device_value) is not None


def test_a_disabled_user_cannot_sign_in(session, engine, signed_up):
    from app.audit.batch import batch
    from app.models import utcnow
    from app.models.enums import BatchKind

    user, _ = signed_up
    # Disabling someone is a real administrative act, so it is audited.
    with batch(session, kind=BatchKind.admin, actor_id=user.id):
        user.disabled_at = utcnow()

    with pytest.raises(Unauthorized):
        service.check_password(
            session, engine, email="janedoe@gmail.com", password=PASSWORD, ip=None
        )


def test_a_password_a_reset_cleared_is_refused_as_an_unknown_email_is(
    session, engine, signed_up, member, monkeypatch
):
    """A reset link leaves the password NULL (#284). It is checked against the
    dummy hash -- the same work and the same sentence as an address nobody
    signs in with -- and the other account still signs in beside it."""
    from app.audit.batch import batch
    from app.models.enums import BatchKind

    user, _ = signed_up
    with batch(session, kind=BatchKind.admin, actor_id=user.id):
        user.password_hash = None
        member.password_hash = passwords.hash_password(PASSWORD)
    session.commit()

    class Recording:
        def __init__(self, inner) -> None:
            self.inner, self.checked = inner, []

        def verify(self, stored, password):
            self.checked.append(stored)
            return self.inner.verify(stored, password)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    hasher = Recording(passwords._hasher)
    monkeypatch.setattr(passwords, "_hasher", hasher)

    with pytest.raises(Unauthorized) as cleared:
        service.check_password(session, engine, email="janedoe@gmail.com", password=PASSWORD, ip=None)
    with pytest.raises(Unauthorized) as unknown:
        service.check_password(session, engine, email="nobody@gmail.com", password=PASSWORD, ip=None)
    assert str(cleared.value) == str(unknown.value)
    assert hasher.checked == [passwords._DUMMY_HASH, passwords._DUMMY_HASH]
    assert session.get(User, user.id).password_hash is None

    found = service.check_password(session, engine, email=member.email, password=PASSWORD, ip=None)
    assert found.id == member.id
