"""The three cookies -- their names and their flags, in one place so neither
can drift apart.

``Secure`` comes from ``settings.cookie_secure``, which is True unless somebody
turned it off by name. Chrome and Firefox treat ``http://localhost`` as a
trustworthy origin, so development works with it on -- and the deployment is
HTTPS over the tailnet, where WebAuthn will need a secure context anyway.

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
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Response

from ..config import settings

#: The names before the prefix. Nothing outside this module should use them:
#: ask `session_name()`, `device_name()` or `pending_name()`.
_SESSION = "st_session"
_DEVICE = "st_device"
_PENDING = "st_pending"

#: The prefix, when `Secure` is on. `Path=/` and no `Domain` are set below
#: whatever the flag, so `Secure` is the only condition left to meet.
HOST_PREFIX = "__Host-"


def _named(bare: str) -> str:
    return f"{HOST_PREFIX}{bare}" if settings.cookie_secure else bare


def session_name() -> str:
    return _named(_SESSION)


def device_name() -> str:
    return _named(_DEVICE)


def pending_name() -> str:
    return _named(_PENDING)


def session_value(cookies: Mapping[str, str]) -> str | None:
    """The session cookie a request carries, under the one name it may have."""
    return cookies.get(session_name())


def device_value(cookies: Mapping[str, str]) -> str | None:
    return cookies.get(device_name())


def pending_value(cookies: Mapping[str, str]) -> str | None:
    return cookies.get(pending_name())


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


def set_session(response: Response, value: str) -> None:
    _set(response, session_name(), value, settings.session_absolute_seconds)


def set_pending(response: Response, value: str) -> None:
    _set(response, pending_name(), value, PENDING_MAX_AGE)


def clear_pending(response: Response) -> None:
    _clear(response, pending_name())


def set_device(response: Response, value: str) -> None:
    _set(response, device_name(), value, settings.trusted_device_seconds)


def clear_session(response: Response) -> None:
    _clear(response, session_name())
