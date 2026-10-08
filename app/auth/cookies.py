"""The three cookies -- their names and their flags, in one place so neither
can drift apart.

``Secure`` comes from ``settings.cookie_secure``, which is True unless somebody
turned it off by name. Browsers treat ``http://localhost`` as a trustworthy
origin, so development works with it on -- and the deployment is HTTPS over
the tailnet, where WebAuthn will need a secure context anyway.

The one case that needs it off is a plain-HTTP instance reached at a LAN
address, where the browser stores neither cookie and the sign-in loops with no
error anywhere. ``app/config.py`` carries the full reasoning; the flag is read
here rather than passed in so the two cookies cannot end up disagreeing.

**The names follow the same flag** (#209). With ``Secure`` on they carry the
``__Host-`` prefix, which makes the browser refuse the cookie unless it was
set by a secure origin, with ``Path=/`` and no ``Domain``. Cookies are not
isolated by port, so without it anything else on this host -- another service
on another port, a local user's listener on ``localhost:9999`` -- could plant
its own ``st_session`` for the host, and the victim would land in the
planter's account, where their next bank import landed too. The prefix needs
``Secure``, so with it off the bare names stay: one decision, read from the
same setting as the flag, and every reader asks this module for the name
rather than spelling it. ``tests/test_security_headers.py`` greps for anyone
spelling it.

**Except on loopback** (#196). Safari keeps a plain ``Secure`` cookie from
``http://localhost`` but drops any ``__Host-`` or ``__Secure-`` one there
(WebKit bug 218980), so a Safari user of the container on their own computer
signed in, lost the cookie, and was back at the sign-in screen with nothing in
either log. So when the request named ``localhost``, ``127.0.0.1`` or
``[::1]``, the three cookies go out under their bare names, ``Secure`` still
on. Every other host keeps the prefix. The host is the one the request named,
read after `AllowedHosts` has admitted it, and it decides the name for
reading, writing and clearing alike: each function below takes the request,
so no caller can set under one name and read under the other. A cookie
carrying the other name is simply not this app's.

What that gives up, on loopback only. The prefix's job was the planting above,
and a bare name lets any other local service on another port set
``st_session`` for ``localhost`` again. What it can plant is bounded by the
session store: a session is a random value whose hash is a row here, so the
planter cannot forge one -- it can only hand over a session it already holds
on *this* instance (landing the victim in the planter's account, the login
CSRF the prefix was for) or a junk value (signing the victim out). A planted
``st_device`` vouches only for the user it was issued to, so at worst it costs
the victim one extra code. Three things keep that small. Only a response from
``localhost`` can set a ``localhost`` cookie, so the planter is a process on
this machine *and* holds a session on this ledger. Such a process is already
sent every one of these cookies, prefix or not, whenever the browser asks it
for anything -- a ``localhost`` cookie goes to every port, and ``HttpOnly``
hides it from scripts, not from servers -- so it could take the victim's
session outright, which is worse than swapping it. And the navigation names
the account it is signed in as, on every screen. There is no cheap binding to
add: whatever a session could be tied to -- the host, the browser's headers --
the planter can match, because it chose the client it signed in with and
sees the victim's requests. The tailnet and every other name keep ``__Host-``,
which is the deployment this is designed for.
"""

from __future__ import annotations

from fastapi import Response
from starlette.requests import HTTPConnection

from ..config import settings
from ..hosts import request_host

#: The names before the prefix. Nothing outside this module should use them:
#: ask `session_name(host)`, `device_name(host)` or `pending_name(host)`, or
#: hand the request to the functions below.
_SESSION = "st_session"
_DEVICE = "st_device"
_PENDING = "st_pending"

#: The prefix, when `Secure` is on and the host is not loopback. `Path=/` and
#: no `Domain` are set below whatever the flag, so `Secure` is the only
#: condition left to meet.
HOST_PREFIX = "__Host-"

#: Where the names go bare (#196), as `hosts.request_host` spells them: port
#: dropped, and `[::1]` without its brackets. The three loopback names in
#: `config.DEFAULT_ALLOWED_HOSTS`, and no others -- `127.0.0.2` is loopback
#: too, but nobody types it, and a name added here is a host that loses the
#: prefix.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def _named(bare: str, host: str) -> str:
    if settings.cookie_secure and not is_loopback(host):
        return f"{HOST_PREFIX}{bare}"
    return bare


# The names take the *host*, so a test can ask what a given address is sent
# without building a request. Everything else takes the request.


def session_name(host: str) -> str:
    return _named(_SESSION, host)


def device_name(host: str) -> str:
    return _named(_DEVICE, host)


def pending_name(host: str) -> str:
    return _named(_PENDING, host)


def session_value(request: HTTPConnection) -> str | None:
    """The session cookie a request carries, under the one name it may have
    at the host it named."""
    return request.cookies.get(session_name(request_host(request)))


def device_value(request: HTTPConnection) -> str | None:
    return request.cookies.get(device_name(request_host(request)))


def pending_value(request: HTTPConnection) -> str | None:
    return request.cookies.get(pending_name(request_host(request)))


def carries_session(request: HTTPConnection) -> bool:
    return session_name(request_host(request)) in request.cookies


def _set(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear(response: Response, name: str) -> None:
    """With the same flags it was set with: a browser will not let a
    ``__Host-`` cookie be overwritten -- deleting is overwriting -- by a
    ``Set-Cookie`` that is not ``Secure``."""
    response.delete_cookie(
        name, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )


#: Between the password and the code, the half-finished sign-in lives in a
#: short-lived cookie rather than server state. It is set *here* rather than in
#: the router that used to own it: it was the one cookie writing its own flags
#: inline, so `Secure` stayed hardcoded on it after the other two learned to
#: read the setting -- and a sign-in that gets past the password and then
#: cannot hold its pending cookie fails on the *second* step, which looks like
#: a broken authenticator rather than a dropped cookie.
PENDING_MAX_AGE = 5 * 60


def set_session(response: Response, request: HTTPConnection, value: str) -> None:
    _set(response, session_name(request_host(request)), value, settings.session_absolute_seconds)


def set_pending(response: Response, request: HTTPConnection, value: str) -> None:
    _set(response, pending_name(request_host(request)), value, PENDING_MAX_AGE)


def clear_pending(response: Response, request: HTTPConnection) -> None:
    _clear(response, pending_name(request_host(request)))


def set_device(response: Response, request: HTTPConnection, value: str) -> None:
    _set(response, device_name(request_host(request)), value, settings.trusted_device_seconds)


def clear_session(response: Response, request: HTTPConnection) -> None:
    _clear(response, session_name(request_host(request)))
