"""One profile setting decides what a deployment offers. Issue #316.

The desktop app is the server's code with the features that need a network, a
second device or a second person left out. These tests boot the whole app
under each profile and assert what a caller can see: the capabilities the
client is told, which routes exist, and that a route left out answers exactly
as one that never existed -- not a 403, and not a refusal of its own.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import HEADERS, PASSWORD, _setup_owner, _stamp_head

#: What the desktop app has no use for, and the server keeps.
NETWORK_ONLY = {"snap", "agents", "invitations", "account_resets", "second_factor", "self_update", "db_view"}


def setup_desktop_owner(client) -> dict:
    """The desktop's wizard (#320): no token, no authenticator, one recovery code."""
    started = client.post(
        "/api/setup/begin",
        json={"token": "", "email": "Jane.Doe@example.test", "display_name": "Jane", "password": PASSWORD},
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    body = started.json()
    done = client.post("/api/setup/complete", json={"blob": body["blob"], "codes_saved": True}, headers=HEADERS)
    assert done.status_code == 200, done.text
    return {"user": done.json(), "recovery_codes": body["recovery_codes"], "started": body}


def _boot(monkeypatch, data: Path, *, profile: str | None) -> TestClient:
    if profile is None:
        monkeypatch.delenv("SPENDTRACKER_PROFILE", raising=False)
    else:
        monkeypatch.setenv("SPENDTRACKER_PROFILE", profile)
    monkeypatch.delenv("SPENDTRACKER_PUBLIC_URL", raising=False)
    monkeypatch.delenv("SPENDTRACKER_RP_ID", raising=False)
    monkeypatch.delenv("SPENDTRACKER_ALLOWED_HOSTS", raising=False)
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
    # The desktop shell sends `Host: localhost` and nothing else.
    host = "localhost" if profile == "desktop" else "testserver"
    return TestClient(main.app, base_url=f"https://{host}")


@pytest.fixture()
def restore_server(monkeypatch):
    """Put the default app back for the tests after this one: `app.main` is a
    module, and the suite's other tests import it as they find it."""
    yield
    monkeypatch.delenv("SPENDTRACKER_PROFILE", raising=False)
    import app.config as config
    import app.db as db
    import app.main as main

    monkeypatch.undo()
    # All three: `app.db` keeps the engine, and left bound to this test's
    # temporary ledger it hands the next test in this worker a deleted file.
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(main)


def _gated_routes(main) -> list[tuple[str, str, str]]:
    """(capability, method, path) for every route a profile can leave out, read
    from the server app as built: the routers `app.main` includes only when
    their capability is on, and the app's own tagged routes."""
    from app import capabilities

    found = []
    for router, need in main._ROUTERS:
        if need is None:
            continue
        for route in router.routes:
            for method in sorted(route.methods - {"HEAD"}):
                found.append((need, method, main.API_PREFIX + route.path))
    for route in main.app.router.routes:
        need = capabilities.needed_by(route)
        if need is not None:
            for method in sorted(route.methods - {"HEAD"}):
                found.append((need, method, route.path))
    return found


def test_the_server_profile_offers_everything_it_did(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "data", profile=None) as client:
        offered = client.get("/api/capabilities")
        assert offered.status_code == 200
        # Passkeys stay off only because no public URL is set here, as before.
        assert offered.json() == {
            "snap": True, "agents": True, "invitations": True, "account_resets": True,
            "passkeys": False, "second_factor": True, "password_reset_by_code": False, "self_update": True,
            "upstream_check": True, "db_view": True, "ynab_import": True,
        }
        import app.main as main

        assert main.UNOFFERED == []


def test_the_desktop_profile_offers_only_what_needs_nobody_else(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "data", profile="desktop") as client:
        offered = client.get("/api/capabilities").json()
    assert {name for name, on in offered.items() if not on} == NETWORK_ONLY | {"passkeys"}
    assert {name for name, on in offered.items() if on} == {"upstream_check", "ynab_import", "password_reset_by_code"}


def test_a_route_left_out_answers_as_one_that_never_existed(monkeypatch, tmp_path, restore_server):
    import app.main as main
    from app import capabilities
    from app.config import Settings

    desktop = Settings(database_url="", data_dir=Path("."), secret_key="", echo_sql=False, profile="desktop")
    off = {name for name, on in capabilities.for_settings(desktop).as_dict().items() if not on}
    gated = [one for one in _gated_routes(main) if one[0] in off]
    # The premise: every capability that leaves routes out has some, and the
    # agent API, the keys, invitations, resets, the updater, the authenticator
    # and /snap are among them.
    assert {need for need, _, _ in gated} == {
        "agents", "invitations", "account_resets", "self_update", "snap", "second_factor",
    }
    for expected in ("/api/me/keys", "/api/admin/invitations", "/api/admin/resets", "/snap", "/llms.txt", "/api/me/authenticator"):
        assert any(path == expected for _, _, path in gated), expected

    with _boot(monkeypatch, tmp_path / "data", profile="desktop") as client:
        setup_desktop_owner(client)
        for need, method, path in gated:
            concrete = re.sub(r"\{[^}]+\}", "x", path)
            answer = client.request(method, concrete, headers=HEADERS)
            # A sibling that was never a route, under the same prefix and method.
            invented = client.request(method, concrete.rsplit("/", 1)[0] + "/no-such-route", headers=HEADERS)
            said = (answer.status_code, answer.headers.get("content-type"), answer.content)
            assert said == (invented.status_code, invented.headers.get("content-type"), invented.content), (
                need, method, path,
            )
            # Whatever a path nobody claims gets: 404, and once the built
            # client is served, its shell for a GET outside /api/ and 405 for
            # any other method -- the same for an invented path as for one
            # left out, which is the point.
            assert invented.status_code in (200, 404, 405)


def test_what_the_desktop_keeps_works_for_two_households(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "data", profile="desktop") as client:
        setup_desktop_owner(client)
        ids = []
        for name in ("Ours", "The flat"):
            made = client.post("/api/households", json={"name": name}, headers=HEADERS)
            assert made.status_code == 201, made.text
            ids.append(made.json()["id"])
        listed = client.get("/api/households", headers=HEADERS)
        assert sorted(h["name"] for h in listed.json()) == ["Ours", "The flat"]
        for household_id in ids:
            accounts = client.get(f"/api/households/{household_id}/accounts", headers=HEADERS)
            assert accounts.status_code == 200, accounts.text
            assert accounts.json() == []


def test_the_same_routes_reach_their_handlers_on_the_server(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "data", profile=None) as client:
        _setup_owner(client)
        keys = client.get("/api/me/keys", headers=HEADERS)
        assert (keys.status_code, keys.json()) == (200, [])
        invitations = client.get("/api/admin/invitations", headers=HEADERS)
        assert (invitations.status_code, invitations.json()) == (200, [])
        resets = client.get("/api/admin/resets", headers=HEADERS)
        assert (resets.status_code, resets.json()) == (200, [])
        assert client.get("/llms.txt").status_code == 200


def test_hsts_is_the_servers_alone(monkeypatch, tmp_path, restore_server):
    with _boot(monkeypatch, tmp_path / "server", profile=None) as client:
        assert "strict-transport-security" in client.get("/api/health").headers
    with _boot(monkeypatch, tmp_path / "desktop", profile="desktop") as client:
        assert "strict-transport-security" not in client.get("/api/health").headers


@pytest.mark.parametrize(
    ("variable", "value", "says"),
    [
        ("SPENDTRACKER_PUBLIC_URL", "https://spend.example.ts.net", "PUBLIC_URL is not used"),
        ("SPENDTRACKER_RP_ID", "spend.example.ts.net", "RP_ID is not used"),
        ("SPENDTRACKER_ALLOWED_HOSTS", "localhost,spend.example.ts.net", "can only be localhost"),
    ],
)
def test_the_desktop_profile_refuses_network_settings_at_boot(monkeypatch, tmp_path, variable, value, says):
    from app.config import Settings

    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPENDTRACKER_PROFILE", "desktop")
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=says):
        Settings.from_env()


def test_the_desktop_profile_allows_only_localhost(monkeypatch, tmp_path):
    from app.config import Settings

    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPENDTRACKER_PROFILE", "desktop")
    monkeypatch.delenv("SPENDTRACKER_ALLOWED_HOSTS", raising=False)
    assert Settings.from_env().allowed_hosts == ("localhost",)
    monkeypatch.setenv("SPENDTRACKER_ALLOWED_HOSTS", "localhost")
    assert Settings.from_env().allowed_hosts == ("localhost",)


def test_an_unknown_profile_is_refused(monkeypatch, tmp_path):
    from app.config import Settings

    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPENDTRACKER_PROFILE", "laptop")
    with pytest.raises(ValueError, match="must be one of server, desktop"):
        Settings.from_env()


def test_a_capability_tag_must_name_one():
    from app import capabilities

    with pytest.raises(ValueError, match="no capability called 'snapp'"):
        capabilities.needs("snapp")
