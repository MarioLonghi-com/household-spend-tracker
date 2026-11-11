"""Opening the desktop app with Touch ID (#432): the secret the backend issues
for it, and everything that ends it.

The shell holds a secret the backend issued, encrypted by the operating
system, and presents it only after a biometric prompt. What is asserted here is
what the backend stored and what a request then gets: the hash and not the
secret, the row's `last_used_at` that a sign-in moves, the session a revoked
secret no longer opens. Two installs, because a desktop install has one person
-- and a secret from one ledger must open nothing in the other. The server
profile has none of it: the routes are not there.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import HEADERS, PASSWORD, _stamp_head

NEW_PASSWORD = "another long enough password"
EMAIL = "jane@example.test"
HEADER = "X-Spend-Tracker-Device-Unlock"


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


def _setup(client, email: str = EMAIL) -> dict:
    started = client.post(
        "/api/setup/begin",
        json={"token": "", "email": email, "display_name": "Jane", "password": PASSWORD},
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    body = started.json()
    done = client.post("/api/setup/complete", json={"blob": body["blob"], "codes_saved": True}, headers=HEADERS)
    assert done.status_code == 200, done.text
    return body


def _turn_on(client, password: str = PASSWORD):
    return client.post("/api/me/device-unlock", json={"password": password, "method": "touch_id"}, headers=HEADERS)


def _rows():
    import app.db as db
    from app.models import DeviceUnlock

    with db.SessionLocal() as session:
        return [(row.id_hash, row.method, row.last_used_at) for row in session.execute(select(DeviceUnlock)).scalars()]


def _attempts(kind: str):
    import app.db as db
    from app.models import LoginAttempt

    with db.SessionLocal() as session:
        return [(row.email_canonical, row.ok) for row in session.execute(select(LoginAttempt).where(LoginAttempt.kind == kind)).scalars()]


def _sign_out(client) -> None:
    assert client.delete("/api/session", headers=HEADERS).status_code == 204
    assert client.get("/api/me", headers=HEADERS).status_code == 401


def _unlock(client, secret: str):
    return client.post("/api/session/device-unlock", json={"secret": secret}, headers=HEADERS)


def test_turning_it_on_takes_the_password_and_hands_the_secret_to_the_shell_alone(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        _setup(client)
        assert client.get("/api/me/device-unlock", headers=HEADERS).json() == {"enabled": False, "method": None, "created_at": None}

        refused = _turn_on(client, "not the password at all")
        assert (refused.status_code, refused.headers.get("x-refused")) == (401, "proof")
        assert HEADER.lower() not in {key.lower() for key in refused.headers}
        assert _rows() == []
        assert client.get("/api/me", headers=HEADERS).status_code == 200, "a wrong password costs no session"

        made = _turn_on(client)
        assert made.status_code == 200, made.text
        secret = made.headers[HEADER]
        assert len(secret) >= 40
        assert secret not in made.text, "the body never carries it"
        assert made.json()["enabled"] is True and made.json()["method"] == "touch_id"
        ((stored, method, used),) = _rows()
        assert stored != secret and len(stored) == 64 and method == "touch_id" and used is None
        assert client.get("/api/me/device-unlock", headers=HEADERS).json()["enabled"] is True

        unknown = client.post("/api/me/device-unlock", json={"password": PASSWORD, "method": "face_id"}, headers=HEADERS)
        assert unknown.status_code == 404
        assert [row[0] for row in _rows()] == [stored], "an unknown method issues nothing"


def test_the_secret_signs_in_once_the_prompt_is_passed_and_is_recorded_like_a_password(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        _setup(client)
        secret = _turn_on(client).headers[HEADER]
        _sign_out(client)

        wrong = _unlock(client, secret[:-4] + "zzzz")
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "auth.device_unlock_refused"
        assert client.get("/api/me", headers=HEADERS).status_code == 401
        assert _attempts("unlock") == [("-", False)], "an unknown secret is counted against no account"

        opened = _unlock(client, secret)
        assert opened.status_code == 200, opened.text
        assert (opened.json()["authenticated"], opened.json()["needs_code"]) == (True, False)
        assert client.get("/api/me", headers=HEADERS).json()["email"] == EMAIL
        ((_, _, used),) = _rows()
        assert used is not None, "the row records the launch it opened"
        assert _attempts("unlock") == [("-", False)], "the right secret is released; the wrong try stays on record"


def test_turning_it_on_again_replaces_the_secret(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        _setup(client)
        first = _turn_on(client).headers[HEADER]
        second = _turn_on(client).headers[HEADER]
        assert first != second
        assert len(_rows()) == 1
        _sign_out(client)
        assert _unlock(client, first).status_code == 401
        assert _unlock(client, second).status_code == 200


def test_one_ledgers_secret_opens_nothing_in_another(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "first", profile="desktop") as client:
        _setup(client)
        first = _turn_on(client).headers[HEADER]
    with _boot(monkeypatch, tmp_path / "second", profile="desktop") as client:
        _setup(client)
        second = _turn_on(client).headers[HEADER]
        _sign_out(client)
        assert _unlock(client, first).status_code == 401
        assert client.get("/api/me", headers=HEADERS).status_code == 401
        assert _unlock(client, second).status_code == 200


def test_turning_it_off_ends_it(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        _setup(client)
        secret = _turn_on(client).headers[HEADER]
        assert client.delete("/api/me/device-unlock", headers=HEADERS).status_code == 204
        assert _rows() == []
        assert client.get("/api/me/device-unlock", headers=HEADERS).json()["enabled"] is False
        _sign_out(client)
        assert _unlock(client, secret).status_code == 401


def test_a_password_change_ends_it(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        _setup(client)
        secret = _turn_on(client).headers[HEADER]
        changed = client.post(
            "/api/me/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=HEADERS,
        )
        assert changed.status_code == 200, changed.text
        assert _rows() == []
        assert client.get("/api/me", headers=HEADERS).status_code == 200, "this window keeps its session"
        _sign_out(client)
        assert _unlock(client, secret).status_code == 401
        assert client.post("/api/session", json={"email": EMAIL, "password": NEW_PASSWORD}, headers=HEADERS).status_code == 200


def test_a_forgotten_password_replaced_by_the_recovery_code_ends_it(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        (code,) = _setup(client)["recovery_codes"]
        secret = _turn_on(client).headers[HEADER]
        _sign_out(client)
        recovered = client.post(
            "/api/session/recover",
            json={"email": EMAIL, "code": code, "new_password": NEW_PASSWORD},
            headers=HEADERS,
        )
        assert recovered.status_code == 200, recovered.text
        assert _rows() == []
        _sign_out(client)
        assert _unlock(client, secret).status_code == 401


def test_a_restore_from_backup_ends_it(monkeypatch, tmp_path, restore_server):
    from app.desktop import control, restore

    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        _setup(client)
        secret = _turn_on(client).headers[HEADER]
        # A backup taken after turning it on carries the row.
        made = control.backup()
        kept = tmp_path / "kept.sqlite3"
        kept.write_bytes(Path(made["path"]).read_bytes())
    # The shell restores with the app stopped, from a process of its own.
    done = restore.restore(kept)
    assert done.ok, done.reason
    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        assert _rows() == []
        assert _unlock(client, secret).status_code == 401
        assert client.post("/api/session", json={"email": EMAIL, "password": PASSWORD}, headers=HEADERS).status_code == 200


def test_a_ledger_from_before_the_table_restores_as_it_did(monkeypatch, tmp_path, restore_server):
    import contextlib
    import sqlite3

    from app.desktop import control, restore

    with _boot(monkeypatch, tmp_path / "ledger", profile="desktop") as client:
        _setup(client)
        made = control.backup()
        kept = tmp_path / "old.sqlite3"
        kept.write_bytes(Path(made["path"]).read_bytes())
    with contextlib.closing(sqlite3.connect(kept)) as conn:
        conn.execute("DROP TABLE device_unlocks")
        conn.execute("UPDATE alembic_version SET version_num = 'ce59881171a4'")
        conn.commit()
    done = restore.restore(kept)
    assert (done.outcome, done.reason) == ("restored", "")


def test_the_server_profile_has_none_of_it(monkeypatch, tmp_path, restore_server):
    from tests.conftest import _setup_owner

    with _boot(monkeypatch, tmp_path / "server", profile=None) as client:
        assert client.get("/api/capabilities").json()["device_unlock"] is False
        _setup_owner(client)
        assert client.get("/api/me", headers=HEADERS).status_code == 200
        assert client.get("/api/me/device-unlock", headers=HEADERS).status_code in (404, 405)
        assert _turn_on(client).status_code in (404, 405)
        assert client.delete("/api/me/device-unlock", headers=HEADERS).status_code in (404, 405)
        assert _unlock(client, "anything").status_code in (404, 405)
        assert _rows() == []
