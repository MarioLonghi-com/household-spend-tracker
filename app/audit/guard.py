"""The runtime half of "no bulk statements against audited tables".

A CI grep catches the pattern at review time and is easy to evade; this makes
the blind spot unreachable at runtime. It listens on ``Session``, so Alembic --
which runs Core statements on a bare ``Connection`` -- is unaffected, which is
right: a data migration must be able to rewrite audited tables without a batch.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import TextClause

from ..errors import BulkStatementForbidden
from .registry import audited_models


@event.listens_for(Session, "do_orm_execute")
def _no_bulk_writes_to_audited_tables(state: Any) -> None:
    if not (state.is_update or state.is_delete or state.is_insert):
        return
    table = getattr(state.statement, "table", None)
    if table is None or table.name not in audited_models():
        return
    verb = "update" if state.is_update else ("delete" if state.is_delete else "insert")
    raise BulkStatementForbidden(
        f"bulk {verb} against the audited table {table.name!r} bypasses the audit hook. "
        "Load the rows and change them, so every effect is recorded."
    )


class AuditedSession(Session):
    """A session with the legacy bulk shortcuts closed off.

    ``bulk_save_objects`` and friends never reach ``before_flush`` or
    ``do_orm_execute`` -- they build statements directly. CLAUDE.md promises the
    hook raises if you write to an audited table without a batch, and these were
    the three ways that promise was not true.
    """

    def _refuse(self, name: str) -> None:
        raise BulkStatementForbidden(
            f"{name}() writes rows without the ORM events the audit log listens to, "
            "so nothing would be recorded. Add the objects and flush instead."
        )

    def bulk_save_objects(self, *args: Any, **kwargs: Any) -> None:
        self._refuse("bulk_save_objects")

    def bulk_insert_mappings(self, *args: Any, **kwargs: Any) -> None:
        self._refuse("bulk_insert_mappings")

    def bulk_update_mappings(self, *args: Any, **kwargs: Any) -> None:
        self._refuse("bulk_update_mappings")


_WRITING_SQL = re.compile(r"^\s*(insert|update|delete|replace|drop|alter|truncate)\b", re.I)


@event.listens_for(Session, "do_orm_execute")
def _no_raw_writes_without_a_batch(state: Any) -> None:
    """A raw ``text()`` write is invisible to the hook as well.

    Pragmas and reads are fine; anything that writes has to go through the ORM
    so the change is recorded. Alembic is unaffected -- it runs on a bare
    Connection, not a Session.
    """
    statement = state.statement
    if not isinstance(statement, TextClause):
        return
    if not _WRITING_SQL.match(str(statement)):
        return
    raise BulkStatementForbidden(
        "a raw SQL write bypasses the audit hook entirely. Change the rows through "
        "the ORM so the change is recorded."
    )
