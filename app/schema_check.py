"""Refuse to start against a schema the migrations did not produce.

The app has always left schema changes to Alembic -- ``lifespan`` says so, and
says why the previous build's ``create_all`` was wrong. What it did not do was
*check*, so a database that was behind, ahead, or never migrated at all started
perfectly happily and failed later, at whatever request first touched the
missing piece.

That is not hypothetical. A demo database built by an older ``seed_demo`` had no
``alembic_version`` row at all; the app started, ``/api/health`` answered ok, and
signing in as the owner threw a 500 out of the SQLite driver:

    no such table: pending_sign_ins

A 500 on sign-in is a bad way to learn you forgot ``make migrate``. Checking at
boot turns it into one sentence before the server ever accepts a connection.

There is deliberately no way to skip this. If the schema is wrong the answer is
to run the migrations, and an escape hatch is just a way to ship the 500.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


class SchemaOutOfDate(RuntimeError):
    """Raised at startup. Ends the process rather than serving broken routes."""


def expected_head() -> str | None:
    """The revision this code's migrations end at."""
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    return script.get_current_head()


def known_revisions() -> set[str]:
    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    return {revision.revision for revision in script.walk_revisions()}


def stamped(engine: Engine) -> str | None:
    """What the database says it is, or None if it has never been migrated."""
    if not inspect(engine).has_table("alembic_version"):
        return None
    with engine.connect() as connection:
        rows = connection.exec_driver_sql("select version_num from alembic_version").fetchall()
    if not rows:
        return None
    # More than one head is a real state Alembic supports, and one this project
    # has no branches to produce -- so say so plainly rather than picking one.
    if len(rows) > 1:
        raise SchemaOutOfDate(
            "this database is stamped with several revisions "
            f"({', '.join(sorted(r[0] for r in rows))}), which this project has no branches to "
            "produce. Resolve it with `alembic merge` or rebuild the database."
        )
    return rows[0][0]


def verify(engine: Engine) -> str:
    """Return the current revision, or raise with something worth reading."""
    head = expected_head()
    if head is None:  # pragma: no cover - only if migrations/ is empty
        raise SchemaOutOfDate("no migrations found; this build is not usable")

    current = stamped(engine)
    if current == head:
        return current

    if current is None:
        has_tables = bool(set(inspect(engine).get_table_names()) - {"alembic_version"})
        if has_tables:
            raise SchemaOutOfDate(
                "this database has tables but no Alembic stamp, so it was built by something "
                "other than the migrations -- an old `create_all`, most likely. Its shape cannot "
                "be trusted to match this code.\n"
                f"  If it really is at {head}:  alembic stamp {head}\n"
                "  Otherwise rebuild it:       make seed"
            )
        raise SchemaOutOfDate(
            "this database has never been migrated.\n"
            "  make migrate   to build the schema\n"
            "  make seed      to build it with a demo household as well"
        )

    if current not in known_revisions():
        raise SchemaOutOfDate(
            f"this database is stamped {current}, which is not a revision this code knows about. "
            "The database is most likely ahead of the code -- deploy the newer build, or "
            "downgrade the database deliberately."
        )

    raise SchemaOutOfDate(
        f"this database is at {current} and this code expects {head}.\n"
        "  make migrate   to bring it up to date"
    )
