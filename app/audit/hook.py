"""The three mapper-level policies, and the flush hook that writes the log.

Installed by importing this module, which ``app.models`` does not do -- the
application and the tests install it explicitly, so a script that genuinely
needs to bypass the audit can choose not to.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Mapper, Session
from sqlalchemy.orm.interfaces import ONETOMANY

from ..errors import CrossHouseholdChange, NoOpenBatch
from ..models import Base, Change, new_id, utcnow
from ..models.enums import ChangeOp
from .registry import is_audited
from .snapshot import images

#: Where ``batch()`` parks the open batch on the session.
BATCH_KEY = "audit_batch"
STACK_KEY = "audit_batch_stack"


@event.listens_for(Base, "init", propagate=True)
def _assign_id_eagerly(target: Any, args: tuple, kwargs: dict) -> None:
    """Give the row its id at construction, not at flush.

    A ``mapped_column(default=new_id)`` default is evaluated by the persistence
    step, which runs *after* ``before_flush``. Without this the hook reads
    ``obj.id`` as ``None`` and writes a change row pointing at nothing -- the
    log looks complete and cannot be replayed.
    """
    if getattr(type(target), "__uuid_pk__", False):
        kwargs.setdefault("id", new_id())


@event.listens_for(Mapper, "after_configured")
def _force_active_history() -> None:
    """Make every audited column fetch its old value when set.

    SQLAlchemy does not load the previous value on assignment by default, so an
    attribute that was never loaded produces ``History(added=[new])`` with no
    old value at all -- an update with an empty before-image, which cannot be
    undone.
    """
    for mapper in Base.registry.mappers:
        if not mapper.class_.__audit__:
            continue
        for attr in mapper.column_attrs:
            getattr(mapper.class_, attr.key).impl.active_history = True


@event.listens_for(Session, "before_flush")
def _write_changes(session: Session, flush_context: Any, instances: Any) -> None:
    """Record every insert, update and delete on an audited table."""
    batch = session.info.get(BATCH_KEY)
    at = utcnow()

    # Before anything is measured: turn the database's own SET NULL into an
    # ordinary ORM update, so it lands in the work list below.
    _nullify_referencing_children(session)

    work: list[tuple[Any, ChangeOp]] = []
    for obj in session.new:
        if is_audited(obj):
            # Before the image is taken, or the image is of a row that never
            # existed -- see _apply_pending_defaults.
            _apply_pending_defaults(obj)
    work += [(o, ChangeOp.insert) for o in session.new if is_audited(o)]
    # session.dirty holds anything that was touched; is_modified filters out the
    # ones that ended up unchanged, so a no-op edit does not pollute the
    # recency check that undo depends on.
    work += [(o, ChangeOp.update) for o in session.dirty if is_audited(o) and session.is_modified(o)]
    work += [(o, ChangeOp.delete) for o in session.deleted if is_audited(o)]
    work += [(o, ChangeOp.delete) for o in _orphans(session)]

    # Compute the images before deciding anything. An update whose every
    # changed column is redacted -- a password rehash, the TOTP counter moving
    # on after a sign-in -- records nothing but its own existence, so it is not
    # a change, and must not demand a batch either.
    records = []
    for obj, op in work:
        before, after, redacted = images(obj)
        if op is ChangeOp.update and before == after:
            continue
        records.append((obj, op, before, after, redacted))

    if not records:
        return

    if batch is None:
        first = records[0][0]
        raise NoOpenBatch(
            f"{type(first).__name__} was written with no batch open. "
            "Wrap the operation in `with batch(session, kind=..., actor_id=...)` "
            "so the change is attributable and undoable."
        )

    for obj, _op, before, after, _redacted in records:
        _refuse_other_household(batch, obj, before, after)

    for obj, op, before, after, redacted in records:
        session.add(
            Change(
                batch_id=batch.id,
                household_id=batch.household_id or getattr(obj, "household_id", None),
                table_name=obj.__tablename__,
                row_id=obj.id,
                op=op,
                before=None if op is ChangeOp.insert else before,
                after=None if op is ChangeOp.delete else after,
                redacted=redacted or None,
                at=at,
            )
        )


def _refuse_other_household(batch: Any, obj: Any, before: dict, after: dict) -> None:
    """A batch filed under one household may only touch that household's rows.

    Every change row is stamped with ``batch.household_id``, and History, the
    change feed and undo are all scoped by that stamp. A row from household B
    written under a batch filed as A is therefore readable -- whole snapshots
    of it -- and undoable by A's members, none of whom can see B. Issue #77
    was a route that let exactly that through; this is the net under every
    other route.

    Only a household on *both* sides is compared. A batch with none (sign-in,
    a profile edit, first-run setup) spans no household by design, and a row
    with no ``household_id`` column (a household itself, a user) has nothing
    to disagree with. Both images are checked so a row cannot be moved *out*
    of another household either.
    """
    filed_under = batch.household_id
    if filed_under is None:
        return
    for image in (before, after):
        row_household = image.get("household_id")
        if row_household is not None and row_household != filed_under:
            raise CrossHouseholdChange(
                f"{type(obj).__name__} {obj.id} belongs to another household than "
                "the batch writing it. Load it scoped to the batch's household."
            )


def _apply_pending_defaults(obj: Any) -> None:
    """Fill in Python-side column defaults before the insert's image is taken.

    The persistence step evaluates ``mapped_column(default=...)`` *after*
    ``before_flush``, so on a pending object ``history`` is empty for every such
    column and the snapshot wrote ``None``. The log then asserted a row that
    never existed: ``closed: None`` where the row holds ``False``,
    ``created_at: None``, ``sort_order: None``, ``enabled: None``. Audit Log
    Decision calls before/after "JSON snapshots of the row", so this mattered
    for reading the log even though undo happens to use only ``before``.

    Assigning the value here is what makes the image true: SQLAlchemy then
    writes what was assigned rather than re-evaluating the default, so the log
    and the row cannot disagree.
    """
    state = inspect(obj)
    for attr in state.mapper.column_attrs:
        column = attr.columns[0]
        default = column.default
        if default is None or column.primary_key:
            continue
        if getattr(obj, attr.key, None) is not None:
            continue
        try:
            if default.is_scalar:
                setattr(obj, attr.key, default.arg)
            elif default.is_callable:
                setattr(obj, attr.key, default.arg(None))
        except Exception:  # pragma: no cover - a default needing real context
            # A default that genuinely needs the execution context is left to
            # the persistence step; an incomplete image beats a wrong row.
            continue


def _nullify_referencing_children(session: Session) -> None:
    """Clear nullable foreign keys that point at a row being deleted.

    ``transactions.payee_id`` is ``ON DELETE SET NULL``. SQLite applies that
    itself, and SQLAlchemy's own nullification runs *inside* the flush -- both
    after ``before_flush``, so deleting a payee used to clear it off every
    transaction that referenced it with no change row written. The undo
    "succeeded" having destroyed the attributions, and undoing the undo brought
    the payee back but not the links.

    Doing it here, explicitly, makes each one a normal audited update: logged,
    and reversible by the same replay as everything else.
    """
    # Read once. `session.deleted` builds a fresh set on every access, and
    # asking it once per child made undoing a 5,000-row import spend a
    # second in this loop alone.
    deleted = session.deleted
    for parent in list(deleted):
        if not is_audited(parent):
            continue
        state = inspect(parent)
        for relationship in state.mapper.relationships:
            if relationship.direction is not ONETOMANY:
                continue
            # A cascading delete is logged as a delete, not as a nulled link.
            if "delete" in relationship.cascade:
                continue
            child_mapper = relationship.mapper
            nullable = [
                child_mapper.get_property_by_column(remote).key
                for _, remote in relationship.local_remote_pairs
                if remote.nullable
            ]
            if not nullable:
                continue
            for child in getattr(parent, relationship.key):
                if child is None or not is_audited(child) or child in deleted:
                    continue
                for key in nullable:
                    if getattr(child, key) is not None:
                        setattr(child, key, None)


def _orphans(session: Session) -> list[Any]:
    """Children removed from a delete-orphan collection.

    ``household.accounts.remove(one)`` deletes that account and everything it
    owns, but the unit of work only decides that *during* the flush -- so
    ``session.deleted`` is empty when the hook looks, and the rows would go
    unlogged, unrecoverable, and without a batch being demanded.
    """
    found: list[Any] = []
    seen: set[int] = set()
    deleted = session.deleted  # once: see _nullify_referencing_children
    for parent in list(session.dirty) + list(deleted):
        state = inspect(parent)
        for relationship in state.mapper.relationships:
            if relationship.direction is not ONETOMANY:
                continue
            if "delete-orphan" not in relationship.cascade:
                continue
            history = state.attrs[relationship.key].history
            for child in history.deleted:
                if child is None or not is_audited(child) or id(child) in seen:
                    continue
                if child in deleted:
                    continue
                seen.add(id(child))
                found.append(child)
                found.extend(_owned_by(session, child, seen))
    return found


def _owned_by(session: Session, parent: Any, seen: set[int]) -> list[Any]:
    """Everything that goes with an orphan, recursively."""
    found: list[Any] = []
    state = inspect(parent)
    for relationship in state.mapper.relationships:
        if relationship.direction is not ONETOMANY or "delete-orphan" not in relationship.cascade:
            continue
        for child in getattr(parent, relationship.key, []) or []:
            if not is_audited(child) or id(child) in seen:
                continue
            seen.add(id(child))
            found.append(child)
            found.extend(_owned_by(session, child, seen))
    return found
