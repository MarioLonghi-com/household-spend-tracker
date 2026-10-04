"""Assembling the before and after images of a row.

One rule governs this module: read ``AttributeState.history`` and nothing else.
``load_history()`` looks like the thorough choice and is a trap -- on an expired
instance that already carries pending changes it refreshes the object and then
reports the *pending* value as the committed one, producing a before-image that
never existed. An audit log that lies is worse than none.
"""

from __future__ import annotations

import base64
import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm.interfaces import MANYTOONE

from .registry import redacted_columns


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode()}
    # A JSON column is already JSON, so it goes in as itself rather than
    # through `str()` below. Without this the image holds a Python *repr* --
    # `"{'Make': 'Apple'}"` -- which writes back as a string on undo, and the
    # column quietly stops being a dict. `receipts.exif` is the first JSON
    # column on an audited table, so nothing had reached this before.
    if isinstance(value, dict):
        return {str(key): _jsonable(one) for key, one in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(one) for one in value]
    return str(value)


def images(obj: Any) -> tuple[dict, dict, list[str]]:
    """Return ``(before, after, redacted)`` for one object.

    Both images carry **every** column, not only the dirty ones: unchanged
    attributes contribute their current value, changed ones contribute the value
    the database still holds. That is what makes ``before`` a complete row, and
    therefore what makes undo a plain assignment rather than a merge.
    """
    state = inspect(obj)
    redact = redacted_columns(type(obj))
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    for attr in state.mapper.column_attrs:
        key = attr.key
        if key in redact:
            continue
        history = state.attrs[key].history
        if history.deleted:
            old = history.deleted[0]
        elif history.unchanged:
            old = history.unchanged[0]
        else:
            # Never loaded, so history knows nothing. Read it -- guessing null
            # here would write a null back on undo.
            old = getattr(obj, key, None) if key in state.unloaded else None
        new = history.added[0] if history.added else old
        before[key] = _jsonable(old)
        after[key] = _jsonable(new)

    _fold_in_relationship_changes(state, after)
    return before, after, sorted(redact)


def _fold_in_relationship_changes(state: Any, after: dict[str, Any]) -> None:
    """Account for ``txn.account = other`` as well as ``txn.account_id = ...``.

    Assigning a relationship does not touch the foreign key column until the
    unit of work synchronises it, which happens *during* the flush -- after the
    hook has run. Without this the images would show the old account id on both
    sides, the change would look like a no-op, and money would move between
    accounts with nothing recorded and no batch demanded.
    """
    for relationship in state.mapper.relationships:
        if relationship.direction is not MANYTOONE:
            continue
        history = state.attrs[relationship.key].history
        if not history.added:
            continue
        target = history.added[0]
        for local, remote in relationship.local_remote_pairs:
            column = state.mapper.get_property_by_column(local).key
            if column in after:
                after[column] = _jsonable(
                    getattr(target, remote.key, None) if target is not None else None
                )
