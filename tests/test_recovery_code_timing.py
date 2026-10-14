"""Redeeming a recovery code costs the same whoever the address belongs to (#338).

`POST /api/session/recover` answers an unknown address and a wrong code with
the same sentence, and before #338 it took measurably different times to do
it: an unknown address verified nothing, a known one an argon2 hash per unused
code. Counted here rather than timed -- a count is what the fix promises, and
a timing test is a flaky test.

Two users, one with every code and one with every code spent, and an address
with no account: each costs exactly a full set of verifies, on the server's
ten and on the desktop's one.
"""

from __future__ import annotations

import pytest

from app.audit.batch import batch
from app.auth import passwords
from app.auth import service as auth_service
from app.auth.setup import recovery_code_count
from app.errors import Unauthorized
from app.models import BatchKind, RecoveryCode, utcnow

NEW_PASSWORD = "a long and unguessable sentence"


@pytest.fixture()
def counted(monkeypatch):
    calls = {"n": 0}
    real = passwords.verify_password

    def counting(stored, password):
        calls["n"] += 1
        return real(stored, password)

    monkeypatch.setattr(passwords, "verify_password", counting)
    return calls


def _codes(session, owner, user, *, spent: bool) -> None:
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        for n in range(recovery_code_count()):
            session.add(
                RecoveryCode(
                    user_id=user.id,
                    code_hash=passwords.hash_password(f"code{n:06d}"),
                    used_at=utcnow() if spent else None,
                )
            )


@pytest.mark.parametrize("second_factor", [True, False], ids=["server", "desktop"])
def test_every_address_costs_a_full_set_of_verifies(
    session, engine, owner, member, counted, monkeypatch, second_factor
):
    from app import capabilities

    monkeypatch.setattr(capabilities, "second_factor", lambda: second_factor)
    _codes(session, owner, owner, spent=False)
    _codes(session, owner, member, spent=True)
    full = recovery_code_count()
    assert full == (10 if second_factor else 1)

    for email in ("jane@example.com", "partner@example.com", "nobody@example.com"):
        counted["n"] = 0
        with pytest.raises(Unauthorized):
            auth_service.check_recovery_code_for_password(
                session, engine, email=email, code="wrong00000", new_password=NEW_PASSWORD, ip=None
            )
        assert counted["n"] == full, email


def test_the_right_code_is_still_found(session, engine, owner, member, counted):
    _codes(session, owner, owner, spent=False)
    user, matched = auth_service.check_recovery_code_for_password(
        session, engine, email="jane@example.com", code="code000003",
        new_password=NEW_PASSWORD, ip=None,
    )
    assert user.id == owner.id
    assert passwords.verify_password(matched.code_hash, "code000003")
