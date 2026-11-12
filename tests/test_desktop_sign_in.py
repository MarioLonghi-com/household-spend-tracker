"""A password opens the desktop app; one recovery code replaces it. Issue #320.

Decided on 2026-10-13: a password at every launch, the web app's rules, and one
recovery code shown at setup. No authenticator, passkeys, reset links or
invitations on the desktop. The server keeps everything it has.

What is asserted is what was stored and what a second request then gets: the
hash that changed, the code that was spent, the session that ended. Two
installs, because a desktop install has one person -- and a recovery code from
one ledger must open nothing in the other.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import HEADERS, PASSWORD, _setup_owner, _stamp_head

NEW_PASSWORD = "another long enough password"


def _boot(monkeypatch, data: Path, *, profile: str | None) -> TestClient:
    for name in ("SPENDTRACKER_PUBLIC_URL", "SPENDTRACKER_RP_ID", "SPENDTRACKER_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    if profile is None:
        monkeypatch.delenv("SPENDTRACKER_PROFILE", raising=False)
    else:
        monkeypatch.setenv("SPENDTRACKER_PROFILE", profile)
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(data))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{data / 'spendtracker.sqlite3'}")
    import app.config as config

    importlib.reload(config)
    import app.db as db

    importlib.reload(db)
    from app.models import Base

    Base.metadata.create_all(db.engine)
    _stamp_head(db.engine)
    import app.main as main

    importlib.reload(main)
    host = "localhost" if profile == "desktop" else "testserver"
    return TestClient(main.app, base_url=f"https://{host}")


@pytest.fixture()
def restore_server(monkeypatch):
    yield
    import app.config as config
    import app.db as db
    import app.main as main

    monkeypatch.undo()
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(main)


def _setup(client, email: str) -> dict:
    started = client.post(
        "/api/setup/begin",
        json={"token": "", "email": email, "display_name": email.split("@")[0], "password": PASSWORD},
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    body = started.json()
    done = client.post("/api/setup/complete", json={"blob": body["blob"], "codes_saved": True}, headers=HEADERS)
    assert done.status_code == 200, done.text
    return body


def _stored(email: str):
    import app.db as db
    from app.models import RecoveryCode, User

    with db.SessionLocal() as session:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        codes = session.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id)).scalars().all()
        return user.password_hash, user.totp_secret, [(c.used_at is not None) for c in codes]


def _sign_in(client, email: str, password: str):
    return client.post("/api/session", json={"email": email, "password": password}, headers=HEADERS)


def test_setup_asks_for_no_token_and_no_authenticator(monkeypatch, tmp_path, restore_server):
    data = tmp_path / "ledger"
    with _boot(monkeypatch, data, profile="desktop") as client:
        state = client.get("/api/setup/state").json()
        assert {key: state[key] for key in ("setup_required", "token_needed", "token_path")} == {
            "setup_required": True, "token_needed": False, "token_path": None
        }
        # The first-run wizard's facts (#458): this installation is the desktop app.
        assert state["install"]["edition"] == {"darwin": "mac", "win32": "windows"}.get(sys.platform, "desktop")
        assert state["install"]["data_dir"] == str(data)
        assert not (data / "setup-token").exists(), "no token is written or logged"

        body = _setup(client, "jane@example.test")
        assert (body["secret"], body["otpauth_uri"]) == (None, None)
        assert len(body["recovery_codes"]) == 1

        assert client.get("/api/me", headers=HEADERS).json()["email"] == "jane@example.test"
        _hash, totp_secret, codes = _stored("jane@example.test")
        assert totp_secret is None
        assert codes == [False]


def test_desktop_setup_makes_the_first_household_and_account_with_no_second_step(
    monkeypatch, tmp_path, restore_server
):
    """The first-run wizard on the desktop (#458): no token, no second step,
    and the household, its Spanish categories and a back-dated account made
    with the owner in the one request."""
    import app.db as db
    from app.models import Account, Category, Household, Transaction

    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        started = client.post(
            "/api/setup/begin",
            json={"token": "", "email": "jane@example.test", "display_name": "Jane", "password": PASSWORD},
            headers=HEADERS,
        )
        assert started.status_code == 200, started.text
        done = client.post(
            "/api/setup/complete",
            json={
                "blob": started.json()["blob"],
                "codes_saved": True,
                "household": {
                    "name": "Nuestra casa",
                    "base_currency": "EUR",
                    "date_format": "DD/MM/YYYY",
                    "week_start": "monday",
                    "locale": "es-ES",
                    "categories": "empty",
                },
                "account": {
                    "name": "Efectivo",
                    "kind": "cash",
                    "currency": "USD",
                    "opening_balance": 2500,
                    "opening_date": "2024-02-29",
                },
            },
            headers=HEADERS,
        )
        assert done.status_code == 200, done.text
        with db.SessionLocal() as session:
            household = session.execute(select(Household)).scalar_one()
            assert (household.name, household.base_currency, household.date_format) == (
                "Nuestra casa", "EUR", "DD/MM/YYYY"
            )
            assert [one.name for one in session.execute(select(Category)).scalars()] == ["Reembolsos"]
            account = session.execute(select(Account)).scalar_one()
            assert (account.name, account.type.value, account.currency) == ("Efectivo", "cash", "USD")
            row = session.execute(select(Transaction)).scalar_one()
            assert (row.date.isoformat(), row.amount) == ("2024-02-29", 2500)


def test_a_password_alone_signs_in_and_a_wrong_one_does_not(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        _setup(client, "jane@example.test")
        assert client.delete("/api/session", headers=HEADERS).status_code == 204
        assert client.get("/api/me", headers=HEADERS).status_code == 401

        wrong = _sign_in(client, "jane@example.test", "not the password at all")
        assert wrong.status_code == 401
        assert client.get("/api/me", headers=HEADERS).status_code == 401

        right = _sign_in(client, "jane@example.test", PASSWORD)
        assert right.status_code == 200, right.text
        assert (right.json()["authenticated"], right.json()["needs_code"]) == (True, False)
        assert client.get("/api/me", headers=HEADERS).json()["email"] == "jane@example.test"


def test_step_up_takes_the_password_alone(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        _setup(client, "jane@example.test")
        refused = client.post("/api/me/step-up", json={"password": "not the password at all"}, headers=HEADERS)
        assert (refused.status_code, refused.headers.get("x-refused")) == (401, "proof")
        granted = client.post("/api/me/step-up", json={"password": PASSWORD}, headers=HEADERS)
        assert granted.status_code == 200, granted.text
        assert granted.json()["token"]


def test_the_recovery_code_replaces_a_forgotten_password_once(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        (code,) = _setup(client, "jane@example.test")["recovery_codes"]
        before_hash, _, _ = _stored("jane@example.test")
        assert client.delete("/api/session", headers=HEADERS).status_code == 204

        weak = client.post(
            "/api/session/recover",
            json={"email": "jane@example.test", "code": code, "new_password": "short"},
            headers=HEADERS,
        )
        assert weak.status_code == 422
        assert _stored("jane@example.test")[2] == [False], "a weak password costs no code"

        recovered = client.post(
            "/api/session/recover",
            json={"email": "jane@example.test", "code": code, "new_password": NEW_PASSWORD},
            headers=HEADERS,
        )
        assert recovered.status_code == 200, recovered.text
        fresh = recovered.json()["recovery_code"]
        assert fresh and fresh != code
        assert client.get("/api/me", headers=HEADERS).json()["email"] == "jane@example.test"

        after_hash, _, codes = _stored("jane@example.test")
        assert after_hash != before_hash
        assert codes == [False], "the spent code is gone and one new one stands"

        again = client.post(
            "/api/session/recover",
            json={"email": "jane@example.test", "code": code, "new_password": NEW_PASSWORD + "!"},
            headers=HEADERS,
        )
        assert again.status_code == 401
        assert _sign_in(client, "jane@example.test", PASSWORD).status_code == 401
        assert _sign_in(client, "jane@example.test", NEW_PASSWORD).status_code == 200


def test_recovering_ends_every_other_session(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        (code,) = _setup(client, "jane@example.test")["recovery_codes"]
        import app.main as main

        with TestClient(main.app, base_url="https://localhost") as other:
            assert _sign_in(other, "jane@example.test", PASSWORD).status_code == 200
            assert other.get("/api/me", headers=HEADERS).status_code == 200
            recovered = client.post(
                "/api/session/recover",
                json={"email": "jane@example.test", "code": code, "new_password": NEW_PASSWORD},
                headers=HEADERS,
            )
            assert recovered.status_code == 200, recovered.text
            assert other.get("/api/me", headers=HEADERS).status_code == 401
        assert client.get("/api/me", headers=HEADERS).status_code == 200


def test_one_ledgers_code_opens_nothing_in_another(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "first", profile="desktop") as client:
        (first_code,) = _setup(client, "jane@example.test")["recovery_codes"]
    with _boot(monkeypatch, tmp_path / "second", profile="desktop") as client:
        _setup(client, "jane@example.test")
        before_hash, _, _ = _stored("jane@example.test")
        refused = client.post(
            "/api/session/recover",
            json={"email": "jane@example.test", "code": first_code, "new_password": NEW_PASSWORD},
            headers=HEADERS,
        )
        assert refused.status_code == 401
        assert _stored("jane@example.test")[0] == before_hash


def test_a_new_code_from_the_profile_takes_the_password_and_is_one(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        (code,) = _setup(client, "jane@example.test")["recovery_codes"]
        made = client.post("/api/me/recovery-codes", json={"password": PASSWORD}, headers=HEADERS)
        assert made.status_code == 200, made.text
        (fresh,) = made.json()["codes"]
        assert fresh != code
        assert client.get("/api/me/authenticator", headers=HEADERS).status_code == 404


def test_the_server_keeps_its_two_factors(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "server", profile=None) as client:
        assert client.get("/api/setup/state").json()["token_needed"] is True
        owner = _setup_owner(client)
        assert len(owner["recovery_codes"]) == 10
        assert client.delete("/api/session", headers=HEADERS).status_code == 204
        # A browser setup did not make trusted, which may skip the code.
        client.cookies.clear()
        half = _sign_in(client, "Jane.Doe@gmail.com", PASSWORD).json()
        assert (half["authenticated"], half["needs_code"]) == (False, True)
        assert client.post("/api/me/step-up", json={"password": PASSWORD}, headers=HEADERS).status_code == 401
        assert client.post(
            "/api/session/recover",
            json={"email": "Jane.Doe@gmail.com", "code": owner["recovery_codes"][0], "new_password": NEW_PASSWORD},
            headers=HEADERS,
        ).status_code in (404, 405)
