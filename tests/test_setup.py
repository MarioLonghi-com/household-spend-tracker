"""First boot.

The property under test throughout: a fresh instance cannot be signed into, and
cannot be talked into an owner without a working authenticator. Nothing is
written until the last step, so an abandoned wizard leaves nothing to clean up.
"""

from __future__ import annotations

import time

import pyotp
import pytest
from sqlalchemy import func, select

from app.auth import setup
from app.errors import Conflict, ValidationError
from app.models import Batch, BatchKind, Instance, InstanceState, RecoveryCode, Role, User


@pytest.fixture()
def token(tmp_path, monkeypatch):
    """A fresh instance, with its setup token on disk."""
    monkeypatch.setattr(setup, "setup_token_path", lambda: tmp_path / "setup-token")
    return setup.rotate_setup_token()


def _walk_to_step_four(token: str) -> setup.SetupBlob:
    blob = setup.begin(
        token,
        email="Jane.Doe+first@gmail.com",
        display_name="Jane",
        password="a sufficiently long password",
    )
    code = pyotp.TOTP(blob.totp_secret).at(int(time.time()))
    return setup.confirm_authenticator(blob, code)


# --------------------------------------------------------------------------- #
# The token
# --------------------------------------------------------------------------- #


def test_the_token_file_is_not_world_readable(token, tmp_path):
    mode = (tmp_path / "setup-token").stat().st_mode & 0o777
    assert mode == 0o600


def test_a_wrong_token_gets_nowhere(token):
    with pytest.raises(ValidationError, match="not right"):
        setup.begin("not-the-token", email="a@gmail.com", display_name="A", password="x" * 12)


def test_restarting_invalidates_a_wizard_in_flight(token):
    """The token rotates on every restart while unclaimed, so a blob issued
    before one is refused after it."""
    blob = _walk_to_step_four(token)
    sealed = setup.seal(blob)
    setup.rotate_setup_token()

    with pytest.raises(ValidationError, match="server restarted"):
        setup.unseal(sealed)


# --------------------------------------------------------------------------- #
# The wizard
# --------------------------------------------------------------------------- #


def test_the_email_is_canonicalised_on_the_way_in(token):
    blob = setup.begin(
        token, email="Jane.Doe+first@gmail.com", display_name="Jane", password="x" * 12
    )
    assert blob.email == "Jane.Doe+first@gmail.com", "as typed, for display"
    assert blob.email_canonical == "janedoe@gmail.com"


def test_a_short_password_is_refused_in_words(token):
    with pytest.raises(ValidationError, match="at least 12 characters"):
        setup.begin(token, email="a@gmail.com", display_name="A", password="short")


def test_setup_cannot_finish_without_a_working_code(session, token):
    blob = setup.begin(token, email="a@gmail.com", display_name="A", password="x" * 12)

    with pytest.raises(ValidationError, match="code is not right"):
        setup.confirm_authenticator(blob, "000000")

    # And the un-confirmed blob is refused by complete() too.
    with pytest.raises(ValidationError, match="enrolling an authenticator"):
        setup.complete(session, blob)


def test_the_blob_carries_nothing_readable(token):
    """It holds an argon2 hash and a TOTP secret in flight. A signed-but-
    readable carrier would hand both to anyone who could read the cookie."""
    blob = _walk_to_step_four(token)
    sealed = setup.seal(blob)
    assert blob.totp_secret not in sealed
    assert blob.password_hash not in sealed
    assert "gmail" not in sealed.lower()


# --------------------------------------------------------------------------- #
# Completion
# --------------------------------------------------------------------------- #


def test_completing_setup_lands_everything_in_one_go(session, token):
    blob = _walk_to_step_four(token)
    user, session_value, device_value = setup.complete(session, blob, user_agent="Firefox/128")

    assert user.role is Role.owner
    assert user.email_canonical == "janedoe@gmail.com"
    assert session.execute(
        select(func.count()).select_from(RecoveryCode).where(RecoveryCode.user_id == user.id)
    ).scalar_one() == setup.RECOVERY_CODE_COUNT
    assert session.execute(select(Instance)).scalar_one().state is InstanceState.configured
    assert session_value and device_value

    # Batch #1 is the setup itself, and its actor is the user it created.
    first = session.execute(select(Batch).order_by(Batch.started_at)).scalars().first()
    assert first.kind is BatchKind.setup
    assert first.actor_id == user.id
    assert first.summary == {"users": 1, "recovery_codes": setup.RECOVERY_CODE_COUNT}


def test_the_setup_token_is_gone_afterwards(session, token, tmp_path):
    setup.complete(session, _walk_to_step_four(token))
    assert not (tmp_path / "setup-token").exists()


def test_setup_cannot_be_run_twice(session, token):
    setup.complete(session, _walk_to_step_four(token))
    fresh_token = setup.rotate_setup_token()
    with pytest.raises(Conflict, match="already been set up"):
        setup.complete(session, _walk_to_step_four(fresh_token))


def test_an_abandoned_wizard_writes_nothing(session, token):
    _walk_to_step_four(token)  # never completed
    assert session.execute(select(func.count()).select_from(User)).scalar_one() == 0
    assert setup.instance_state(session) is InstanceState.fresh


def test_the_stored_secret_is_encrypted_not_the_secret_itself(session, token):
    blob = _walk_to_step_four(token)
    user, _, _ = setup.complete(session, blob)
    assert user.totp_secret != blob.totp_secret.encode()

    from app.auth import crypto

    assert crypto.open_totp_secret(user.totp_secret, user_id=user.id) == blob.totp_secret


def test_the_wizard_will_not_finish_until_the_codes_are_said_to_be_stored(client):
    """#211: `codes_saved` was sent by the page and read by nobody."""
    from tests.conftest import HEADERS, PASSWORD

    started = client.post(
        "/api/setup/begin",
        json={
            "token": setup.current_setup_token(),
            "email": "Jane.Doe@gmail.com",
            "display_name": "Jane",
            "password": PASSWORD,
        },
        headers=HEADERS,
    ).json()
    enrolled = client.post(
        "/api/setup/enrol",
        json={"blob": started["blob"], "code": pyotp.TOTP(started["secret"]).at(int(time.time()))},
        headers=HEADERS,
    ).json()

    refused = client.post(
        "/api/setup/complete", json={"blob": enrolled["blob"], "codes_saved": False}, headers=HEADERS
    )
    assert refused.status_code == 422, refused.text
    assert client.get("/api/health").json()["setup_required"] is True

    done = client.post(
        "/api/setup/complete", json={"blob": enrolled["blob"], "codes_saved": True}, headers=HEADERS
    )
    assert done.status_code == 200, done.text
    assert client.get("/api/health").json()["setup_required"] is False
