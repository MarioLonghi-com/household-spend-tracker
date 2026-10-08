"""The `Host` header, checked against `SPENDTRACKER_ALLOWED_HOSTS`.

Nothing checked it before. `/llms.txt`, `/.well-known/llms.txt` and the agent
descriptor build their `base_url` from it, and the CSRF check compares `Origin`
against it -- so a page on `evil.example` that re-pointed its own DNS at this
machine (DNS rebinding) was talking to this app, same-origin as far as the
browser knew, with every anonymous surface answering it.

Starlette's `TrustedHostMiddleware`, with one addition: the `ip` entry, which
admits any host written as an IP literal. See `config.DEFAULT_ALLOWED_HOSTS`
for why that is safe and why `make lan` needs it.
"""

from __future__ import annotations

from ipaddress import ip_address

from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import IP_LITERALS


def _host_of(header: str) -> str:
    """The host in a `Host` header, port dropped: `[::1]:8000` -> `::1`.

    Ours rather than Starlette's: its parser lives in `starlette._utils`, which
    is private and has come and gone between releases.
    """
    if header.startswith("["):
        return header[1:].partition("]")[0]
    return header.rpartition(":")[0] if header.count(":") == 1 else header


def request_host(request: HTTPConnection) -> str:
    """The host a request named, port dropped, lower-cased, no trailing dot.

    Only meaningful once `AllowedHosts` has let the request through: before
    that it is whatever the client wrote. The passkey checks and the cookie
    names both read it from here, so they cannot parse it two ways.
    """
    return _host_of(request.headers.get("host", "")).lower().rstrip(".")


def _is_ip_literal(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        return False
    return True


class AllowedHosts(TrustedHostMiddleware):
    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...]) -> None:
        self.ip_literals = IP_LITERALS in allowed_hosts
        # `www_redirect` off: a redirect to a host nobody listed is not an
        # answer this should give.
        super().__init__(
            app,
            allowed_hosts=[one for one in allowed_hosts if one != IP_LITERALS] or ["localhost"],
            www_redirect=False,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.ip_literals and scope["type"] in ("http", "websocket"):
            host = Headers(scope=scope).get("host")
            if host and _is_ip_literal(_host_of(host)):
                await self.app(scope, receive, send)
                return
        await super().__call__(scope, receive, send)
