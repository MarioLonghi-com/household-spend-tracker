"""Sessions, trusted devices and the rate limiter.

The properties here are the ones the brief turned on: a browser that has passed
2FA is not asked again for thirty days, that trust does not renew itself by
being used, and two people sharing a laptop both keep theirs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.auth import cookies, devices, ratelimit, sessions, tokens
from app.config import settings
from app.errors import TooManyAttempts
from app.models import LoginAttempt, TrustedDevice, WebSession, utcnow

# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


def test_a_session_round_trips_and_the_value_is_never_stored(session, owner):
    value = sessions.issue(session, owner, ip="100.64.0.2", user_agent="Firefox")
    session.flush()

    row = sessions.lookup(session, value)
    assert row is not None and row.user_id == owner.id

    stored = session.execute(select(WebSession.id_hash)).scalars().all()
    assert value not in stored
    assert tokens.fingerprint(value) in stored


def test_a_session_dies_at_the_absolute_limit(session, owner):
    value = sessions.issue(session, owner)
    session.flush()
    now = utcnow()

    just_inside = now + timedelta(seconds=settings.session_absolute_seconds - 60)
    assert sessions.lookup(session, value, now=just_inside) is None, "idle window should bite first"

    # Keep it warm so only the absolute limit can end it.
    row = sessions.lookup(session, value)
    row.last_seen_at = now + timedelta(seconds=settings.session_absolute_seconds - 120)
    assert sessions.lookup(session, value, now=just_inside) is not None

    past = now + timedelta(seconds=settings.session_absolute_seconds + 1)
    assert sessions.lookup(session, value, now=past) is None


def test_a_session_dies_when_left_alone(session, owner):
    value = sessions.issue(session, owner)
    session.flush()
    idle = utcnow() + timedelta(seconds=settings.session_idle_seconds + 1)
    assert sessions.lookup(session, value, now=idle) is None


def test_last_seen_is_not_rewritten_on_every_request(session, owner, engine):
    """A fourteen-day idle window does not need second-level precision, and
    writing on every request turns every page view into a WAL write."""
    from sqlalchemy import event

    value = sessions.issue(session, owner)
    session.flush()
    row = sessions.lookup(session, value)

    writes = []

    def _count(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("UPDATE SESSIONS"):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        start = row.last_seen_at
        for offset in range(0, 50):
            sessions.touch(session, row, now=start + timedelta(seconds=offset))
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    # 50 requests across 49 seconds, with a 60-second threshold: none at all.
    assert writes == []

    sessions.touch(session, row, now=start + timedelta(seconds=settings.session_touch_seconds + 1))
    assert row.last_seen_at > start


def test_signing_out_everywhere_leaves_the_current_browser_alone(session, owner):
    here = sessions.issue(session, owner)
    elsewhere = sessions.issue(session, owner)
    session.flush()

    removed = sessions.revoke_all_for(session, owner.id, except_hash=tokens.fingerprint(here))
    assert removed == 1
    assert sessions.lookup(session, here) is not None
    assert sessions.lookup(session, elsewhere) is None


def test_presence_is_who_was_seen_recently(session, owner, member):
    mine = sessions.issue(session, owner)
    sessions.issue(session, member)
    session.flush()

    assert sessions.online_users(session) == {owner.id, member.id}

    stale = sessions.lookup(session, mine)
    stale.last_seen_at = utcnow() - timedelta(seconds=settings.presence_window_seconds + 60)
    session.flush()
    assert sessions.online_users(session) == {member.id}


# --------------------------------------------------------------------------- #
# Trusted devices
# --------------------------------------------------------------------------- #


def test_a_trusted_browser_is_not_asked_again(session, owner):
    value = devices.issue(session, owner, label="Jane's laptop")
    session.flush()
    assert devices.is_trusted(session, owner, value) is not None


def test_trust_does_not_renew_itself_by_being_used(session, owner):
    """A sliding window would mean a stolen laptop stays trusted forever."""
    value = devices.issue(session, owner)
    session.flush()
    row = devices.is_trusted(session, owner, value)
    expiry = row.expires_at

    later = utcnow() + timedelta(days=20)
    devices.is_trusted(session, owner, value, now=later)
    assert row.expires_at == expiry
    assert row.last_used_at == later

    past = utcnow() + timedelta(seconds=settings.trusted_device_seconds + 1)
    assert devices.is_trusted(session, owner, value, now=past) is None


def test_two_people_on_one_laptop_both_stay_trusted(session, owner, member):
    """One cookie name holds one value, so a single token means the second
    person to sign in silently evicts the first."""
    first = devices.issue(session, owner)
    second = devices.issue(session, member)
    session.flush()

    cookie = devices.build_cookie(devices.parse_cookie(first), second)

    assert devices.is_trusted(session, owner, cookie) is not None
    assert devices.is_trusted(session, member, cookie) is not None


def test_one_persons_token_does_not_trust_another(session, owner, member):
    value = devices.issue(session, owner)
    session.flush()
    assert devices.is_trusted(session, member, value) is None


def test_the_cookie_holds_at_most_four(session):
    cookie = ""
    for index in range(6):
        cookie = devices.build_cookie(devices.parse_cookie(cookie), f"token{index}")
    parsed = devices.parse_cookie(cookie)
    assert len(parsed) == devices.MAX_PER_BROWSER
    assert parsed[0] == "token5", "newest first"
    assert "token0" not in parsed, "oldest evicted"


def test_changing_a_password_forgets_every_device(session, owner):
    devices.issue(session, owner)
    devices.issue(session, owner)
    session.flush()

    assert devices.revoke_all_for(session, owner.id) == 2
    assert session.execute(
        select(func.count()).select_from(TrustedDevice).where(TrustedDevice.user_id == owner.id)
    ).scalar_one() == 0


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


def test_five_failures_lock_the_email_out_with_a_wait(session, engine):
    for _ in range(5):
        ratelimit.record(engine, email_canonical="jane@example.com", ip="100.64.0.2", ok=False)

    with pytest.raises(TooManyAttempts) as caught:
        ratelimit.check(session, email_canonical="jane@example.com", ip="10.0.0.9")
    assert caught.value.retry_after > 0
    assert caught.value.status_code == 429


def test_a_spray_across_accounts_does_not_lock_an_untouched_one(session, engine):
    """This test used to assert the opposite, and that was the bug.

    Keyed on the address alone at a threshold of five, an unauthenticated
    stranger typed five invented addresses and every real account on the
    instance was refused its own correct password -- renewably, for as long as
    they cared to keep it up. Behind `tailscale serve` every request shares one
    loopback peer, so it was also one person's five typos away by accident.
    """
    for index in range(5):
        ratelimit.record(
            engine, email_canonical=f"victim{index}@example.com", ip="203.0.113.5", ok=False
        )

    ratelimit.check(session, email_canonical="untouched@example.com", ip="203.0.113.5")


def test_a_wide_enough_spray_from_one_address_is_still_stopped(session, engine):
    """The arm survives; it sits where only a sweep reaches it."""
    for index in range(ratelimit.FREE_ATTEMPTS_PER_IP):
        ratelimit.record(
            engine, email_canonical=f"victim{index}@example.com", ip="203.0.113.9", ok=False
        )

    with pytest.raises(TooManyAttempts):
        ratelimit.check(session, email_canonical="untouched@example.com", ip="203.0.113.9")


def test_the_same_account_from_one_address_is_limited_at_five(session, engine):
    """The pair is what the second arm is really for."""
    for _ in range(5):
        ratelimit.record(
            engine, email_canonical="victim@example.com", ip="203.0.113.7", ok=False
        )

    # From somewhere else the per-account arm catches it anyway ...
    with pytest.raises(TooManyAttempts):
        ratelimit.check(session, email_canonical="victim@example.com", ip="198.51.100.4")

    # ... and an untouched account from that same address is unaffected.
    ratelimit.check(session, email_canonical="someone.else@example.com", ip="203.0.113.7")


def test_retry_after_says_when_the_lockout_actually_lifts(session, engine):
    """It used to say two seconds for a fifteen-minute window, every time.

    A refused attempt raises before the attempt is recorded, so the failure
    count was pinned at exactly five and the exponential backoff never moved off
    its first step. A client honouring the header retried every two seconds for
    a quarter of an hour -- the request amplification the module exists to
    prevent.
    """
    for _ in range(5):
        ratelimit.record(
            engine, email_canonical="jane@example.com", ip="100.64.0.2", ok=False
        )

    with pytest.raises(TooManyAttempts) as raised:
        ratelimit.check(session, email_canonical="jane@example.com", ip="100.64.0.2")

    window = int(ratelimit.WINDOW.total_seconds())
    assert window - 60 <= raised.value.retry_after <= window + 1, (
        f"retry_after was {raised.value.retry_after}, "
        f"but the lockout lasts until the oldest failure ages out of {window}s"
    )


def test_four_failures_are_still_allowed_through(session, engine):
    for _ in range(4):
        ratelimit.record(engine, email_canonical="jane@example.com", ip="100.64.0.2", ok=False)
    ratelimit.check(session, email_canonical="jane@example.com", ip="100.64.0.2")


def test_an_attempt_survives_the_rollback_of_the_login_that_failed(session, engine):
    """The failed sign-in raises; the record of it must not vanish with the
    transaction that raised."""
    ratelimit.record(engine, email_canonical="jane@example.com", ip="100.64.0.2", ok=False)
    session.rollback()
    assert session.execute(select(func.count()).select_from(LoginAttempt)).scalar_one() == 1


def test_succeeding_wipes_the_slate(session, engine):
    for _ in range(4):
        ratelimit.record(engine, email_canonical="jane@example.com", ip="100.64.0.2", ok=False)
    session.expire_all()
    ratelimit.clear_for(session, "jane@example.com")
    session.flush()
    ratelimit.check(session, email_canonical="jane@example.com", ip="100.64.0.2")


# --------------------------------------------------------------------------- #
# What a refused sign-in is allowed to leave behind (#202)
# --------------------------------------------------------------------------- #


def _longest_recorded_address(engine) -> int:
    from sqlalchemy.orm import Session

    with Session(engine) as own:
        return own.execute(
            select(func.coalesce(func.max(func.length(LoginAttempt.email_canonical)), 0))
        ).scalar_one()


def test_a_huge_address_is_refused_at_the_door_and_records_nothing(client):
    """A 300 KiB address used to become a 300 KiB `login_attempts` row that
    outlived the 401 by design -- a stranger could fill the disk with it."""
    from app.auth import email_canonical
    from tests.conftest import HEADERS

    engine = client.app_module.db_engine
    ordinary = client.post(
        "/api/session",
        json={"email": "somebody@example.com", "password": "not the password"},
        headers=HEADERS,
    )
    assert ordinary.status_code == 401
    before = _longest_recorded_address(engine)
    assert 0 < before <= email_canonical.MAX_LENGTH

    huge = "a" * (300 * 1024) + "@example.com"
    refused = client.post(
        "/api/session", json={"email": huge, "password": "whatever"}, headers=HEADERS
    )
    assert refused.status_code == 422

    too_long_password = client.post(
        "/api/session",
        json={"email": "somebody@example.com", "password": "p" * 1025},
        headers=HEADERS,
    )
    assert too_long_password.status_code == 422

    assert _longest_recorded_address(engine) == before
    from sqlalchemy.orm import Session

    with Session(engine) as own:
        assert own.execute(select(func.count()).select_from(LoginAttempt)).scalar_one() == 1


def test_an_address_that_will_not_fold_is_recorded_no_longer_than_the_column(session, engine):
    """The second line, for a caller that did not come through `SignIn`: the
    fallback is still counted, but truncated to what the column says it holds."""
    from app.auth import email_canonical, service
    from app.errors import Unauthorized

    junk = "x" * 5000  # no @, so `canonical` refuses it and the fallback runs
    with pytest.raises(Unauthorized):
        service.check_password(session, engine, email=junk, password="nope", ip="100.64.0.2")

    assert _longest_recorded_address(engine) == email_canonical.MAX_LENGTH


# --------------------------------------------------------------------------- #
# The lockout is for the guesser, not for a browser that proved both (#203)
# --------------------------------------------------------------------------- #


def _sign_in(client, email: str, password: str):
    from tests.conftest import HEADERS

    return client.post(
        "/api/session", json={"email": email, "password": password}, headers=HEADERS
    )


def _failures_recorded(client) -> int:
    from sqlalchemy.orm import Session

    with Session(client.app_module.db_engine) as own:
        return own.execute(
            select(func.count()).select_from(LoginAttempt).where(LoginAttempt.ok.is_(False))
        ).scalar_one()


def _lock_everything_from_here(client, owner_email: str) -> None:
    """Five wrong passwords at the owner, then invented addresses until the
    per-address arm is full: both of the arms a stranger can reach."""
    for _ in range(ratelimit.FREE_ATTEMPTS):
        assert _sign_in(client, owner_email, "not the password").status_code == 401
    for index in range(ratelimit.FREE_ATTEMPTS_PER_IP // ratelimit.FREE_ATTEMPTS - 1):
        for _ in range(ratelimit.FREE_ATTEMPTS):
            assert _sign_in(client, f"nobody{index}@example.com", "guess").status_code == 401
    assert _failures_recorded(client) == ratelimit.FREE_ATTEMPTS_PER_IP


def test_a_trusted_browser_signs_in_through_a_strangers_lockout(client):
    from tests.conftest import PASSWORD, _setup_owner

    world = _setup_owner(client)
    email = world["user"]["email"]
    device = client.cookies.get(cookies.device_name("testserver"))
    assert device, "setup trusts the browser that did it"

    _lock_everything_from_here(client, email)

    # Without the trusted-device cookie: the guessing case, still locked.
    client.cookies.delete(cookies.device_name("testserver"))
    refused = _sign_in(client, email, PASSWORD)
    assert refused.status_code == 429, refused.text

    # With it: the owner's own laptop, which has already proved the second
    # factor, gets in -- and is not asked for a code.
    client.cookies.set(cookies.device_name("testserver"), device, domain="testserver.local")
    allowed = _sign_in(client, email, PASSWORD)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["authenticated"] is True


def test_a_trusted_browser_s_failures_are_still_recorded_and_still_bounded(client):
    """A stolen device cookie must not buy unlimited password guesses."""
    from tests.conftest import PASSWORD, _setup_owner

    world = _setup_owner(client)
    email = world["user"]["email"]

    for attempt in range(ratelimit.FREE_ATTEMPTS_TRUSTED):
        wrong = _sign_in(client, email, "not the password")
        assert wrong.status_code == 401, (attempt, wrong.text)
    # Every one of them was written down, past the ordinary five.
    assert _failures_recorded(client) == ratelimit.FREE_ATTEMPTS_TRUSTED

    stopped = _sign_in(client, email, PASSWORD)
    assert stopped.status_code == 429, stopped.text
    assert _failures_recorded(client) == ratelimit.FREE_ATTEMPTS_TRUSTED


def test_trust_for_one_person_does_not_lift_the_lock_on_another(session, owner, member):
    """The check is per account: a browser trusted for the member is a
    stranger to the owner's lockout."""
    value = devices.issue(session, member)
    session.flush()
    assert devices.holds_live_trust(session, member.id, value) is True
    assert devices.holds_live_trust(session, owner.id, value) is False
    assert devices.holds_live_trust(session, "-", value) is False
    assert devices.holds_live_trust(session, member.id, None) is False
