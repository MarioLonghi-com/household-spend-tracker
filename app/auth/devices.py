"""Trusted devices: "this browser already passed 2FA".

Separate from the session on purpose. The session says who you are right now;
this says the browser has already proved the second factor, so it need not be
asked again for thirty days. Different lifetime, different revocation.

The cookie holds a **list** of tokens rather than one. Two people share a laptop
in most households, and one cookie name at one path holds one value -- so
storing a single token means the second person to sign in silently overwrites
the first, who is then re-prompted despite having a perfectly good unexpired
record. Up to four, oldest evicted.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import TrustedDevice, User, utcnow
from . import tokens

SEPARATOR = "."
MAX_PER_BROWSER = 4


def parse_cookie(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part for part in raw.split(SEPARATOR) if part][:MAX_PER_BROWSER]


def build_cookie(existing: list[str], new_value: str) -> str:
    """Newest first, oldest evicted past the cap."""
    kept = [v for v in existing if v != new_value][: MAX_PER_BROWSER - 1]
    return SEPARATOR.join([new_value, *kept])


def issue(session: Session, user: User, *, label: str | None = None) -> str:
    value, digest = tokens.issue()
    now = utcnow()
    session.add(
        TrustedDevice(
            id_hash=digest,
            user_id=user.id,
            created_at=now,
            last_used_at=now,
            #: Fixed from issuance and deliberately non-sliding. Trust that
            #: renews itself on use means a stolen laptop is trusted forever.
            expires_at=now + timedelta(seconds=settings.trusted_device_seconds),
            label=(label or "")[:120] or None,
        )
    )
    return value


def is_trusted(
    session: Session, user: User, raw_cookie: str | None, *, now: datetime | None = None
) -> TrustedDevice | None:
    """Does this browser already have a live trust record for *this* user?"""
    now = now or utcnow()
    candidates = parse_cookie(raw_cookie)
    if not candidates:
        return None
    digests = [tokens.fingerprint(v) for v in candidates]
    row = session.execute(
        select(TrustedDevice).where(
            TrustedDevice.id_hash.in_(digests),
            TrustedDevice.user_id == user.id,
            TrustedDevice.revoked_at.is_(None),
            TrustedDevice.expires_at > now,
        )
    ).scalars().first()
    if row is not None:
        # Recording use does not extend the window; see expires_at above.
        row.last_used_at = now
    return row


def holds_live_trust(session: Session, user_id: str, raw_cookie: str | None) -> bool:
    """`is_trusted` without the `last_used_at` write: a read-only check.

    What the sign-in lockout asks before the password is judged, when writing
    "this browser was used" would be claiming something not yet proved. Takes
    an id rather than a user so the caller can ask the same question, with the
    same one indexed read, about an address that has no account -- the answer
    is then always no, and the time taken says nothing about which it was.
    """
    candidates = parse_cookie(raw_cookie)
    if not candidates:
        return False
    return (
        session.execute(
            select(TrustedDevice.id_hash).where(
                TrustedDevice.id_hash.in_([tokens.fingerprint(v) for v in candidates]),
                TrustedDevice.user_id == user_id,
                TrustedDevice.revoked_at.is_(None),
                TrustedDevice.expires_at > utcnow(),
            )
        ).first()
        is not None
    )


def revoke_all_for(session: Session, user_id: str) -> int:
    """What a password change, a TOTP re-enrolment or "forget my devices" does."""
    table = TrustedDevice.__table__
    result = session.execute(  # audit-exempt: trusted_devices are not audited
        delete(table).where(table.c.user_id == user_id)
    )
    return result.rowcount or 0


def list_for(session: Session, user_id: str, *, now: datetime | None = None) -> list[TrustedDevice]:
    now = now or utcnow()
    return list(
        session.execute(
            select(TrustedDevice)
            .where(
                TrustedDevice.user_id == user_id,
                TrustedDevice.revoked_at.is_(None),
                TrustedDevice.expires_at > now,
            )
            .order_by(TrustedDevice.last_used_at.desc())
        ).scalars()
    )
