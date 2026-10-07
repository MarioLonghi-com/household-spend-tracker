"""Passkeys (WebAuthn): whether this instance may offer them, and to whom.

Passkeys are offered **only when the conditions are good for them** (#47,
decision 4). No screen shows a passkey button that can only fail, and every
deployment that cannot use them keeps password + code exactly as it is. The
browser checks its half (`isSecureContext`, `PublicKeyCredential` and its JSON
helpers); this module is the server's half, answered per request by
`GET /api/session/passkey/state`.

Available means all of:

- **an RP ID is configured** -- `SPENDTRACKER_RP_ID`, or the host of
  `SPENDTRACKER_PUBLIC_URL` (`config._rp_id`). Never the request's `Host`;
- **it is a domain name, not an IP literal.** Browsers refuse an IP as an RP
  ID, even over HTTPS;
- **this request's host is that RP ID.** Someone who opened the app at its
  tailnet `100.x` address, or at `make lan`'s LAN address, is on another
  origin, where a passkey for this name cannot be made or used;
- **the request arrived over HTTPS, or the host is `localhost`** -- the
  secure context WebAuthn needs;
- **the expected-origins list is not empty.** An assertion's
  `clientDataJSON` names the origin the browser was at, and it is checked
  against this list, which therefore has to be configured: a proxy does not
  change what the browser says, and nothing about the request can be trusted
  to infer it.

On HTTPS behind a proxy: `tailscale serve` terminates TLS and the app sees
plain HTTP from loopback or the Docker bridge. uvicorn believes the proxy's
`X-Forwarded-Proto` only from `FORWARDED_ALLOW_IPS`, and not every proxy sends
it. So a request also counts as HTTPS when the public URL this instance is
configured with is `https://` and the request reached it by that name -- unless
a header says outright that it arrived over plain HTTP. That is advisory, not
the gate: a browser on a page that is not a secure context has no
`navigator.credentials` to call, and the origin in `clientDataJSON` is checked
against `expected_origins` whatever this answer said.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from fastapi import Request

from .. import config
from ..hosts import _host_of, _is_ip_literal

LOCALHOST = "localhost"


class Unavailable(StrEnum):
    """Why this instance does not offer passkeys to this request. Stable
    words: the client chooses its sentence by them (#47 §3)."""

    #: Neither `SPENDTRACKER_RP_ID` nor `SPENDTRACKER_PUBLIC_URL` is set.
    NOT_CONFIGURED = "not_configured"
    #: The RP ID is an IP literal, which no browser accepts.
    IP_ADDRESS = "ip_address"
    #: The app was opened at another name or address than the RP ID.
    WRONG_HOST = "wrong_host"
    #: Plain HTTP on a host that is not `localhost`: not a secure context.
    INSECURE = "insecure"
    #: Nothing to check an assertion's origin against.
    NO_ORIGINS = "no_origins"


#: One sentence per reason, for an operator reading the JSON or the log. The
#: client words its own for the member.
DETAIL: dict[Unavailable, str] = {
    Unavailable.NOT_CONFIGURED: "Passkeys are not set up on this server: set SPENDTRACKER_PUBLIC_URL "
    "to the HTTPS address people open it at.",
    Unavailable.IP_ADDRESS: "Passkeys need a host name, and this server is configured with an IP address.",
    Unavailable.WRONG_HOST: "Passkeys work only at this server's own address.",
    Unavailable.INSECURE: "Passkeys need this app opened over HTTPS.",
    Unavailable.NO_ORIGINS: "Passkeys need the server's public address configured.",
}


@dataclass(frozen=True, slots=True)
class PasskeyState:
    available: bool
    reason: Unavailable | None = None
    #: Where passkeys do work, when it is somewhere: the public URL. Sent so the
    #: screen can say "open https://… instead"; `/llms.txt` already publishes it.
    address: str | None = None

    @property
    def detail(self) -> str | None:
        return DETAIL[self.reason] if self.reason else None


def _request_host(request: Request) -> str:
    return _host_of(request.headers.get("host", "")).lower().rstrip(".")


def _public_origin() -> str | None:
    """The configured public URL as an origin, if it names the RP ID."""
    settings = config.settings
    if settings.public_url and urlsplit(settings.public_url).hostname == settings.rp_id:
        return settings.public_url
    return None


def expected_origins(request: Request) -> list[str]:
    """What an assertion's or attestation's origin must be one of.

    The public URL, when its host is the RP ID. In development with the RP ID
    `localhost`, also `http://localhost:<this request's port>`: `make dev`
    takes `PORT=`, and a worktree on 8851 is as much "this machine" as one on
    8848. Only then, and only for `localhost`, is anything read from the
    request -- a host that can only be this machine.
    """
    settings = config.settings
    origins = [one for one in (_public_origin(),) if one]
    if (
        settings.environment == "development"
        and settings.rp_id == LOCALHOST
        and _request_host(request) == LOCALHOST
    ):
        port = request.url.port
        local = f"http://{LOCALHOST}" + (f":{port}" if port and port != 80 else "")
        if local not in origins:
            origins.append(local)
    return origins


def _over_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if forwarded == "http":
        return False
    return config.settings.public_url.startswith("https://") and _public_origin() is not None


def state(request: Request) -> PasskeyState:
    """Whether passkeys may be offered to this request, and if not, why."""
    settings = config.settings
    rp_id = settings.rp_id
    address = _public_origin()
    if not rp_id:
        return PasskeyState(False, Unavailable.NOT_CONFIGURED)
    if _is_ip_literal(rp_id):
        return PasskeyState(False, Unavailable.IP_ADDRESS)
    if _request_host(request) != rp_id:
        return PasskeyState(False, Unavailable.WRONG_HOST, address)
    if rp_id != LOCALHOST and not _over_https(request):
        return PasskeyState(False, Unavailable.INSECURE, address)
    if not expected_origins(request):
        return PasskeyState(False, Unavailable.NO_ORIGINS)
    return PasskeyState(True, address=address)
