"""Authenticator codes, and making a used code dead.

``pyotp.TOTP.verify(code, valid_window=1)`` reports *that* a code matched and
never *which* timestep it matched, so it cannot support replay prevention. The
window is walked by hand here so the matching step can be recorded and refused
next time.
"""

from __future__ import annotations

import hmac
import time

import pyotp
from sqlalchemy import inspect, or_, update
from sqlalchemy.orm.attributes import set_committed_value

from ..models import User
from . import crypto

#: The TOTP standard's step, and what every authenticator app assumes.
STEP_SECONDS = 30
#: One step either side, for clock drift between the phone and the server.
DRIFT_STEPS = 1
ISSUER = "Spend Tracker"


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, email: str) -> str:
    """The ``otpauth://`` URI an authenticator scans.

    Rendered as a QR in the browser rather than as an image from the server, so
    the secret never lands in an image cache or a proxy log.
    """
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def code_matches(secret: str, code: str, *, at: int | None = None) -> int | None:
    """Return the timestep a code matches, or None. No state is touched."""
    now = int(time.time()) if at is None else at
    current = now // STEP_SECONDS
    totp = pyotp.TOTP(secret)
    candidate = (code or "").strip().replace(" ", "")
    if not candidate:
        return None
    for offset in range(-DRIFT_STEPS, DRIFT_STEPS + 1):
        step = current + offset
        # Bytes, not str: `compare_digest` raises TypeError on a str holding
        # anything outside ASCII, so "12345é" was a 500 rather than a no (#208).
        if hmac.compare_digest(totp.at(step * STEP_SECONDS).encode(), candidate.encode()):
            return step
    return None


def verify_and_consume(user: User, code: str, *, at: int | None = None) -> bool:
    """Check a code and burn the step it matched.

    ``users.totp_last_counter`` is the replay defence: the counter only moves
    forward, so a code that has been used is refused for the rest of its window
    and forever after. A second sign-in inside the same thirty seconds is
    refused too -- that is the intended cost, and not a bug to be fixed.

    The burn is a **compare-and-set** in the database, not a comparison in
    Python followed by a write. Two requests carrying the same code -- a
    sign-in and a step-up, or two sign-ins -- each loaded the user before
    either wrote, both saw the old counter, and both were told yes: one code,
    two uses, which is the guarantee `stepup.py` is built on. Now the row
    moves only ``WHERE totp_last_counter IS NULL OR totp_last_counter < step``,
    and exactly one of them gets a row count of one.

    It runs on its own connection and commits at once, like
    `sessions.claim_pending`: a spent code stays spent even if the rest of the
    request fails, which is the safe direction, and the request is not left
    holding a SQLite write lock that its own `ratelimit.record` would then wait
    on. The caller must therefore not have flushed a write on this session yet.

    ``users`` is audited, and this is a Core statement the flush hook never
    sees. That is the same outcome the ORM path had, not a gap in it:
    ``totp_last_counter`` is in ``User.__audit_redact__``, so an ORM write of it
    alone produced no change row and demanded no batch either. The in-memory
    object is then told the new value as *committed* state, so a later flush
    of this user does not write it a second time.

    A user that is not in a session (only unit tests build one) has no row to
    race on and takes the in-memory path.
    """
    if user.totp_secret is None:
        # Cleared by an account reset (#284): there is no code that is right.
        return False
    secret = crypto.open_totp_secret(user.totp_secret, user_id=user.id)
    step = code_matches(secret, code, at=at)
    if step is None:
        return False
    if user.totp_last_counter is not None and step <= user.totp_last_counter:
        return False

    state = inspect(user)
    if state.session is None or not state.persistent:
        user.totp_last_counter = step
        return True

    table = User.__table__
    with state.session.get_bind().begin() as own:
        won = own.execute(  # audit-exempt: only totp_last_counter, which users redacts
            update(table)
            .where(
                table.c.id == user.id,
                or_(table.c.totp_last_counter.is_(None), table.c.totp_last_counter < step),
            )
            .values(totp_last_counter=step)
        ).rowcount
    if won != 1:
        return False
    set_committed_value(user, "totp_last_counter", step)
    return True
