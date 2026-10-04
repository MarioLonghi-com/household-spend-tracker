"""A member replaces their own recovery codes (#289).

Two members throughout: the owner, who regenerates, and an invited member
whose codes must come through every attempt byte-for-byte unchanged. Every
test asserts what happened to the code rows, not only the status code.
"""

from __future__ import annotations

import time

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import HEADERS, PASSWORD, _setup_owner

ROUTE = "/api/me/recovery-codes"


def _db():
    # `app.db` is reloaded per test by the `client` fixture, so it is read late.
    import app.db as db

    return Session(db.engine)


def _rows(user_id: str) -> list[tuple]:
    """Every column of every recovery code row the user has, in a stable order."""
    from app.models import RecoveryCode

    with _db() as s:
        rows = s.scalars(
            select(RecoveryCode).where(RecoveryCode.user_id == user_id).order_by(RecoveryCode.id)
        ).all()
        return [(r.id, r.user_id, r.code_hash, r.used_at, r.created_at) for r in rows]


def _redeems(user_id: str, code: str) -> bool:
    """Whether this would be accepted as one of the user's unused codes. Spends nothing."""
    from app.auth import service as auth_service
    from app.models import User

    with _db() as s:
        return auth_service.match_recovery_code(s, s.get(User, user_id), code) is not None


def _counter(user_id: str) -> int | None:
    """The last authenticator step the user has burned."""
    from app.models import User

    with _db() as s:
        return s.get(User, user_id).totp_last_counter


def _failed_stepups(user_id: str) -> int:
    """How many failed attempts the user has against the shared step-up budget."""
    from app.auth import stepup
    from app.models import LoginAttempt, User

    with _db() as s:
        canonical = s.get(User, user_id).email_canonical
        return len(
            s.scalars(
                select(LoginAttempt).where(
                    LoginAttempt.email_canonical == canonical,
                    LoginAttempt.kind == stepup.KIND,
                    LoginAttempt.ok.is_(False),
                )
            ).all()
        )


def _two_members(client) -> dict:
    """The owner, signed in on `client`, and a member on a browser of their own."""
    owner = _setup_owner(client)
    invite = client.post(
        "/api/admin/invitations",
        json={"role": "member", "email": None, "household_ids": [], "step_up_token": None},
        headers=HEADERS,
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["link"].rsplit("/", 1)[-1]

    other = TestClient(client.app_module.app, base_url="https://testserver")
    started = other.post(
        "/api/invite/begin",
        json={"token": token, "email": "sam@example.com", "display_name": "Sam", "password": PASSWORD},
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    body = started.json()
    enrolled = other.post(
        "/api/invite/enrol",
        json={"blob": body["blob"], "code": pyotp.TOTP(body["secret"]).at(int(time.time()))},
        headers=HEADERS,
    )
    assert enrolled.status_code == 200, enrolled.text
    done = other.post(
        "/api/invite/complete",
        json={"blob": enrolled.json()["blob"], "codes_saved": True},
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    return {
        "owner": owner,
        "member": {
            "user": done.json(),
            "secret": body["secret"],
            "recovery_codes": body["recovery_codes"],
            "client": other,
        },
    }


def test_new_codes_replace_the_old_ones_and_leave_the_other_member_alone(client, clock):
    world = _two_members(client)
    owner_id = world["owner"]["user"]["id"]
    member_id = world["member"]["user"]["id"]
    old = world["owner"]["recovery_codes"]
    assert client.get(ROUTE, headers=HEADERS).json() == {"unused": 10}
    member_before = _rows(member_id)
    assert len(member_before) == 10

    answer = client.post(
        ROUTE,
        json={"password": PASSWORD, "code": pyotp.TOTP(world["owner"]["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    new = answer.json()["codes"]

    assert len(new) == 10 and len(set(new)) == 10
    assert not set(new) & set(old)
    assert len(_rows(owner_id)) == 10, "exactly ten rows, the old ones gone"
    assert not any(_redeems(owner_id, code) for code in old), "an old code still redeems"
    assert all(_redeems(owner_id, code) for code in new), "a new code does not redeem"
    assert client.get(ROUTE, headers=HEADERS).json() == {"unused": 10}

    # The other member: every byte of every row the same, and their codes still theirs.
    assert _rows(member_id) == member_before
    assert _redeems(member_id, world["member"]["recovery_codes"][0])
    assert not any(_redeems(member_id, code) for code in new)


def test_the_authenticator_code_is_burned_and_buys_nothing_else(client, clock):
    """One code, one act: the code that made the sheet cannot make a second
    one, finish a step-up, or finish a sign-in in its own thirty seconds.

    `verify_and_consume`, not `code_matches`, and this is what says so.
    """
    world = _two_members(client)
    owner_id = world["owner"]["user"]["id"]
    member_id = world["member"]["user"]["id"]
    now = clock()
    used = pyotp.TOTP(world["owner"]["secret"]).at(now)
    member_code = pyotp.TOTP(world["member"]["secret"]).at(now)
    member_counter = _counter(member_id)
    assert _counter(owner_id) < now // 30

    made = client.post(ROUTE, json={"password": PASSWORD, "code": used}, headers=HEADERS)
    assert made.status_code == 200, made.text
    assert _counter(owner_id) == now // 30, "the step the code matched was not burned"
    assert _counter(member_id) == member_counter
    owner_after, member_before = _rows(owner_id), _rows(member_id)

    # A second sheet from the same code.
    again = client.post(ROUTE, json={"password": PASSWORD, "code": used}, headers=HEADERS)
    assert again.status_code == 401, again.text
    assert "codes" not in again.json()
    assert _rows(owner_id) == owner_after
    assert all(_redeems(owner_id, code) for code in made.json()["codes"])

    # A step-up from the same code.
    stepped = client.post(
        "/api/me/step-up", json={"password": PASSWORD, "code": used}, headers=HEADERS
    )
    assert stepped.status_code == 401, stepped.text
    assert "token" not in stepped.json()

    # A sign-in on another browser, finished with the same code.
    third = TestClient(client.app_module.app, base_url="https://testserver")
    started = third.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD}, headers=HEADERS
    )
    assert started.status_code == 200 and started.json()["needs_code"] is True, started.text
    finished = third.post(
        "/api/session/code", json={"code": used, "trust_this_browser": False}, headers=HEADERS
    )
    assert finished.status_code == 401, finished.text
    assert third.get("/api/me", headers=HEADERS).status_code == 401

    assert _counter(owner_id) == now // 30
    assert _rows(owner_id) == owner_after

    # The burn is the owner's alone: the member's code from the same instant
    # still makes the member a sheet, and touches nothing of the owner's.
    theirs = world["member"]["client"].post(
        ROUTE, json={"password": PASSWORD, "code": member_code}, headers=HEADERS
    )
    assert theirs.status_code == 200, theirs.text
    assert _rows(member_id) != member_before
    assert _counter(member_id) == now // 30
    assert _rows(owner_id) == owner_after

    # And the owner's account is fine: the next step's code is accepted.
    later = clock()
    fresh = client.post(
        ROUTE,
        json={"password": PASSWORD, "code": pyotp.TOTP(world["owner"]["secret"]).at(later)},
        headers=HEADERS,
    )
    assert fresh.status_code == 200, fresh.text
    assert _rows(owner_id) != owner_after
    assert _counter(owner_id) == later // 30


def test_used_codes_go_too_and_the_count_says_so(client, clock):
    world = _two_members(client)
    owner_id = world["owner"]["user"]["id"]
    member_id = world["member"]["user"]["id"]

    # Spend one by actually signing in with it. Redeeming a code signs out
    # every other browser, so the browser that redeemed it carries on.
    third = TestClient(client.app_module.app, base_url="https://testserver")
    first = third.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD}, headers=HEADERS
    )
    assert first.status_code == 200, first.text
    spent = third.post(
        "/api/session/recovery",
        json={"code": world["owner"]["recovery_codes"][0]},
        headers=HEADERS,
    )
    assert spent.status_code == 200, spent.text
    assert third.get(ROUTE, headers=HEADERS).json() == {"unused": 9}
    member_before = _rows(member_id)

    answer = third.post(
        ROUTE,
        json={"password": PASSWORD, "code": pyotp.TOTP(world["owner"]["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text

    rows = _rows(owner_id)
    assert len(rows) == 10
    assert all(used_at is None for (_id, _u, _h, used_at, _c) in rows), "a used row survived"
    assert third.get(ROUTE, headers=HEADERS).json() == {"unused": 10}
    assert _rows(member_id) == member_before


@pytest.mark.parametrize("wrong", ["password", "totp", "recovery", "garbage"])
def test_wrong_proof_changes_nothing(client, clock, wrong):
    world = _two_members(client)
    owner_id = world["owner"]["user"]["id"]
    member_id = world["member"]["user"]["id"]
    owner_before, member_before = _rows(owner_id), _rows(member_id)
    owner_counter, member_counter = _counter(owner_id), _counter(member_id)

    # One instant, so every live code below is live for the rest of the test.
    now = clock()
    good_code = pyotp.TOTP(world["owner"]["secret"]).at(now)
    member_code = pyotp.TOTP(world["member"]["secret"]).at(now)
    body = {
        # The owner's real, live code, with a wrong password.
        "password": {"password": "not my password at all", "code": good_code},
        # A real code from the other member's authenticator: valid digits, wrong person.
        "totp": {"password": PASSWORD, "code": member_code},
        # An unused recovery code of the caller's own, offered as the second factor.
        "recovery": {"password": PASSWORD, "code": world["owner"]["recovery_codes"][1]},
        "garbage": {"password": PASSWORD, "code": "12345x"},
    }[wrong]

    answer = client.post(ROUTE, json=body, headers=HEADERS)
    assert answer.status_code == 401, answer.text
    assert "codes" not in answer.json()

    assert _rows(owner_id) == owner_before
    assert _rows(member_id) == member_before
    # The recovery code offered was not spent, and still redeems.
    assert _redeems(owner_id, world["owner"]["recovery_codes"][1])

    # No authenticator code was burned, the owner's or the member's. The
    # password is judged first: a caller without it must not be able to spend
    # the owner's current code and refuse their next sign-in every 30 seconds.
    assert _counter(owner_id) == owner_counter
    assert _counter(member_id) == member_counter
    mine = client.post(ROUTE, json={"password": PASSWORD, "code": good_code}, headers=HEADERS)
    assert mine.status_code == 200, mine.text
    assert _rows(owner_id) != owner_before
    assert _counter(owner_id) == now // 30
    assert _rows(member_id) == member_before
    theirs = world["member"]["client"].post(
        ROUTE, json={"password": PASSWORD, "code": member_code}, headers=HEADERS
    )
    assert theirs.status_code == 200, theirs.text
    assert _rows(member_id) != member_before
    assert _counter(member_id) == now // 30


def test_wrong_proof_spends_the_shared_step_up_budget(client, clock):
    """Guesses here and on the password re-check draw on one budget."""
    from app.auth import ratelimit

    world = _two_members(client)
    owner_id = world["owner"]["user"]["id"]
    before = _rows(owner_id)
    for i in range(ratelimit.FREE_ATTEMPTS - 1):
        refused = client.post(
            ROUTE, json={"password": f"guess {i}", "code": "000000"}, headers=HEADERS
        )
        assert refused.status_code == 401, refused.text
    # The last free attempt on a different route that shares the budget.
    refused = client.post(
        "/api/me/password",
        json={"current_password": "guess again", "new_password": "x" * 30},
        headers=HEADERS,
    )
    assert refused.status_code == 401

    # Now even the right proof is a lockout, and nothing was written.
    locked = client.post(
        ROUTE,
        json={"password": PASSWORD, "code": pyotp.TOTP(world["owner"]["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert locked.status_code == 429, locked.text
    assert _rows(owner_id) == before


def test_a_right_proof_gives_its_reservation_back(client, clock):
    """A regeneration that succeeds is not a failed attempt.

    The attempt is reserved as a failure before it is judged, and released
    when both factors turn out right. Without the release, every legitimate
    regeneration would be a spent step-up attempt, and five of them in
    fifteen minutes would lock the member out of step-up, password change and
    re-enrolment alike.
    """
    from app.auth import ratelimit

    world = _two_members(client)
    owner_id = world["owner"]["user"]["id"]
    member_id = world["member"]["user"]["id"]
    owner_secret = world["owner"]["secret"]
    assert _failed_stepups(owner_id) == 0
    assert _failed_stepups(member_id) == 0
    member_before = _rows(member_id)

    # One more right proof than the budget holds, all inside one window.
    for _ in range(ratelimit.FREE_ATTEMPTS + 1):
        before = _rows(owner_id)
        made = client.post(
            ROUTE,
            json={"password": PASSWORD, "code": pyotp.TOTP(owner_secret).at(clock())},
            headers=HEADERS,
        )
        assert made.status_code == 200, made.text
        assert _rows(owner_id) != before
        assert _failed_stepups(owner_id) == 0, "a right proof was left counted as a failure"

    # And the budget is whole: exactly FREE_ATTEMPTS wrong guesses are heard
    # before the door shuts, and then even the right proof is refused.
    for i in range(ratelimit.FREE_ATTEMPTS):
        refused = client.post(
            ROUTE, json={"password": f"guess {i}", "code": "000000"}, headers=HEADERS
        )
        assert refused.status_code == 401, refused.text
    assert _failed_stepups(owner_id) == ratelimit.FREE_ATTEMPTS
    locked_out = _rows(owner_id)
    locked = client.post(
        ROUTE,
        json={"password": PASSWORD, "code": pyotp.TOTP(owner_secret).at(clock())},
        headers=HEADERS,
    )
    assert locked.status_code == 429, locked.text
    assert _rows(owner_id) == locked_out

    # The member's budget is their own: untouched by any of it.
    assert _failed_stepups(member_id) == 0
    assert _rows(member_id) == member_before
    theirs = world["member"]["client"].post(
        ROUTE,
        json={"password": PASSWORD, "code": pyotp.TOTP(world["member"]["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert theirs.status_code == 200, theirs.text
    assert _rows(member_id) != member_before
    assert _failed_stepups(member_id) == 0


def test_the_codes_are_redacted_in_the_audit_log(client, clock):
    from app.models import Batch, BatchKind, Change, ChangeOp

    world = _two_members(client)
    owner_id = world["owner"]["user"]["id"]
    answer = client.post(
        ROUTE,
        json={"password": PASSWORD, "code": pyotp.TOTP(world["owner"]["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert answer.status_code == 200
    new = answer.json()["codes"]

    with _db() as s:
        latest = s.scalars(select(Batch).order_by(Batch.started_at.desc())).first()
        assert latest.kind == BatchKind.manual and latest.actor_id == owner_id
        changes = s.scalars(
            select(Change).where(Change.batch_id == latest.id, Change.table_name == "recovery_codes")
        ).all()
        ops = sorted(c.op for c in changes)
        assert ops.count(ChangeOp.delete) == 10 and ops.count(ChangeOp.insert) == 10
        for change in changes:
            assert change.redacted == ["code_hash"]
            for image in (change.before, change.after):
                if image is not None:
                    assert "code_hash" not in image
        logged = repr([(c.before, c.after) for c in changes])
    assert not any(code in logged for code in new), "a plaintext code reached the audit log"
    assert "$argon2" not in logged, "a code hash reached the audit log"


def test_sessions_and_other_browsers_survive(client, clock):
    world = _two_members(client)
    second = TestClient(client.app_module.app, base_url="https://testserver")
    first = second.post(
        "/api/session", json={"email": "Jane.Doe@gmail.com", "password": PASSWORD}, headers=HEADERS
    )
    assert first.status_code == 200
    assert second.post(
        "/api/session/code",
        json={"code": pyotp.TOTP(world["owner"]["secret"]).at(clock()), "trust_device": False},
        headers=HEADERS,
    ).status_code == 200

    answer = client.post(
        ROUTE,
        json={"password": PASSWORD, "code": pyotp.TOTP(world["owner"]["secret"]).at(clock())},
        headers=HEADERS,
    )
    assert answer.status_code == 200

    assert client.get("/api/me", headers=HEADERS).status_code == 200
    assert second.get("/api/me", headers=HEADERS).status_code == 200, "another browser was signed out"
    assert world["member"]["client"].get("/api/me", headers=HEADERS).status_code == 200


def test_an_account_without_an_authenticator_is_refused_cleanly(session, owner, member):
    """`totp_secret` is becoming nullable on another branch; no secret, no codes.

    The user is detached first: the column is still NOT NULL here, so the
    None is only ever in memory.
    """
    from app.errors import ValidationError
    from app.models import Batch, RecoveryCode
    from app.services import profile

    batches_before = len(session.scalars(select(Batch)).all())
    session.expunge(owner)
    owner.totp_secret = None
    with pytest.raises(ValidationError):
        profile.regenerate_recovery_codes(
            session, session.get_bind(), owner, password="x", code="123456", ip=None
        )
    assert session.scalars(select(RecoveryCode)).all() == []
    assert len(session.scalars(select(Batch)).all()) == batches_before


@pytest.mark.parametrize("method", ["get", "post"])
def test_not_reachable_signed_out(client, method):
    _setup_owner(client)
    client.cookies.clear()
    answer = getattr(client, method)(
        ROUTE, **({"json": {"password": PASSWORD, "code": "123456"}} if method == "post" else {}),
        headers=HEADERS,
    )
    assert answer.status_code == 401
