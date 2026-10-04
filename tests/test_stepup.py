"""Proving both factors again, and spending that proof exactly once.

The bar exists because an agent key outlives the session that minted it, which
nothing else a signed-in person can do here does. So these tests assert the two
properties that make a grant worth more than a cookie -- it is spent, and it
expires -- and they assert them on the rows, not on the status codes.
"""

from __future__ import annotations

from datetime import timedelta

import pyotp

from app.models import StepUpGrant, utcnow
from tests.conftest import HEADERS, PASSWORD, _setup_owner


def _grants(client) -> list[StepUpGrant]:
    """Every live grant, read straight from the app's own database."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    with Session(client.app_module.db_engine) as own:
        return list(own.execute(select(StepUpGrant)).scalars())


def _step_up(client, secret: str, clock, *, password: str = PASSWORD):
    return client.post(
        "/api/me/step-up",
        json={"password": password, "code": pyotp.TOTP(secret).at(clock())},
        headers=HEADERS,
    )


# --------------------------------------------------------------------------- #
# Getting one
# --------------------------------------------------------------------------- #


def test_both_factors_buy_exactly_one_grant(client, clock):
    world = _setup_owner(client)

    answer = _step_up(client, world["secret"], clock)
    assert answer.status_code == 200, answer.text

    rows = _grants(client)
    assert len(rows) == 1
    # The value is never stored -- only its hash, like every other token here.
    assert answer.json()["token"] not in {row.id_hash for row in rows}
    assert rows[0].user_id == world["user"]["id"]


def test_the_window_is_five_minutes_and_does_not_slide(client, clock):
    from app.auth import stepup

    world = _setup_owner(client)
    answer = _step_up(client, world["secret"], clock)

    row = _grants(client)[0]
    assert (row.expires_at - row.created_at) == timedelta(seconds=stepup.GRANT_SECONDS)
    # And the moment it stops being worth anything is told to the client, so a
    # screen can say how long is left rather than find out by being refused.
    assert answer.json()["expires_at"].startswith(row.expires_at.isoformat()[:16])


def test_a_second_grant_drops_the_first(client, clock):
    """Two live grants would be one act paid for twice."""
    world = _setup_owner(client)

    first = _step_up(client, world["secret"], clock).json()["token"]
    clock()
    second = _step_up(client, world["secret"], clock).json()["token"]

    assert len(_grants(client)) == 1
    from app.auth import stepup

    engine = client.app_module.db_engine
    assert stepup.claim(engine, first, user_id=world["user"]["id"]) is False
    assert stepup.claim(engine, second, user_id=world["user"]["id"]) is True


# --------------------------------------------------------------------------- #
# Being refused one
# --------------------------------------------------------------------------- #


def test_the_wrong_password_buys_nothing_and_says_nothing(client, clock):
    world = _setup_owner(client)

    answer = _step_up(client, world["secret"], clock, password="not it at all")
    assert answer.status_code == 401
    assert _grants(client) == []

    # Same sentence as a wrong code, for the reason the sign-in door gives:
    # which of the two was wrong is only ever useful to somebody guessing.
    clock()
    wrong_code = client.post(
        "/api/me/step-up", json={"password": PASSWORD, "code": "000000"}, headers=HEADERS
    )
    assert wrong_code.status_code == 401
    assert wrong_code.json()["detail"] == answer.json()["detail"]
    assert _grants(client) == []


def test_a_wrong_password_does_not_burn_the_authenticator_code(client, clock):
    """The ordering in `stepup.grant` is load-bearing, so it is asserted.

    If the code were consumed first, anybody who could reach this route without
    the password -- which is to say anybody with the session cookie -- could
    burn a live code, and the real owner's next sign-in would be refused.
    """
    world = _setup_owner(client)
    at = clock()
    code = pyotp.TOTP(world["secret"]).at(at)

    refused = client.post(
        "/api/me/step-up", json={"password": "not it at all", "code": code}, headers=HEADERS
    )
    assert refused.status_code == 401

    # The very same code still works, which it would not if it had been spent.
    good = client.post(
        "/api/me/step-up", json={"password": PASSWORD, "code": code}, headers=HEADERS
    )
    assert good.status_code == 200, good.text
    assert len(_grants(client)) == 1


def test_the_code_a_step_up_spends_cannot_also_sign_someone_in(client, clock):
    """The cost `totp.verify_and_consume` documents, imposed here too."""
    world = _setup_owner(client)
    at = clock()
    code = pyotp.TOTP(world["secret"]).at(at)

    assert _step_up(client, world["secret"], lambda: at).status_code == 200

    client.cookies.clear()
    first = client.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert first.status_code == 200, first.text
    refused = client.post(
        "/api/session/code", json={"code": code, "trust_device": False}, headers=HEADERS
    )
    assert refused.status_code == 401
    assert "already been used" in refused.json()["detail"]


def test_guessing_locks_the_step_up_out_and_says_how_long(client, clock):
    """Its own counter, and -- the part that matters -- one that a request can
    actually reach.

    `routers/auth.py` carries a comment about binding `db.engine` at import
    time: the harness reloads `app.db` per test but not the routers, so a
    module-level handle writes every attempt to the *previous* test's database,
    "which is why no TestClient test could ever observe rate limiting". This
    route made that mistake. A test that only asserted 401 on a wrong password
    passed the whole time it was broken; this one does not, because the lockout
    can only arrive if the attempts landed in the database this request reads.
    """
    from app.auth import ratelimit

    world = _setup_owner(client)

    for _ in range(ratelimit.FREE_ATTEMPTS):
        clock()
        refused = _step_up(client, world["secret"], clock, password="not it at all")
        assert refused.status_code == 401, refused.text

    # And now the RIGHT password is refused too, which is what a lockout means.
    clock()
    locked = _step_up(client, world["secret"], clock)
    assert locked.status_code == 429, locked.text
    assert int(locked.headers["Retry-After"]) > 0
    assert _grants(client) == []


def test_a_signed_out_caller_gets_nowhere(client, clock):
    world = _setup_owner(client)
    client.cookies.clear()

    answer = _step_up(client, world["secret"], clock)
    assert answer.status_code == 401
    assert _grants(client) == []


# --------------------------------------------------------------------------- #
# Spending one
# --------------------------------------------------------------------------- #


def test_a_grant_is_single_use(client, clock):
    from app.auth import stepup

    world = _setup_owner(client)
    token = _step_up(client, world["secret"], clock).json()["token"]
    engine = client.app_module.db_engine
    me = world["user"]["id"]

    assert stepup.claim(engine, token, user_id=me) is True
    assert _grants(client) == [], "spending it must delete the row"
    assert stepup.claim(engine, token, user_id=me) is False


def test_a_grant_belonging_to_somebody_else_is_refused_and_still_spent(client, clock):
    from app.auth import stepup

    world = _setup_owner(client)
    token = _step_up(client, world["secret"], clock).json()["token"]
    engine = client.app_module.db_engine

    assert stepup.claim(engine, token, user_id="somebody-else") is False
    assert _grants(client) == [], "a credential somebody has shown is worth keeping from nobody"
    assert stepup.claim(engine, token, user_id=world["user"]["id"]) is False


def test_an_expired_grant_is_refused_and_still_spent(client, clock):
    from app.auth import stepup

    world = _setup_owner(client)
    token = _step_up(client, world["secret"], clock).json()["token"]
    engine = client.app_module.db_engine

    later = utcnow() + timedelta(seconds=stepup.GRANT_SECONDS + 1)
    assert stepup.claim(engine, token, user_id=world["user"]["id"], now=later) is False
    assert _grants(client) == []


def test_require_turns_a_missing_grant_into_a_sentence(client, clock):
    import pytest

    from app.auth import stepup
    from app.errors import Unauthorized

    world = _setup_owner(client)
    engine = client.app_module.db_engine

    with pytest.raises(Unauthorized) as refused:
        stepup.require(engine, None, user_id=world["user"]["id"])
    assert "authenticator code" in str(refused.value)


# --------------------------------------------------------------------------- #
# Sweeping them
# --------------------------------------------------------------------------- #


def test_housekeeping_removes_expired_grants_and_leaves_live_ones(client, clock):
    """Item 3 of the spec's door tests, in the place it actually applies.

    `step_up_grants` is NOT audited, so this sweep is allowed to be a bulk
    delete -- and the assertion is on the row count, because a sweep that
    reports a number without moving one is the failure mode this module exists
    to close.
    """
    from sqlalchemy.orm import Session

    from app.auth import housekeeping
    from app.models import StepUpGrant, utcnow

    world = _setup_owner(client)
    _step_up(client, world["secret"], clock)
    engine = client.app_module.db_engine

    # A live one, and one that expired a minute ago.
    with Session(engine) as own:
        own.add(
            StepUpGrant(
                id_hash="f" * 64,
                user_id=world["user"]["id"],
                created_at=utcnow() - timedelta(minutes=10),
                expires_at=utcnow() - timedelta(minutes=1),
            )
        )
        own.commit()
    assert len(_grants(client)) == 2

    removed = housekeeping.sweep(engine)

    assert removed["step_up_grants"] == 1
    remaining = _grants(client)
    assert len(remaining) == 1
    assert remaining[0].id_hash != "f" * 64


def test_a_wrong_proof_says_so_and_a_missing_session_does_not(client, clock):
    """The client ends the session on a 401 unless it says a proof was refused.

    A mistyped password in a step-up form comes from somebody still signed in;
    a 401 with no session behind it is the one that means "sign in again".
    """
    world = _setup_owner(client)

    wrong = _step_up(client, world["secret"], clock, password="not it at all")
    assert wrong.status_code == 401
    assert wrong.headers.get("x-refused") == "proof"
    # Still signed in: the next request is answered, not refused.
    assert client.get("/api/me", headers=HEADERS).status_code == 200

    client.delete("/api/session", headers=HEADERS)
    gone = client.get("/api/me", headers=HEADERS)
    assert gone.status_code == 401
    assert "x-refused" not in gone.headers
