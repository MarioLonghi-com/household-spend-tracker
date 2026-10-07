"""When an instance may offer passkeys, and the RP ID it would bind them to (#119).

Each test sets the environment the way an operator would, re-reads the
settings as a restart would, and asks `GET /api/session/passkey/state` from
the address a browser would be at. The rows are the ones in #47 §1.1 that a
test can reach: no RP ID, an IP address, plain HTTP, the wrong host, and the
two good configurations -- the tailnet name over HTTPS and `localhost` in
development. Every answer is asserted whole, reason and all, not just its
status.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import HEADERS

PUBLIC = "https://spend.example.ts.net"


@pytest.fixture()
def configure(monkeypatch, client):
    """Set the variables and re-read the settings, as a restart would."""
    import app.config as config

    def set_to(public_url: str | None = None, rp_id: str | None = None, env: str | None = None):
        for name, value in (
            ("SPENDTRACKER_PUBLIC_URL", public_url),
            ("SPENDTRACKER_RP_ID", rp_id),
            ("SPENDTRACKER_ENV", env),
        ):
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        monkeypatch.setattr(config, "settings", config.Settings.from_env())
        return config.settings

    return set_to


def _state(client, at: str, **headers) -> dict:
    """The state answer, asked by a browser that opened the app at ``at``."""
    with TestClient(client.app_module.app, base_url=at) as browser:
        answer = browser.get("/api/session/passkey/state", headers={**HEADERS, **headers})
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_nothing_configured_offers_nothing(client, configure):
    settings = configure()
    assert settings.rp_id == ""
    assert _state(client, PUBLIC) == {
        "available": False,
        "reason": "not_configured",
        "detail": "Passkeys are not set up on this server: set SPENDTRACKER_PUBLIC_URL "
        "to the HTTPS address people open it at.",
        "address": None,
    }


def test_the_rp_id_is_never_taken_from_the_request(client, configure):
    """Without a configured name, a request at a perfectly good host name is
    still refused: `Host` is whatever the client wrote."""
    configure()
    assert _state(client, "https://other.example.ts.net")["reason"] == "not_configured"


def test_the_tailnet_name_over_https_is_available(client, configure):
    settings = configure(public_url=PUBLIC + "/")
    assert settings.rp_id == "spend.example.ts.net"
    assert _state(client, PUBLIC) == {
        "available": True,
        "reason": None,
        "detail": None,
        "address": PUBLIC,
    }


def test_behind_a_proxy_that_terminates_tls_it_is_still_available(client, configure):
    """`tailscale serve` hands the app plain HTTP. The browser was on HTTPS,
    the public URL says so, and the name matches."""
    configure(public_url=PUBLIC)
    assert _state(client, "http://spend.example.ts.net")["available"] is True


def test_a_proxy_that_says_it_was_plain_http_is_believed(client, configure):
    configure(public_url=PUBLIC)
    answer = _state(client, "http://spend.example.ts.net", **{"X-Forwarded-Proto": "http"})
    assert (answer["available"], answer["reason"]) == (False, "insecure")


def test_plain_http_on_a_name_is_not_a_secure_context(client, configure):
    configure(public_url="http://spend.example.ts.net:8848")
    answer = _state(client, "http://spend.example.ts.net:8848")
    assert answer == {
        "available": False,
        "reason": "insecure",
        "detail": "Passkeys need this app opened over HTTPS.",
        "address": "http://spend.example.ts.net:8848",
    }


@pytest.mark.parametrize("at", ["https://192.168.1.50:8848", "https://100.64.0.7", "https://other.example.ts.net"])
def test_the_app_opened_at_another_address_is_the_wrong_host(client, configure, at):
    """`make lan`'s LAN address, a tailnet 100.x address, another name: each
    is another origin, and the answer says where passkeys do work."""
    configure(public_url=PUBLIC)
    assert _state(client, at) == {
        "available": False,
        "reason": "wrong_host",
        "detail": "Passkeys work only at this server's own address.",
        "address": PUBLIC,
    }


def test_an_ip_public_url_keeps_passkeys_off(client, configure):
    settings = configure(public_url="https://192.168.1.50:8443")
    assert settings.rp_id == "192.168.1.50"
    answer = _state(client, "https://192.168.1.50:8443")
    assert (answer["available"], answer["reason"]) == (False, "ip_address")


def test_localhost_in_development_is_available_on_whatever_port_it_runs(client, configure):
    settings = configure(rp_id="localhost", env="development")
    assert settings.rp_id == "localhost"
    # Plain HTTP: localhost is a secure context by itself.
    assert _state(client, "http://localhost:8851")["available"] is True

    from starlette.requests import Request

    from app.auth import passkeys

    def origins(host: str) -> list[str]:
        scope = {"type": "http", "scheme": "http", "server": (host, 8851), "path": "/",
                 "headers": [(b"host", f"{host}:8851".encode())], "query_string": b""}
        return passkeys.expected_origins(Request(scope))

    assert origins("localhost") == ["http://localhost:8851"]
    # A request at another host gets nothing from the request.
    assert origins("127.0.0.1") == []


def test_the_public_url_and_localhost_together_in_development(client, configure):
    configure(public_url=PUBLIC, rp_id="localhost", env="development")
    assert _state(client, "http://localhost:8848")["available"] is True
    # The public name is not the RP ID now, so it is the wrong host there.
    assert _state(client, PUBLIC)["reason"] == "wrong_host"


def test_a_set_rp_id_equal_to_the_public_urls_host_is_accepted(configure, client):
    assert configure(public_url=PUBLIC, rp_id="Spend.Example.TS.net.").rp_id == "spend.example.ts.net"


@pytest.mark.parametrize(
    ("public_url", "rp_id", "env"),
    [
        # Wider than the host: every other tailnet node could ask for assertions.
        (PUBLIC, "example.ts.net", None),
        # Another host altogether.
        (PUBLIC, "spend.example.org", None),
        # No public URL to agree with.
        (None, "spend.example.ts.net", None),
        # localhost outside development.
        (PUBLIC, "localhost", None),
        (None, "localhost", "production"),
    ],
)
def test_boot_refuses_an_rp_id_that_is_not_the_public_urls_host(monkeypatch, public_url, rp_id, env):
    import app.config as config

    for name, value in (
        ("SPENDTRACKER_PUBLIC_URL", public_url),
        ("SPENDTRACKER_RP_ID", rp_id),
        ("SPENDTRACKER_ENV", env),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="SPENDTRACKER_RP_ID must be the host of SPENDTRACKER_PUBLIC_URL"):
        config.Settings.from_env()


def test_the_state_answer_is_the_same_signed_in_or_not(client, configure):
    """It describes the server and the request, never an account."""
    from tests.conftest import _setup_owner

    configure(public_url=PUBLIC)
    signed_out = _state(client, PUBLIC)
    _setup_owner(client)
    signed_in = client.get("/api/session/passkey/state", headers=HEADERS).json()
    assert signed_out["available"] is True
    # The suite's own client is at https://testserver: the wrong host.
    assert signed_in == {**signed_out, "available": False, "reason": "wrong_host",
                         "detail": "Passkeys work only at this server's own address."}


def test_every_page_may_use_passkeys_except_snap(client):
    from tests.test_security_headers import _permissions

    for path in ("/api/health", "/", "/api/session/passkey/state"):
        policy = _permissions(client.get(path, headers=HEADERS))
        assert policy["publickey-credentials-get"] == "(self)", path
        assert policy["publickey-credentials-create"] == "(self)", path
    snap = _permissions(client.get("/snap", follow_redirects=False))
    assert snap["publickey-credentials-get"] == "()"
    assert snap["publickey-credentials-create"] == "()"
    assert snap["geolocation"] == "(self)"
