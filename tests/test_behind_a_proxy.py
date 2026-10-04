"""Who the app believes a request came from, and where it says it lives (#207).

Two halves. The address: uvicorn's proxy-header middleware believes
`X-Forwarded-For` only from `FORWARDED_ALLOW_IPS`, and `compose.yaml` sets that
to the Docker bridge -- so behind `tailscale serve` each tailnet peer is its own
address to the rate limiter, and a caller that is not the bridge cannot name
one. The origin: `SPENDTRACKER_PUBLIC_URL`, when set, is what invitation links
and the discovery documents are built from, rather than whatever the request
says.

The first half reads `compose.yaml` and wraps the app with the value found
there, so it tests the shipped setting rather than a copy of it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import LoginAttempt
from tests.conftest import HEADERS

# Reads compose.yaml, outside the backend; runs on every pull request.
pytestmark = pytest.mark.repo_wide

COMPOSE = Path(__file__).resolve().parent.parent / "compose.yaml"


def _compose_trusted_proxies() -> str:
    match = re.search(
        r'^\s*FORWARDED_ALLOW_IPS:\s*"\$\{FORWARDED_ALLOW_IPS:-([^}]*)\}"\s*$',
        COMPOSE.read_text(),
        re.M,
    )
    assert match, "compose.yaml no longer sets FORWARDED_ALLOW_IPS with a default"
    return match.group(1)


def test_the_container_trusts_a_network_and_never_everybody():
    trusted = _compose_trusted_proxies()
    assert trusted.strip() not in {"", "*"}, "'*' lets any caller name its own address"
    assert "/" in trusted, "a network, the bridge's, not a single guessed address"


def _recorded_ips(client) -> list[str | None]:
    with Session(client.app_module.db_engine) as own:
        return list(own.execute(select(LoginAttempt.ip).order_by(LoginAttempt.at)).scalars())


def _behind(client, peer: str) -> TestClient:
    """The same app, reached from ``peer``, through the middleware uvicorn
    puts in front of it with the container's setting. Not entered as a
    context manager: the app's lifespan is already running under ``client``."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    wrapped = ProxyHeadersMiddleware(
        client.app_module.app, trusted_hosts=_compose_trusted_proxies()
    )
    return TestClient(wrapped, base_url="https://testserver", client=(peer, 50000))


def _wrong_password(through: TestClient, forwarded_for: str):
    return through.post(
        "/api/session",
        json={"email": "somebody@example.com", "password": "not the password"},
        headers={**HEADERS, "X-Forwarded-For": forwarded_for},
    )


def test_behind_the_bridge_each_tailnet_peer_is_its_own_address(client):
    bridge = _behind(client, "172.17.0.1")
    assert _wrong_password(bridge, "100.64.0.7").status_code == 401
    assert _wrong_password(bridge, "100.64.0.8").status_code == 401

    assert _recorded_ips(client) == ["100.64.0.7", "100.64.0.8"]


def test_a_caller_that_is_not_the_bridge_cannot_name_its_own_address(client):
    stranger = _behind(client, "203.0.113.9")
    assert _wrong_password(stranger, "100.64.0.7").status_code == 401
    # A client-prepended value in front of the bridge's own does not win either:
    # uvicorn reads the list from the right.
    bridge = _behind(client, "172.17.0.1")
    assert _wrong_password(bridge, "1.2.3.4, 100.64.0.8").status_code == 401

    assert _recorded_ips(client) == ["203.0.113.9", "100.64.0.8"]


# --------------------------------------------------------------------------- #
# SPENDTRACKER_PUBLIC_URL
# --------------------------------------------------------------------------- #


@pytest.fixture()
def public_url(monkeypatch, client):
    """Set the variable and re-read the settings, as a restart would."""
    import app.config as config

    def set_to(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("SPENDTRACKER_PUBLIC_URL", raising=False)
        else:
            monkeypatch.setenv("SPENDTRACKER_PUBLIC_URL", value)
        monkeypatch.setattr(config, "settings", config.Settings.from_env())

    return set_to


def test_an_invitation_link_and_the_discovery_documents_use_the_public_url(client, public_url):
    from tests.conftest import _setup_owner

    _setup_owner(client)

    def invite_link() -> str:
        made = client.post("/api/admin/invitations", json={"role": "member"}, headers=HEADERS)
        assert made.status_code == 201, made.text
        return made.json()["link"]

    # Unset: from the request, as before.
    assert invite_link().startswith("https://testserver/invite/")

    public_url("https://spend.example.ts.net/")
    assert invite_link().startswith("https://spend.example.ts.net/invite/")
    assert "https://spend.example.ts.net/api" in client.get("/llms.txt").text
    assert "testserver" not in client.get("/llms.txt").text
    from app.api import discovery

    descriptor = client.get(discovery.WELL_KNOWN)
    assert descriptor.json()["base_url"] == "https://spend.example.ts.net"


@pytest.mark.parametrize(
    "value", ["spend.example.ts.net", "ftp://spend.example", "https://spend.example/sub"]
)
def test_a_public_url_that_is_not_an_origin_stops_the_boot(monkeypatch, value):
    import app.config as config

    monkeypatch.setenv("SPENDTRACKER_PUBLIC_URL", value)
    with pytest.raises(ValueError, match="SPENDTRACKER_PUBLIC_URL"):
        config.Settings.from_env()
