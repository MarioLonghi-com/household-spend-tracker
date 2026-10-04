"""Opaque credentials: 256 random bits, stored as a hash.

Sessions and trusted devices work the same way and for the same reason -- the
database lookup *is* the verification, so there is no signature and therefore no
dependency on the server key. A read of the database yields nothing usable.
"""

from __future__ import annotations

import hashlib
import secrets

#: 32 bytes. Long enough that guessing is not a threat model.
BYTES = 32


def issue() -> tuple[str, str]:
    """Return ``(value_for_the_cookie, hash_for_the_database)``."""
    value = secrets.token_urlsafe(BYTES)
    return value, fingerprint(value)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
