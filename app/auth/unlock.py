"""The secret a desktop shell presents after a biometric prompt (#432).

"Open with Touch ID": the person who turns it on in Profile, after typing the
password in that session, opens the app at the next launch with Touch ID
instead. The password stays the recovery path and is never stored. What the
shell keeps is a secret issued *here*, for this one purpose, bound to this
ledger -- 256 random bits whose hash is a row in `device_unlocks`, exactly as a
session or a trusted device, so a read of the database yields nothing usable
and a secret from one ledger opens nothing in another.

The backend decides. Sign-in by that secret is a desktop capability
(`app.capabilities.device_unlock`), refused on a server by the route not being
there, and recorded in `login_attempts` like a password sign-in. The biometric
prompt gates the shell's *use* of the secret, not its storage: a failed or
cancelled prompt simply leaves the password screen, with nothing counted.

What revokes it, and `revoke_all_for` is called from each: turning it off in
Profile, a password change, a recovery code (*Forgot your password?*), an
account reset, disabling the account, and a restore from backup
(`app.desktop.restore`, which empties the table in the restored ledger).
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..errors import Unauthorized
from ..models import DeviceUnlock, User, utcnow
from . import ratelimit, tokens

#: The response header the secret travels in, once, on the answer to
#: `POST /me/device-unlock`. A header and not the body: the desktop shell takes
#: it off the answer before the page sees it (`desktop/src/unlock.ts`), the way
#: it takes `Set-Cookie`, so the window that asked never holds the secret.
HEADER = "X-Spend-Tracker-Device-Unlock"

#: The methods a shell may name. Windows Hello would be added here (#432).
METHODS = frozenset({"touch_id"})

#: Its own counter in `login_attempts`, like "totp" and "recovery".
KIND = "unlock"

_REFUSAL = "that unlock secret is not right, or has been revoked"


def current(session: Session, user: User) -> DeviceUnlock | None:
    """The row that stands for this person, if any."""
    return session.execute(
        select(DeviceUnlock).where(DeviceUnlock.user_id == user.id)
    ).scalar_one_or_none()


def issue(session: Session, user: User, *, method: str) -> str:
    """Replace whatever stood with one new secret, and return its value.

    One row per person: the shell on one computer holds one secret, and a
    second "turn on" -- after the shell lost its copy, say -- must not leave
    the first one working somewhere.
    """
    if method not in METHODS:
        raise ValueError(f"no unlock method called {method!r}")
    revoke_all_for(session, user.id)
    value, digest = tokens.issue()
    session.add(DeviceUnlock(id_hash=digest, user_id=user.id, method=method, created_at=utcnow()))
    session.flush()
    return value


def revoke_all_for(session: Session, user_id: str) -> int:
    """What turning it off, a password change, a recovery code and an account
    reset do."""
    table = DeviceUnlock.__table__
    result = session.execute(  # audit-exempt: device_unlocks are not audited
        delete(table).where(table.c.user_id == user_id)
    )
    return result.rowcount or 0


def check(session: Session, engine: Engine, *, secret: str, ip: str | None) -> User:
    """The person this secret opens, or a refusal. Records the attempt.

    The lookup by hash is the verification, as for a session cookie. An
    attempt is reserved first and released on success, so it lands in
    `login_attempts` either way -- audited like a password sign-in -- and a
    process guessing at the door is counted per address like any other.
    Nobody is named until the hash matches, so an unknown secret is counted
    against no account.
    """
    row = session.execute(
        select(DeviceUnlock).where(DeviceUnlock.id_hash == tokens.fingerprint(secret or ""))
    ).scalar_one_or_none()
    user = session.get(User, row.user_id) if row is not None else None
    held = ratelimit.reserve(
        engine, email_canonical=user.email_canonical if user else "-", ip=ip, kind=KIND
    )
    if row is None or user is None or user.disabled_at is not None:
        raise Unauthorized(_REFUSAL, code="auth.device_unlock_refused")
    ratelimit.release(engine, held)
    row.last_used_at = utcnow()
    return user
