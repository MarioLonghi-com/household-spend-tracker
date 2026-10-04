"""Does this `secret.key` open the authenticators in this ledger?

The question behind the one failure in this app that no log announces: a
ledger beside the wrong key -- restored without its own, or beside a key
`app/config.py` created afresh because the old one was missing -- keeps every
row readable and refuses every authenticator. Nothing says so until somebody
tries to sign in, which on a single-household instance can be days.

Read straight from the file, read-only, so `scripts.doctor` can ask it of a
running instance and the boot can ask it before anything else has opened a
session. The secrets are opened and dropped; none is returned or logged.

**Recovery mode** (#287) is the same question asked by the running app, of
one member at a time (`locked_by_key`) or of all of them (`in_session`). It is
computed on every ask and never stored: there is no flag to set, so there is
none to forget to clear. A member is locked by the key when they have an
authenticator enrolled and this key cannot open it. Nothing in recovery mode
writes the sealed secret, so putting the original key back ends it for
everybody who has not re-enrolled in the meantime. A member whose secret is
NULL was cleared by a reset (#284): a different state with its own refusal,
and not recovery mode.

**A disabled member is not counted**, by `check` or `in_session`. They cannot
sign in, so they will never be asked for the recovery code the boot log and
the banner say each counted member will be, and could never re-enrol to
leave the count: one former member kept the banner up for good. Re-enabled,
they count again, and are asked for a recovery code like anybody else. The
disabling keeps their sealed secret, so `locked_by_key` still answers for
them one at a time.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import User
from . import crypto


@dataclass(frozen=True, slots=True)
class KeyCheck:
    #: Members who can sign in -- not disabled -- with an authenticator enrolled.
    enrolled: int
    #: The sign-in emails of those whose secret this key does not open.
    refused: tuple[str, ...]

    @property
    def opened(self) -> int:
        return self.enrolled - len(self.refused)


def check(database: Path, key_text: str) -> KeyCheck:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        try:
            rows = conn.execute(
                "SELECT id, email, totp_secret FROM users"
                " WHERE totp_secret IS NOT NULL AND disabled_at IS NULL"
                " ORDER BY created_at"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    refused = tuple(
        email
        for user_id, email, sealed in rows
        if not crypto.key_opens_totp_secret(key_text, bytes(sealed), user_id=user_id)
    )
    return KeyCheck(enrolled=len(rows), refused=refused)


def locked_by_key(user: User) -> bool:
    """This member has an authenticator, and the running key cannot open it.

    Sign-in asks it at the code step and at the trusted-browser check, and
    re-enrolment asks it before a recovery sign-in's grant may stand in for
    the authenticator (`services/profile.py`).
    """
    return user.totp_secret is not None and not crypto.opens_totp_secret(
        user.totp_secret, user_id=user.id
    )


def key_from_environment() -> bool:
    """The key `crypto` seals with came from `SPENDTRACKER_SECRET_KEY`.

    Then `secret.key` is never read, so the owners' banner names the variable:
    blaming the file sent an operator to copy the right key into it, and
    recovery mode carried on.
    """
    return crypto.settings.secret_key_from_env


def in_session(session: Session) -> KeyCheck:
    """`check`, asked by the running app of its own ledger with its own key.

    Through the request's session rather than the file, so it sees what the
    request sees and asks the key `crypto` seals with. What the owners'
    banner counts.
    """
    rows = session.execute(
        select(User.id, User.email, User.totp_secret)
        .where(User.totp_secret.is_not(None), User.disabled_at.is_(None))
        .order_by(User.created_at)
    ).all()
    refused = tuple(
        email
        for user_id, email, sealed in rows
        if not crypto.opens_totp_secret(sealed, user_id=user_id)
    )
    return KeyCheck(enrolled=len(rows), refused=refused)
