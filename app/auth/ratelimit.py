"""Slowing down guessing, without becoming the denial of service.

Backoff is a **lockout window**, never a sleep. Implemented as a delay it would
tie up a worker per attacker request, which is the attack the limit exists to
prevent. The caller gets a 429 and a Retry-After and decides for itself.

Attempts are recorded on their own short-lived session, because a failed sign-in
raises -- and the record has to survive the rollback that raise causes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..errors import TooManyAttempts
from ..models import LoginAttempt, utcnow

WINDOW = timedelta(minutes=15)
FREE_ATTEMPTS = 5
#: The per-address ceiling is deliberately much higher than the per-account one.
#: It is there to make a spray across many accounts expensive, not to let five
#: requests decide who may sign in.
FREE_ATTEMPTS_PER_IP = 50
#: What a browser holding a live trusted-device record for the account may
#: fail before it too is stopped. It has already proved the second factor, so
#: it is not the guesser the three arms are for, and letting a stranger's five
#: wrong passwords lock the owner's own laptop out was the denial of service
#: (#203). But a stolen device cookie must not become an unlimited password
#: oracle either, so it gets a ceiling of its own -- above anything the
#: ordinary arms let a stranger write (a refused reservation is deleted, so they
#: cannot push the count past `FREE_ATTEMPTS`) and far below a useful guess rate.
FREE_ATTEMPTS_TRUSTED = 20


def _window_start() -> datetime:
    return utcnow() - WINDOW


def _failures(session: Session, *filters, kind: str) -> tuple[int, datetime | None]:
    """How many failures inside the window, and when the oldest of them was."""
    row = session.execute(
        select(func.count(), func.min(LoginAttempt.at)).select_from(LoginAttempt).where(
            *filters,
            LoginAttempt.kind == kind,
            LoginAttempt.ok.is_(False),
            LoginAttempt.at >= _window_start(),
        )
    ).one()
    return int(row[0] or 0), row[1]


def _retry_after(oldest: datetime | None) -> int:
    """When the lockout actually lifts.

    The old formula was `2 ** (failures - FREE_ATTEMPTS + 1)`, computed from a
    count that a refused attempt never increments -- so it was pinned at 5 and
    always said "2 seconds" for what is really a fifteen-minute window. A client
    that honours Retry-After then retries every two seconds for a quarter of an
    hour, which is the request amplification this module exists to avoid.
    """
    if oldest is None:
        return int(WINDOW.total_seconds())
    lifts_at = oldest + WINDOW
    return max(1, int((lifts_at - utcnow()).total_seconds()) + 1)


def check(session: Session, *, email_canonical: str, ip: str | None, kind: str = "password") -> None:
    """Raise if this account, or this address, has been guessing.

    Two axes, and they are not symmetric.

    Per **account**: five failures locks that account for the window. This is
    the real anti-guessing control.

    Per **(address, account)** and per **address**: both exist to make a spray
    across many accounts costly. The pair is what the second arm is keyed on --
    keyed on the address alone at a threshold of five, an unauthenticated
    stranger could type five invented addresses and lock every real account on
    the instance out of their own ledger, renewably, forever. Worse behind a
    reverse proxy: `tailscale serve` hands every request the same loopback peer,
    so one person's typos would lock out the household. The bare-address arm
    survives at a much higher ceiling, where it still bounds a sweep but cannot
    be reached by anyone acting in good faith.
    """
    _refuse_if_over(session, email_canonical=email_canonical, ip=ip, kind=kind, already=0)


def _refuse_if_over(
    session: Session,
    *,
    email_canonical: str,
    ip: str | None,
    kind: str,
    already: int,
    trusted: bool = False,
) -> None:
    """The three axes of `check`. ``already`` is how many of the counted
    failures are the caller's own reservation, and so are not yet evidence.

    ``trusted`` is a browser that holds a live trusted-device record for this
    account: only the account arm applies to it, at `FREE_ATTEMPTS_TRUSTED`.
    """
    account_failures, account_oldest = _failures(
        session, LoginAttempt.email_canonical == email_canonical, kind=kind
    )
    if trusted:
        if account_failures >= FREE_ATTEMPTS_TRUSTED + already:
            wait = _retry_after(account_oldest)
            raise TooManyAttempts(
                "too many attempts for this account. "
                f"Try again in {wait} seconds.",
                retry_after=wait,
                code="auth.too_many_for_account",
                params={"seconds": wait},
            )
        return

    if account_failures >= FREE_ATTEMPTS + already:
        wait = _retry_after(account_oldest)
        raise TooManyAttempts(
            "too many attempts for this account. "
            f"Try again in {wait} seconds.",
            retry_after=wait,
            code="auth.too_many_for_account",
            params={"seconds": wait},
        )

    if ip is None:
        return

    pair_failures, pair_oldest = _failures(
        session,
        LoginAttempt.ip == ip,
        LoginAttempt.email_canonical == email_canonical,
        kind=kind,
    )
    if pair_failures >= FREE_ATTEMPTS + already:
        wait = _retry_after(pair_oldest)
        raise TooManyAttempts(
            f"too many attempts. Try again in {wait} seconds.",
            retry_after=wait,
            code="auth.too_many_attempts",
            params={"seconds": wait},
        )

    ip_failures, ip_oldest = _failures(session, LoginAttempt.ip == ip, kind=kind)
    if ip_failures >= FREE_ATTEMPTS_PER_IP + already:
        wait = _retry_after(ip_oldest)
        raise TooManyAttempts(
            f"too many attempts from here. Try again in {wait} seconds.",
            retry_after=wait,
            code="auth.too_many_from_here",
            params={"seconds": wait},
        )


def record(engine: Engine, *, email_canonical: str, ip: str | None, ok: bool, kind: str = "password") -> None:
    """Write the attempt on its own transaction, so it outlives the failure."""
    with Session(engine, expire_on_commit=False) as own:
        own.add(
            LoginAttempt(email_canonical=email_canonical, ip=ip, ok=ok, kind=kind, at=utcnow())
        )
        own.commit()


def reserve(
    engine: Engine,
    *,
    email_canonical: str,
    ip: str | None,
    kind: str = "password",
    trusted: bool = False,
) -> str:
    """Count this attempt as a failure *before* it is judged. Returns its id.

    `check` then `record` is a read followed, a whole password hash later, by a
    write -- and every request that arrives inside that gap reads the same
    count. Seven concurrent guesses against a budget of five were all
    evaluated. So the attempt is written first, on its own committed
    transaction, and only then are the failures counted, this one included.
    Whichever order the inserts land in, the k-th one committed sees at least k
    rows, so no more than the budget can ever get past the count. Two racing
    at the boundary can both be refused; that is the safe direction.

    A refused reservation is deleted again, like a refusal from `check`, so a
    locked-out caller hammering the door does not keep the lock renewing.

    ``trusted`` is passed by a caller that has seen a live trusted-device
    record for the account on this browser: the attempt is written and counted
    all the same, so a failure from it still counts against a guesser elsewhere,
    but it is judged only against `FREE_ATTEMPTS_TRUSTED`.

    The caller hands the id to `release` when the attempt turns out to be
    right. On a wrong answer it does nothing: the failure is already recorded,
    and it survives the rollback the refusal causes because it was committed on
    its own session.
    """
    with Session(engine, expire_on_commit=False) as own:
        held = LoginAttempt(email_canonical=email_canonical, ip=ip, ok=False, kind=kind, at=utcnow())
        own.add(held)
        own.commit()
        try:
            _refuse_if_over(
                own,
                email_canonical=email_canonical,
                ip=ip,
                kind=kind,
                already=1,
                trusted=trusted,
            )
        except TooManyAttempts:
            own.delete(held)
            own.commit()
            raise
        return held.id


def release(engine: Engine, attempt_id: str) -> None:
    """The reserved attempt was right after all: it is not a failure."""
    with Session(engine) as own:
        held = own.get(LoginAttempt, attempt_id)
        if held is not None:
            own.delete(held)
            own.commit()


def clear_for(session: Session, email_canonical: str) -> None:
    """A successful sign-in wipes the slate for that email."""
    for row in session.execute(
        select(LoginAttempt).where(
            LoginAttempt.email_canonical == email_canonical, LoginAttempt.ok.is_(False)
        )
    ).scalars():
        session.delete(row)


#: How long a recorded attempt is worth keeping. Long enough to answer "was
#: somebody trying?", short enough that the table does not grow without end.
#: `Access Control Investigation.md` states 30 days; nothing was enforcing it.
RETENTION = timedelta(days=30)


def prune(engine: Engine) -> int:
    """Drop attempts older than the retention window. Returns how many went.

    Its own session and its own transaction, like `record`, and deliberately a
    bulk delete: `login_attempts` is not an audited table -- it records attempts
    at the door rather than changes to the ledger -- so there is no batch to
    open and nothing for the hook to log.
    """
    with Session(engine) as own:
        # login_attempts records attempts at the door, not changes to the
        # ledger, so there is no batch to open and nothing for the hook to miss.
        stale = delete(LoginAttempt).where(LoginAttempt.at < utcnow() - RETENTION)
        done = own.execute(stale)  # audit-exempt: login_attempts is not audited
        own.commit()
        return done.rowcount or 0
