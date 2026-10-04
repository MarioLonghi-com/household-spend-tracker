"""What a key asked for, and the guard against a loop that never stops asking.

Two jobs in one table, and they genuinely are one thing: the record of what a
key did *is* the count of what it did recently.

**The log.** The audit log records writes, so an analysis agent that pulled the
whole register left no trace beyond `last_used_at`. For a household ledger on a
tailnet that is arguably fine -- but "what did that key do last Tuesday" is a
question worth being able to answer, and "it read everything" is the answer
worth having.

**The limit.** Not an anti-guessing control: the token is 256 bits and the
front door is a tailnet. It guards against the realistic failure, which is an
agent that misreads a response and retries forever against SQLite, where there
is one writer. A generous ceiling catches a runaway within a minute and never
troubles bulk-shaped work.

Like `auth/ratelimit.py` it is a **lockout window, never a sleep**: a sleep
ties up a worker per caller, which is the denial of service the limit exists to
prevent. The caller gets a 429 and a `Retry-After` and decides for itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..errors import TooManyAttempts
from ..models import AgentRequest, utcnow

#: Per key, per hour. Generous for bulk-shaped work and still tight enough that
#: a tight loop trips it inside a minute. YNAB's 200/hour is recorded in
#: `YNAB — Feature Analysis` as having *"forced you to design around bulk
#: endpoints and deltas"* -- which is a feature, and one this design has
#: already paid for with server-side aggregation and delta reads.
PER_HOUR = 600
WINDOW = timedelta(hours=1)

#: How long a line is worth keeping. The same thirty days `ratelimit.RETENTION`
#: uses, and for the same reason: long enough to answer "what was that key
#: doing?", short enough that the table does not grow without end. It is swept
#: by `auth/housekeeping.py`, because a window with no caller is not a window.
RETENTION = timedelta(days=30)


def _window_start(now: datetime | None = None) -> datetime:
    return (now or utcnow()) - WINDOW


#: A refusal by this limiter, which is logged but never counted. See below.
REFUSED = 429


def used_in_window(session: Session, agent_key_id: str, *, now: datetime | None = None) -> int:
    """How much work this key has had out of the window. Refusals are not work.

    Excluding `REFUSED` is load-bearing, not tidiness. The count is what decides
    the lockout, so a 429 that counted toward it would extend the window that
    produced it: a runaway agent -- the exact thing this limit exists to catch --
    would hammer a door that never reopened, and the window would never drain.

    That is the amplification `auth/ratelimit._retry_after` has its own comment
    about, one subsystem over. It stayed correct here for a different reason
    until refusals started being logged at all: a 429 never reached the handler,
    so it wrote no line to count. Now that `main._key_behind` attributes one, the
    exclusion has to be said out loud.
    """
    return (
        session.execute(
            select(func.count())
            .select_from(AgentRequest)
            .where(
                AgentRequest.agent_key_id == agent_key_id,
                AgentRequest.at >= _window_start(now),
                AgentRequest.status != REFUSED,
            )
        ).scalar_one()
        or 0
    )


def check(session: Session, agent_key_id: str, *, now: datetime | None = None) -> None:
    """Raise if this key has been going too hard. Read-only; records nothing.

    Called from `current_agent`, so the refusal happens before the handler --
    the point of a limit is not doing the work.
    """
    now = now or utcnow()
    used = used_in_window(session, agent_key_id, now=now)
    if used < PER_HOUR:
        return

    # When the oldest request in the window falls out of it, there is room
    # again. Telling the caller exactly that is what lets a well-behaved agent
    # pace itself instead of polling a closed door.
    # The same population `used_in_window` counted, or the answer is a time at
    # which there is still no room: a refusal logged a moment ago is not what
    # the caller is waiting to fall out of the window.
    oldest = session.execute(
        select(func.min(AgentRequest.at)).where(
            AgentRequest.agent_key_id == agent_key_id,
            AgentRequest.at >= _window_start(now),
            AgentRequest.status != REFUSED,
        )
    ).scalar_one_or_none()
    retry_after = max(1, int(((oldest + WINDOW) - now).total_seconds())) if oldest else 60
    raise TooManyAttempts(
        f"that key has made {PER_HOUR} requests in the last hour, which is its limit. "
        "If this is a loop, stop it; if it is real work, use the bulk and summary "
        "endpoints rather than one request per row.",
        retry_after=retry_after,
    )


def record(
    engine: Engine,
    *,
    agent_key_id: str | None,
    method: str,
    route: str,
    status: int,
    rows: int | None = None,
    batch_id: str | None = None,
) -> None:
    """Write the line on its own transaction, so it outlives the request.

    Exactly why `ratelimit.record` does the same: a request that raised has had
    its session rolled back, and the record of it must not be rolled back with
    it -- a limit you can escape by failing is not a limit.
    """
    with Session(engine, expire_on_commit=False) as own:
        own.add(
            AgentRequest(
                agent_key_id=agent_key_id,
                method=method[:8],
                route=route[:120],
                status=status,
                rows=rows,
                batch_id=batch_id,
                at=utcnow(),
            )
        )
        own.commit()


def sweep(session: Session, *, now: datetime | None = None) -> int:
    """Drop lines past the retention window. Returns how many went.

    A bulk delete, which is allowed: `agent_requests` is `__audit__ = False`,
    so there is no batch to open and nothing for the hook to miss.
    """
    stale = delete(AgentRequest).where(  # audit-exempt: agent_requests is not audited
        AgentRequest.at < (now or utcnow()) - RETENTION
    )
    return session.execute(stale).rowcount or 0
