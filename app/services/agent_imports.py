"""Rows an agent posted, staged through the machinery a CSV already uses.

> [!success] The central decision, and it is a decision not to build something
> **There is no second write path for agent transactions.** They go through
> the import staging that already exists, which brings dedupe by `import_id`,
> twin matching, payee rules, per-line verdicts, one batch, one undo, and a
> review screen a person already knows how to read.

The naive alternative is 300 `POST /transactions` calls. That is 300 batches,
300 History rows hidden by default, and an undo that must be performed 300
times in reverse order because `undo._refuse_if_superseded` only lets the most
recent batch touching a row come back. One import is one row in History and
one undo.

What this module does is turn JSON into :class:`statements.parsing.ParsedRow`,
which is the carrier `stage()` already takes. Everything after that is shared
code, and that is the point.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from statements import ParsedRow

from ..models import AgentReplay, utcnow
from ..money import to_decimal, to_minor

#: Matching `IdList`'s ceiling. A larger set is several imports, which is
#: correct rather than a limitation: they are several operations and should be
#: several undos.
MAX_ROWS = 1000

#: How long a retry is still a retry. A day is generous for a network blip and
#: short enough that the table does not become a log.
REPLAY_WINDOW = timedelta(hours=24)


def canonical_digest(payload: dict) -> str:
    """A stable SHA-256 of a request body.

    `sort_keys` and no whitespace, so two encoders that disagree about key
    order or spacing produce the same digest -- otherwise the idempotency guard
    would depend on which HTTP library the agent happened to use.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def to_parsed_rows(rows: list, *, currency: str) -> list[ParsedRow]:
    """JSON rows as the dataclass `stage()` takes.

    ``raw`` is synthesised as the canonical JSON of the row, because
    `import_lines.raw` is NOT NULL and its docstring says it is what lets an
    import be explained a year later -- which matters *more* when the source
    was an API call nobody kept a copy of.
    """
    parsed: list[ParsedRow] = []
    for index, row in enumerate(rows, start=1):
        # `ParsedRow.amount` is a Decimal in MAJOR units, because that is what a
        # statement file carries and `stage()` converts it with the account's
        # currency exactly as it would a CSV cell. So both spellings are
        # normalised to that here.
        #
        # Through `money` in both directions, never by hand: the standing rule
        # is that `app/money.py` owns every conversion, and an exponent worked
        # out locally is the beginning of the bug that rule exists to prevent.
        minor = (
            row.amount_minor
            if row.amount_minor is not None
            else to_minor(row.amount, currency)
        )
        amount = to_decimal(minor, currency)
        parsed.append(
            ParsedRow(
                line_no=index,
                raw=json.dumps(row.model_dump(mode="json"), sort_keys=True, default=str),
                when=row.date,
                amount=amount,
                payee=(row.payee or None),
                memo=(row.memo or None),
                # The reuse that matters. `stage()` already prefers `fitid`
                # over its derived key *because the bank promises it is
                # stable*, and a source system's own id has that same property.
                # An agent that supplies one gets exact dedupe across
                # overlapping pulls; one that does not falls back to the
                # approximation a CSV gets. No new dedupe concept.
                fitid=(row.external_id or None),
                details=dict(row.details or {}),
            )
        )
    return parsed


# --------------------------------------------------------------------------- #
# Answering a retry with what it got the first time
# --------------------------------------------------------------------------- #


def replay_of(
    session: Session, *, agent_key_id: str, header: str, route: str, request_sha256: str
) -> AgentReplay | None:
    """The answer this exact request already got, if it got one.

    A body that changed is **not** a retry. Returning the first result for it
    would silently discard what the caller actually asked for, so that case is
    a conflict rather than a replay -- see the caller.
    """
    found = session.execute(
        select(AgentReplay).where(
            AgentReplay.agent_key_id == agent_key_id,
            AgentReplay.idempotency_key == header,
            AgentReplay.route == route,
        )
    ).scalar_one_or_none()
    if found is None or found.at <= utcnow() - REPLAY_WINDOW:
        return None
    return found


def remember(
    session: Session,
    *,
    agent_key_id: str,
    header: str,
    route: str,
    request_sha256: str,
    status: int,
    response: dict,
) -> None:
    """Record what this request produced. Called only when it succeeded."""
    session.add(
        AgentReplay(
            agent_key_id=agent_key_id,
            idempotency_key=header[:200],
            route=route[:120],
            request_sha256=request_sha256,
            status=status,
            response=response,
            at=utcnow(),
        )
    )
    session.flush()


def sweep(session: Session, *, now=None) -> int:
    """Drop replays past the window. A bulk delete: not an audited table."""
    stale = delete(AgentReplay).where(  # audit-exempt: agent_replays is not audited
        AgentReplay.at < (now or utcnow()) - REPLAY_WINDOW
    )
    return session.execute(stale).rowcount or 0
