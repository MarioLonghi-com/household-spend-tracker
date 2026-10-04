"""The `Host` header, and the documents rendered from it. Issue #94.

Nothing checked `Host`. `/llms.txt`, `/.well-known/llms.txt` and the agent
descriptor build their `base_url` from it and were served `public,
max-age=3600`, and the CSRF check compares `Origin` against it -- so DNS
rebinding reached every anonymous surface.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:8848", "localhost.evil.example", "ts.net.evil.example"],
)
def test_a_host_nobody_listed_is_refused(client, host):
    answer = client.get("/llms.txt", headers={"host": host})
    assert answer.status_code == 400
    assert "evil" not in answer.text
    # A refusal like any other.
    assert answer.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize(
    "host",
    [
        "testserver",
        "localhost:8848",
        "127.0.0.1:8848",
        "[::1]:8848",
        # `make lan`: bound to 0.0.0.0 and reached at whatever DHCP handed out.
        "192.168.1.50:8848",
        # A tailnet address, and the tailnet name `tailscale serve` answers on.
        "100.101.102.103",
        "spend.tail1234.ts.net",
    ],
)
def test_every_way_this_app_is_reached_today_still_works(client, host):
    answer = client.get("/llms.txt", headers={"host": host})
    assert answer.status_code == 200
    # And the host is what the document was rendered for.
    assert host in answer.text


def test_the_discovery_documents_are_not_cached(client):
    """Rendered from `Host`, so a shared cache must never hold one."""
    for path in ("/llms.txt", "/.well-known/llms.txt"):
        assert client.get(path).headers["Cache-Control"] == "no-store", path

    from app.api import discovery

    assert client.get(discovery.WELL_KNOWN).headers["Cache-Control"] == "no-store"


def test_setting_the_list_replaces_the_default(monkeypatch, tmp_path):
    """Named hosts only: once somebody sets it, IP literals are not implied."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'hosts.sqlite3'}")
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPENDTRACKER_ALLOWED_HOSTS", "Spend.Example, *.home.arpa")

    import app.config as config

    importlib.reload(config)
    import app.db as db_module

    importlib.reload(db_module)
    from app.models import Base

    Base.metadata.create_all(db_module.engine)
    from tests.conftest import _stamp_head

    _stamp_head(db_module.engine)
    import app.main as main

    importlib.reload(main)
    try:
        with TestClient(main.app, base_url="https://testserver") as live:
            assert live.get("/llms.txt", headers={"host": "spend.example"}).status_code == 200
            assert live.get("/llms.txt", headers={"host": "nas.home.arpa"}).status_code == 200
            assert live.get("/llms.txt", headers={"host": "192.168.1.50"}).status_code == 400
            assert live.get("/llms.txt", headers={"host": "testserver"}).status_code == 400
    finally:
        monkeypatch.delenv("SPENDTRACKER_ALLOWED_HOSTS")
        importlib.reload(config)
