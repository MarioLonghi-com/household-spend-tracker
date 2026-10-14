"""Nothing the app does writes to standard output. Issue #315.

The desktop build runs this backend as a child of its shell and speaks to it
over stdin and stdout, so a single byte the app prints is a corrupt frame. The
app is clean today -- logs go to stderr and files -- but nothing held it to
that except habit. Ruff's T20 stops a `print` in `app/`; this catches the rest:
a library handler on `sys.stdout`, SQLAlchemy's `echo`, anything a dependency
writes at the file-descriptor level, which `capfd` sees and `capsys` does not.

Boots a whole app from a fresh data directory, as `test_file_permissions`
does, so import, lifespan, first-boot setup and ordinary requests are all
inside the capture.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import HEADERS, _setup_owner, _stamp_head


def _boot(monkeypatch, data: Path) -> TestClient:
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
    return TestClient(main.app, base_url="https://testserver")


@pytest.fixture()
def restore_app(monkeypatch):
    """Rebuild the default app afterwards: the engine `app.db` keeps would
    otherwise stay bound to this test's temporary ledger for the next test."""
    yield
    import app.config as config
    import app.db as db
    import app.main as main

    monkeypatch.undo()
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(main)


@pytest.mark.parametrize("echo_sql", ["", "1"], ids=["plain", "echo-sql"])
def test_import_lifespan_setup_and_requests_print_nothing(
    monkeypatch, tmp_path, capfd, echo_sql, restore_app
):
    monkeypatch.setenv("SPENDTRACKER_ECHO_SQL", echo_sql)
    capfd.readouterr()

    with _boot(monkeypatch, tmp_path / "data") as client:
        _setup_owner(client)
        house = client.post("/api/households", json={"name": "Quiet"}, headers=HEADERS)
        assert house.status_code == 201, house.text
        listed = client.get("/api/households", headers=HEADERS)
        assert listed.status_code == 200, listed.text
        assert [h["name"] for h in listed.json()] == ["Quiet"]
        assert client.get("/api/health").status_code == 200

    out, err = capfd.readouterr()
    assert out == ""
    # The boot-time echo still shows the statements -- on standard error -- and
    # only when asked for.
    assert ("CREATE TABLE" in err) is bool(echo_sql)
    # ...and stops when lifespan hands over to the logging style, which at
    # `normal` writes no statement into `sql.log`.
    sql_log = tmp_path / "data" / "logs" / "sql.log"
    logged = sql_log.read_text() if sql_log.exists() else ""
    assert "INSERT INTO households" not in logged
    assert "INSERT INTO households" not in err
