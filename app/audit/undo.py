"""Reversing a batch.

Undo replays a batch's changes in reverse ``seq``: an insert becomes a delete, an
update is restored from its before-image, a delete is re-inserted with its
original id. It is recorded as its own batch of kind ``undo``, which falls out
for free -- the replay runs through loaded ORM objects, so the hook logs it with
no special casing, and undoing an undo is therefore a redo.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime, LargeBinary, and_, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.interfaces import ONETOMANY

from ..errors import Conflict, Forbidden, NotFound
from ..models import Batch, BatchKind, BatchStatus, Change, EnumStr
from ..models.enums import ChangeOp
from .batch import batch as open_batch
from .registry import audited_models, is_audited, redacted_columns


def _coerce(model: type, key: str, value: Any) -> Any:
    """Turn a JSON snapshot value back into what the column expects."""
    if value is None:
        return None
    column = model.__table__.columns.get(key)
    if column is None:
        return value
    kind = column.type
    if isinstance(value, dict) and "__bytes__" in value:
        return base64.b64decode(value["__bytes__"])
    if isinstance(kind, EnumStr):
        return kind.enum_class(value)
    if isinstance(kind, LargeBinary) and isinstance(value, str):
        return value.encode()
    if isinstance(kind, DateTime) and isinstance(value, str):
        return datetime.fromisoformat(value)
    if isinstance(kind, Date) and isinstance(value, str):
        return date.fromisoformat(value)
    return value


def _refuse_if_superseded(session: Session, batch_id: str) -> None:
    """Only the most recent batch touching a row may be undone.

    Undoing an older one would restore a before-image that a later edit has
    already moved past, silently discarding that edit.
    """
    touched = (
        select(Change.table_name, Change.row_id)
        .where(Change.batch_id == batch_id)
        .distinct()
        .subquery()
    )
    latest = (
        select(func.max(Change.seq).label("seq"))
        .join(
            touched,
            and_(Change.table_name == touched.c.table_name, Change.row_id == touched.c.row_id),
        )
        .group_by(Change.table_name, Change.row_id)
        .subquery()
    )
    candidates = list(
        session.execute(
            select(Change).join(latest, Change.seq == latest.c.seq).where(Change.batch_id != batch_id)
        ).scalars()
    )

    blockers = []
    for change in candidates:
        blocking_batch = session.get(Batch, change.batch_id)
        if blocking_batch is None:
            continue
        # A change that has since been undone is not standing in the way, and
        # neither is the undo that reversed it -- otherwise "undo that batch
        # first" is advice you can follow and still be refused, forever.
        if blocking_batch.status is BatchStatus.undone:
            continue
        if blocking_batch.kind is BatchKind.undo:
            continue
        blockers.append(change)

    if blockers:
        first = blockers[0]
        raise Conflict(
            f"{len(blockers)} row(s) in this batch were changed again afterwards "
            f"(for example {first.table_name} {first.row_id}, by batch {first.batch_id}). "
            "Undo that batch first."
        )


def _refuse_if_dependants_are_newer(session: Session, batch_id: str) -> None:
    """Refuse when undoing would take rows a later batch created.

    ``_refuse_if_superseded`` only looks at rows the batch itself touched, so
    dependants are invisible to it: undoing the batch that created an account
    also deletes every transaction a later batch added to that account, and
    nothing stood in the way. The deletes *were* logged and a redo puts them
    back, so this was destructive-without-warning rather than unrecoverable --
    but "undo only the most recent batch that touched a row" is supposed to mean
    the user is told, not that they find out afterwards.

    Only inserts matter: undoing one deletes the row, and deleting a row is what
    reaches its children.
    """
    models = audited_models()
    inserted = session.execute(
        select(Change.table_name, Change.row_id).where(
            Change.batch_id == batch_id, Change.op == ChangeOp.insert
        )
    ).all()

    # Every cascading child of every row the batch inserted, by table. Asking
    # "which batch made it" once per child was one query per row the batch
    # wrote whenever it had also created their account: 10,425 of them, 13.9 s,
    # to undo a one-time import that made one (#230). Gathered first, it is a
    # grouped read per table and per chunk of ids.
    children: dict[str, set[str]] = {}
    for table, row_id in inserted:
        model = models.get(table)
        if model is None:
            continue
        parent = session.get(model, row_id)
        if parent is None:
            continue
        for relationship in inspect(model).relationships:
            if relationship.direction is not ONETOMANY:
                continue
            # Only the cascading ones. A nullable link is *cleared*, as a logged
            # update that the same replay reverses -- no row is lost, so there
            # is nothing to refuse and refusing would block ordinary undos.
            if "delete" not in relationship.cascade:
                continue
            for child in getattr(parent, relationship.key):
                if child is None or not is_audited(child):
                    continue
                children.setdefault(child.__tablename__, set()).add(child.id)

    blockers: list[tuple[str, str, str]] = []
    for table in sorted(children):
        for owner_id, row_id in _batches_that_made(session, table, children[table]):
            if owner_id != batch_id:
                blockers.append((table, row_id, owner_id))

    if blockers:
        table, row_id, owner_id = blockers[0]
        raise Conflict(
            f"undoing this batch would also delete {len(blockers)} row(s) that a later batch "
            f"created (for example {table} {row_id}, by batch {owner_id}). "
            "Undo that batch first."
        )


def _batches_that_made(
    session: Session, table: str, row_ids: set[str]
) -> list[tuple[str, str]]:
    """(batch id, row id) for each row whose latest insert is by a live batch.

    A row put back by an undo carries two inserts, and the newer one is its
    maker now; a batch since undone, or an undo itself, is not standing in the
    way of anything. Read per chunk of ids on `ix_changes_row_history`, in id
    order so the first blocker named is the same one every time.
    """
    found: list[tuple[str, str]] = []
    ids = sorted(row_ids)
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        latest = (
            select(func.max(Change.seq).label("seq"))
            .where(
                Change.table_name == table,
                Change.row_id.in_(chunk),
                Change.op == ChangeOp.insert,
            )
            .group_by(Change.row_id)
            .subquery()
        )
        found += session.execute(
            select(Batch.id, Change.row_id)
            .join(latest, Change.seq == latest.c.seq)
            .join(Batch, Batch.id == Change.batch_id)
            .where(Batch.status != BatchStatus.undone, Batch.kind != BatchKind.undo)
            .order_by(Change.row_id)
        ).all()
    return found


def _refuse_if_redacted(session: Session, batch_id: str) -> None:
    """A redacted column is not in the snapshot, so it cannot be restored.

    Re-inserting a user would write a null password hash. Refusing is also the
    right answer on its own terms: "undo" should not roll a password back.
    """
    models = audited_models()
    tables = set(
        session.execute(
            select(Change.table_name).where(Change.batch_id == batch_id).distinct()
        ).scalars()
    )
    for table in sorted(tables):
        model = models.get(table)
        if model is None or not redacted_columns(model):
            continue
        # Only a *delete* is unrecoverable: putting the row back would need the
        # columns the log deliberately never captured, and would write nulls
        # into them. An insert is undone by deleting, and an update by
        # restoring the columns that were captured -- neither touches a secret.
        deleted_here = session.execute(
            select(func.count())
            .select_from(Change)
            .where(
                Change.batch_id == batch_id,
                Change.table_name == table,
                Change.op == ChangeOp.delete.value,
            )
        ).scalar_one()
        if deleted_here:
            raise Conflict(
                f"this batch deleted from {table}, which holds secrets the log deliberately "
                "does not capture, so those rows cannot be put back."
            )


#: Tables whose rows only an owner may change. Undo replays the original act,
#: so undoing a batch that touched one of these *is* that act -- and the
#: authorisation has to be re-run here, or a member evicts the owner by
#: reversing the batch that admitted them.
OWNER_ONLY_TABLES = frozenset({"household_members"})


def _refuse_if_owner_only(session: Session, batch_id: str, *, actor_id: str) -> None:
    """Re-run the authorisation the original act required.

    ``add_member``/``remove_member`` are owner-only in the service. Undo reached
    the same rows through a route that asked only for membership, which made the
    owner-only rule reachable by anyone in the household.

    A one-time import is owner-only at its doors (`routers/one_time_import.py`)
    and is one batch, so undoing it takes a household's whole migrated history
    back out -- the same act at the same scale, and so the same owner (#216).
    It is recognised by the mark it leaves on `Batch.source`, not by the tables
    it touched, which are every ordinary import's.
    """
    from ..models import Role, User
    from ..services.one_time_import.engine import MARK

    touched = set(
        session.execute(
            select(Change.table_name).where(Change.batch_id == batch_id).distinct()
        ).scalars()
    )
    one_time = _is_one_time_import(session, batch_id, MARK)
    if not touched & OWNER_ONLY_TABLES and not one_time:
        return
    actor = session.get(User, actor_id)
    if actor is None or actor.role is not Role.owner:
        if one_time:
            raise Forbidden("only the owner can undo a one-time import")
        raise Forbidden("only the owner can undo a change to who is in a household")


def _is_one_time_import(session: Session, batch_id: str, mark: str) -> bool:
    """The batch is a one-time import, or an undo that reaches back to one.

    Undoing the undo of an import is that import again -- redo is the same
    act -- so the owner rule follows the `undoes` chain back to where it
    started.
    """
    seen: set[str] = set()
    current: str | None = batch_id
    while current and current not in seen:
        seen.add(current)
        row = session.get(Batch, current)
        if row is None:
            return False
        source = row.source or {}
        if mark in source:
            return True
        current = source.get("undoes") if row.kind is BatchKind.undo else None
    return False


#: Tables whose rows are credentials. Undo never touches them, in either
#: direction: undoing a revocation would bring a revoked key or invitation back
#: to life (`revoked_at` is an ordinary captured column, so the before-image
#: holds NULL), and undoing an issue is a revocation by another name, which has
#: its own route and its own rules about who may do it. The undo route asks
#: only for household membership, so without this any member could resurrect
#: somebody else's revoked key.
CREDENTIAL_TABLES = frozenset({"agent_keys", "invitations"})


def _refuse_if_credentials(session: Session, batch_id: str) -> None:
    """A batch that touched a credential is not undoable. Revoke or reissue."""
    touched = set(
        session.execute(
            select(Change.table_name).where(Change.batch_id == batch_id).distinct()
        ).scalars()
    )
    hit = sorted(touched & CREDENTIAL_TABLES)
    if hit:
        raise Conflict(
            f"this batch changed {', '.join(hit)}, and undo never restores or removes a "
            "credential. Revoke it, or issue a new one, instead."
        )


def _refuse_if_receipt_bytes_are_gone(session: Session, batch_id: str) -> None:
    """Refuse to put back a receipt whose bytes the sweep has already taken.

    `receipt_blobs` is out of the audit log, so an undo re-inserts a receipt
    row and knows nothing about bytes. `receipts.sweep_orphan_blobs` keeps an
    orphaned blob for `ORPHAN_GRACE` so an undo inside that window finds it;
    after it, the replay would restore a receipt pointing at nothing -- which
    is worse than not undoing, because it looks like it worked.
    """
    from ..models import ReceiptBlob

    deleted = session.execute(
        select(Change).where(
            Change.batch_id == batch_id,
            Change.table_name == "receipts",
            Change.op == ChangeOp.delete,
        )
    ).scalars()
    for change in deleted:
        sha = (change.before or {}).get("blob_sha256")
        present = session.execute(
            select(ReceiptBlob.sha256).where(ReceiptBlob.sha256 == sha).limit(1)
        ).scalar_one_or_none()
        if present is None:
            raise Conflict(
                "this batch removed a receipt whose picture has since been cleared out, "
                "so it cannot be put back."
            )


def undo_batch(session: Session, batch_id: str, *, actor_id: str) -> Batch:
    target = session.get(Batch, batch_id)
    if target is None:
        raise NotFound(f"no batch {batch_id}")
    if target.status is not BatchStatus.applied:
        raise Conflict(f"that batch is {target.status.value}, and only an applied batch can be undone")

    # Held for the whole of this function: the guards below and the replay
    # all reach these rows, and without a strong reference each would read
    # them again (issue #103).
    held = _hold_touched_rows(session, batch_id)

    _refuse_if_owner_only(session, batch_id, actor_id=actor_id)
    _refuse_if_superseded(session, batch_id)
    _refuse_if_dependants_are_newer(session, batch_id)
    _refuse_if_redacted(session, batch_id)
    _refuse_if_credentials(session, batch_id)
    _refuse_if_receipt_bytes_are_gone(session, batch_id)

    models = audited_models()

    # Reverse seq unwinds the common case correctly, because the forward order
    # came from a dependency-sorted flush. Deferring the foreign keys is what
    # makes it safe when a batch spanned several flushes: enforcement moves to
    # COMMIT, where every row is in its final state. A genuinely dangling
    # reference is still refused. It is also what lets the replay itself be
    # several flushes, below.
    with open_batch(
        session,
        kind=BatchKind.undo,
        actor_id=actor_id,
        household_id=target.household_id,
        source={"undoes": batch_id},
    ) as undo:
        # Rows this replay has already put back. A batch that deleted a row it
        # had also updated -- which is exactly the shape of deleting a transfer
        # -- replays the delete first and the update second, and session.get()
        # cannot see a pending insert.
        restored: dict[tuple[str, str], Any] = {}

        # A chunk of the log at a time, each flushed before the next is read,
        # all inside the one transaction. Reading the whole batch first held
        # every change row with both its images, every row it touched, and
        # every new change row the hook wrote, at once: 214 MiB to undo a
        # 10.4k-row import (#234).
        for rows in _changes_newest_first(session, batch_id):
            gone: list[tuple[str, str]] = []
            for change in rows:
                model = models.get(change.table_name)
                if model is None:  # pragma: no cover - a table left the audit set
                    raise Conflict(
                        f"{change.table_name} is no longer audited; this batch cannot be undone"
                    )

                key = (change.table_name, change.row_id)
                if change.op is ChangeOp.insert:
                    obj = restored.pop(key, None) or session.get(model, change.row_id)
                    if obj is not None:
                        _forget_children_already_deleted(obj)
                        session.delete(obj)
                        gone.append(key)
                elif change.op is ChangeOp.update:
                    obj = restored.get(key) or session.get(model, change.row_id)
                    if obj is None:
                        raise Conflict(
                            f"{change.table_name} {change.row_id} no longer exists, so the "
                            "update recorded in this batch cannot be put back"
                        )
                    for column, value in (change.before or {}).items():
                        setattr(obj, column, _coerce(model, column, value))
                else:
                    obj = model(
                        **{k: _coerce(model, k, v) for k, v in (change.before or {}).items()}
                    )
                    session.add(obj)
                    restored[key] = obj

            # Before every flush, not once: SQLite clears this pragma at every
            # COMMIT, and that includes the implicit one that ends a read run
            # outside a transaction -- which is what reading the first chunk
            # is, since opening the batch committed its own row. Set before
            # that read, it was off by the time the replay wrote anything.
            session.execute(text("PRAGMA defer_foreign_keys=ON"))
            try:
                session.flush()
            except IntegrityError as exc:
                raise Conflict(
                    "undoing this batch collides with a row that exists now "
                    f"({exc.orig}). Something has taken the slot it used to occupy."
                ) from exc
            # A deleted row is not read again: an insert is the first change a
            # batch logs for a row, so replaying backwards it is the last.
            for key in gone:
                held.pop(key, None)
            del rows

        target.status = BatchStatus.undone
        target.undone_by_id = undo.id
        session.add(target)

    del held
    return undo


#: Ids per `IN (...)`, well under SQLite's bound-parameter limit.
_IN_CHUNK = 500

def _forget_children_already_deleted(obj: Any) -> None:
    """Take rows an earlier flush of this replay deleted out of obj's collections.

    The replay flushes a chunk at a time, and a collection loaded before a
    flush still lists the children that flush deleted. Deleting the parent
    afterwards cascades over that list -- a second DELETE of each child, and a
    second change row logging it, which a redo then tries to insert twice.
    Set as the committed value, so nothing is recorded as a change and nothing
    is read.
    """
    state = inspect(obj)
    for relationship in state.mapper.relationships:
        if relationship.direction is not ONETOMANY or relationship.key not in state.dict:
            continue
        loaded = state.dict[relationship.key]
        live = [child for child in loaded if not inspect(child).was_deleted]
        if len(live) != len(loaded):
            set_committed_value(obj, relationship.key, live)


#: Change rows the replay reads, and flushes, at a time.
_REPLAY_CHUNK = 2000


def _changes_newest_first(session: Session, batch_id: str) -> Iterator[list[Change]]:
    """The batch's change rows in reverse `seq`, `_REPLAY_CHUNK` at a time.

    Each chunk is its own read, below the last `seq` of the one before, rather
    than one cursor held open across the replay's flushes: the flushes write
    to `changes` too, and a chunk in hand is a list nobody else is moving.
    """
    below: int | None = None
    while True:
        stmt = select(Change).where(Change.batch_id == batch_id)
        if below is not None:
            stmt = stmt.where(Change.seq < below)
        rows = list(
            session.execute(stmt.order_by(Change.seq.desc()).limit(_REPLAY_CHUNK)).scalars()
        )
        if not rows:
            return
        below = rows[-1].seq
        yield rows
        if len(rows) < _REPLAY_CHUNK:
            return


def _hold_touched_rows(session: Session, batch_id: str) -> dict[tuple[str, str], Any]:
    """Every row the batch inserted or updated, read in a query or two per table.

    The session's identity map holds rows weakly. Undoing a 300-row import
    read each transaction twice -- once to check for newer dependants, once to
    delete it -- because nothing held it in between, and then read each one's
    receipts, and each payee's rules and rows, one lazy load at a time: 1,811
    statements. Loaded here and returned, the dict keeps them alive, so every
    `session.get` in the guards and the replay is answered from the identity
    map, and the collections the delete walks are already there.

    Only an inserted row gets its one-to-many collections loaded, because only
    an inserted row is deleted by the replay -- and deleting walks every one of
    them anyway (the dependants check, the cascade, the hook's nulling of
    links). An updated row is restored in place; loading an account's every
    transaction to rename it back would be the opposite of the point.

    Nothing is decided here. A row missing now is simply not in the dict, and
    the replay's own `session.get` finds it missing exactly as it did before.
    """
    models = audited_models()
    touched = session.execute(
        select(Change.table_name, Change.row_id, Change.op)
        .where(Change.batch_id == batch_id, Change.op != ChangeOp.delete)
        .distinct()
    ).all()
    inserted: dict[str, set[str]] = {}
    updated: dict[str, set[str]] = {}
    for table, row_id, op in touched:
        (inserted if op is ChangeOp.insert else updated).setdefault(table, set()).add(row_id)

    held: dict[tuple[str, str], Any] = {}
    for wanted, with_children in ((inserted, True), (updated, False)):
        for table, ids in wanted.items():
            model = models.get(table)
            if model is None:
                continue
            options = (
                [
                    selectinload(getattr(model, relationship.key))
                    for relationship in inspect(model).relationships
                    if relationship.direction is ONETOMANY
                ]
                if with_children
                else []
            )
            remaining = sorted(ids - {row_id for t, row_id in held if t == table})
            for start in range(0, len(remaining), _IN_CHUNK):
                chunk = remaining[start : start + _IN_CHUNK]
                for obj in session.execute(
                    select(model).where(model.id.in_(chunk)).options(*options)
                ).scalars():
                    held[(table, obj.id)] = obj
    return held
