"""The batch context manager.

A batch is one operation: an import, a bulk re-categorisation, a single edit in
the register. It is the level people ask questions at, so it is the level undo
works at.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from ..errors import AuditError, Conflict
from ..models import Batch, utcnow
from ..models.enums import BatchKind, BatchStatus
from .hook import BATCH_KEY, STACK_KEY


@contextmanager
def batch(
    session: Session,
    *,
    kind: BatchKind,
    actor_id: str,
    household_id: str | None = None,
    source: dict | None = None,
    own_transaction: bool = True,
) -> Iterator[Batch]:
    """Open a batch for the duration of one operation.

    ``own_transaction`` commits the batch row on entry, before any work, which
    is the only way a *failed* batch can leave a trace: if the row were written
    in the operation's own transaction, the rollback would take it with it and
    the failure would be invisible. Pass ``False`` only when the operation must
    genuinely be a single transaction and leaving no trace on failure is the
    intent -- the setup wizard is the one such case.
    """
    stack: list[Batch] = session.info.setdefault(STACK_KEY, [])
    if stack:
        # Nesting joins the outer batch rather than opening another. An import
        # calling a service that opens its own batch must stay one operation.
        yield stack[-1]
        return

    row = Batch(
        kind=kind,
        actor_id=actor_id,
        household_id=household_id,
        source=source,
        status=BatchStatus.running,
    )
    session.add(row)
    # The batch row has to exist before any change can reference it. This flush
    # does not recurse into "no batch open" because `batches` is not audited.
    session.flush()
    if own_transaction:
        session.commit()

    stack.append(row)
    session.info[BATCH_KEY] = row
    try:
        yield row
    except BaseException:
        session.rollback()
        if own_transaction:
            # Same connection deliberately: SQLite has one writer, so recording
            # the failure on a second connection would deadlock against it.
            row.status = BatchStatus.failed
            row.finished_at = utcnow()
            session.add(row)
            session.commit()
        raise
    else:
        # Flush inside the block, while the batch is still the open one. Without
        # this an own_transaction=False caller commits after the context has
        # closed, and its pending rows arrive at the hook with no batch open.
        session.flush()
        if row.status is BatchStatus.running:
            # Only if nobody inside chose otherwise. A staged import ends in
            # `preview`: it is a complete, recorded operation that has
            # deliberately not been applied yet.
            row.status = BatchStatus.applied
        row.finished_at = utcnow()
        if own_transaction:
            session.commit()
    finally:
        stack.pop()
        session.info.pop(BATCH_KEY, None)


@contextmanager
def resume(
    session: Session,
    row: Batch,
    *,
    status: BatchStatus | None = None,
    committed_by: str | None = None,
    agent: dict | None = None,
) -> Iterator[Batch]:
    """Re-open an existing batch and attribute further changes to it.

    An import is one operation that happens in two visits: it is staged, looked
    at, and then applied. Opening a second batch to apply it would split one act
    across two rows -- the staged one holding the per-line verdicts, the other
    holding the changes -- and "undo this import" would then have two entries to
    choose between, one of which does nothing.

    The two visits need not be made by the same hand. ``actor_id`` and
    ``agent_key_id`` stay whoever staged it; who applied it -- ``committed_by``,
    and ``agent`` when a key did (``{"key_id", "label", "name"}``, the same copy
    `AgentContext.batch` keeps) -- goes into ``source["committed"]``, so History
    can name them. Recorded when the person differs or a key was used; a person
    applying their own import is already the batch's actor (issue #213).
    """
    stack: list[Batch] = session.info.setdefault(STACK_KEY, [])
    if stack:
        raise AuditError("a batch is already open on this session")
    # Refused here, before the `try`: its except arm marks the row `failed`,
    # and a second commit of an applied import must not be what turns it into
    # one -- a failed batch cannot be undone, and its rows are still in the
    # register (issue #212).
    if row.status is not BatchStatus.preview:
        raise Conflict(
            f"that batch is {row.status.value}, and only a staged one can be resumed"
        )

    stack.append(row)
    session.info[BATCH_KEY] = row
    try:
        yield row
    except BaseException:
        session.rollback()
        row.status = BatchStatus.failed
        row.finished_at = utcnow()
        session.add(row)
        session.commit()
        raise
    else:
        session.flush()
        if status is not None:
            row.status = status
        if (committed_by is not None and committed_by != row.actor_id) or agent is not None:
            # A new dict rather than a key set on the old one: a JSON column
            # does not notice a mutation in place.
            row.source = {
                **(row.source or {}),
                "committed": {
                    "user_id": committed_by,
                    "agent": agent,
                    "at": utcnow().isoformat(),
                },
            }
        row.finished_at = utcnow()
        session.commit()
    finally:
        stack.pop()
        session.info.pop(BATCH_KEY, None)
