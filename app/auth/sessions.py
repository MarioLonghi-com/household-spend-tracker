"""Server-side sessions.

Not JWTs: a session has to be revocable, and that is the one thing a signed
token cannot do. The cookie carries a random value; the row carries its hash,
who it belongs to, and when it stops working.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ..config import settings
from ..models import PendingSignIn, User, WebSession, utcnow
from . import tokens

#: The cookie's name is `cookies.session_name()`: it depends on `Secure`
#: (#209) and on whether the request named a loopback host (#196), and one
#: module decides all of it.


def issue(
    session: Session, user: User, *, ip: str | None = None, user_agent: str | None = None
) -> str:
    """Create a session and return the value to put in the cookie."""
    value, digest = tokens.issue()
    now = utcnow()
    session.add(
        WebSession(
            id_hash=digest,
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=settings.session_absolute_seconds),
            ip=ip,
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    return value


def lookup(session: Session, value: str | None, *, now: datetime | None = None) -> WebSession | None:
    """The row behind a cookie, if it is still good.

    Two clocks have to agree: an absolute expiry fixed at sign-in, and an idle
    window measured from the last time we saw this browser.
    """
    if not value:
        return None
    now = now or utcnow()
    row = session.execute(
        select(WebSession).where(WebSession.id_hash == tokens.fingerprint(value))
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at <= now:
        return None
    if row.last_seen_at + timedelta(seconds=settings.session_idle_seconds) <= now:
        return None
    return row


def touch(session: Session, row: WebSession, *, now: datetime | None = None) -> None:
    """Record that this session is still in use, sparingly.

    Writing on every request turns every page load into a WAL write and an
    fsync. A minute's granularity is plenty for a fourteen-day idle window and
    for the "someone else is here" indicator, and removes almost all of them.

    **On its own transaction, committed before the request goes on** (#273).
    This runs inside `current_user`, before the route. Written on the
    request's session, the UPDATE held SQLite's write lock until the request
    ended -- and the routes that then write on a connection of their own
    (spending a step-up grant, reserving a rate-limit attempt, consuming an
    authenticator code) waited for that lock, from the same thread that held
    it, until `busy_timeout` ran out: a 409 "ledger busy" for an owner's
    update, after a minute spent reading its confirmation. The row in hand is
    given the new time as already committed, so the request's own session has
    nothing left to flush for it.
    """
    now = now or utcnow()
    if (now - row.last_seen_at).total_seconds() < settings.session_touch_seconds:
        return
    statement = (  # audit-exempt: sessions are not audited
        update(WebSession.__table__)
        .where(WebSession.__table__.c.id_hash == row.id_hash)
        .values(last_seen_at=now)
    )
    bind = session.get_bind()
    if isinstance(bind, Engine):
        with bind.begin() as own:
            own.execute(statement)
    else:  # pragma: no cover - a session bound to one connection has no other
        session.execute(statement)
    set_committed_value(row, "last_seen_at", now)


def revoke(session: Session, value: str) -> None:
    session.execute(  # audit-exempt: sessions are not audited
        delete(WebSession.__table__).where(
            WebSession.__table__.c.id_hash == tokens.fingerprint(value)
        )
    )


def revoke_all_for(session: Session, user_id: str, *, except_hash: str | None = None) -> int:
    """"Sign out everywhere". Also what a password change does."""
    table = WebSession.__table__
    stmt = delete(table).where(table.c.user_id == user_id)  # audit-exempt: sessions
    if except_hash:
        stmt = stmt.where(table.c.id_hash != except_hash)
    return session.execute(stmt).rowcount or 0


def online_users(session: Session, *, now: datetime | None = None) -> set[str]:
    """Who has been seen recently enough to count as here."""
    now = now or utcnow()
    cutoff = now - timedelta(seconds=settings.presence_window_seconds)
    return set(
        session.execute(
            select(WebSession.user_id)
            .where(WebSession.last_seen_at >= cutoff, WebSession.expires_at > now)
            .distinct()
        ).scalars()
    )


# --------------------------------------------------------------------------- #
# The half-finished sign-in
# --------------------------------------------------------------------------- #


def issue_pending(session: Session, user: User, *, seconds: int) -> str:
    """Record a sign-in that has passed the password and still owes a factor.

    Returns the cookie value; only its hash is stored, like every other token
    here. Any earlier pending sign-in for this user is dropped, so an abandoned
    half-attempt cannot be picked up later.
    """
    value, id_hash = tokens.issue()
    for old in session.execute(
        select(PendingSignIn).where(PendingSignIn.user_id == user.id)
    ).scalars():
        session.delete(old)
    session.add(
        PendingSignIn(
            id_hash=id_hash,
            user_id=user.id,
            expires_at=utcnow() + timedelta(seconds=seconds),
        )
    )
    session.flush()
    return value


def drop_pending_for(session: Session, user_id: str) -> int:
    """Every half-finished sign-in this user has. What a password change does.

    A pending sign-in is a password that was right *then*. After the password
    changes it is a password that is wrong now, still one code away from a
    session.
    """
    rows = list(
        session.execute(select(PendingSignIn).where(PendingSignIn.user_id == user_id)).scalars()
    )
    for row in rows:
        session.delete(row)
    return len(rows)


def peek_pending(session: Session, value: str | None, *, now: datetime | None = None) -> str | None:
    """Whose live pending sign-in this is, without spending it.

    For the one refusal that must not cost the person their half-finished
    sign-in: a member the server's key cannot open (#287) is told so at the
    code step and sent on to a recovery code, and the recovery step claims the
    same pending sign-in. Nothing is tried on a peek, so the "one attempt per
    cookie" that `claim_pending` guarantees still holds for every attempt that
    is made.
    """
    if not value:
        return None
    return session.execute(
        select(PendingSignIn.user_id).where(
            PendingSignIn.id_hash == tokens.fingerprint(value),
            PendingSignIn.expires_at > (now or utcnow()),
        )
    ).scalar_one_or_none()


def claim_pending(engine: Engine, value: str | None, *, now: datetime | None = None) -> str | None:
    """Spend a pending sign-in and return whose it was.

    Single-use: the row goes whether or not the code that follows is right, so a
    stolen cookie is worth at most one attempt rather than five minutes of them.

    On its own connection and its own transaction, for the same reason
    `ratelimit.record` is: the claim has to be committed before the rest of the
    request runs. Holding it open on the request's session leaves SQLite with a
    write lock that `record` then blocks on -- "database is locked" on every
    wrong code.

    **One statement whose row count is the verdict** (#206), the compare-and-set
    `totp.verify_and_consume` already uses. It used to read the row and then
    delete it; pysqlite begins a transaction only before DML, so two concurrent
    claims both read it, both deleted it (an ORM delete of a row already gone
    is a warning, not an error), and one pending sign-in bought two code
    guesses. On Postgres that is the ordinary READ COMMITTED outcome. Now the
    DELETE ... RETURNING hands the row to exactly one of them.
    """
    if not value:
        return None
    table = PendingSignIn.__table__
    with engine.begin() as own:
        row = own.execute(  # audit-exempt: pending_sign_ins are not audited
            delete(table)
            .where(table.c.id_hash == tokens.fingerprint(value))
            .returning(table.c.user_id, table.c.expires_at)
        ).first()
    if row is None:
        return None
    user_id, expires_at = row
    return None if expires_at <= (now or utcnow()) else user_id

