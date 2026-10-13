"""What the desktop app does with its ledger before the app is imported (#318).

On a server an operator runs the upgrade drill by hand, reads it, and starts
the service. The desktop app has nobody at a terminal: each launch of a newer
version must do the same work itself, in this order, before `app.main` opens
the database --

1. **hold the ledger** (`instance_lock`): a second copy of the app on the same
   folder stops here;
2. **compare** the ledger's stamp with this code's migrations. A stamp this
   code has never heard of means a newer version wrote it: nothing is touched,
   and the shell says "opened by a newer version";
3. **ask**, when a migration cannot be undone (`Reversible: lossy`): the shell
   shows its own dialog, and declining changes nothing;
4. **back up** with `VACUUM INTO`, reopened and counted, into the ledger's own
   `backups/`;
5. **migrate** in this process (`app.migrate`);
6. **count again**: a table holding fewer rows than the backup counted means
   the app must not start, and the backup is where the ledger is;
7. **write `upgrade.log`** beside the backup.

It writes nothing to standard output, which belongs to the shell.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

READY = "ready"
NEWER = "newer"
DECLINED = "declined"
MIGRATION_FAILED = "migration-failed"
ROWS_DROPPED = "rows-dropped"


@dataclass
class Prepared:
    outcome: str
    stamp_before: str | None = None
    stamp_after: str | None = None
    migrated: list[str] = field(default_factory=list)
    lossy: list[str] = field(default_factory=list)
    backup: Path | None = None
    dropped: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == READY


def _to_stderr(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _counts(database: Path, tables) -> dict[str, int]:
    with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}


def prepare(
    *,
    say: Callable[[str], None] = _to_stderr,
    confirm_lossy: Callable[[list[str]], bool] = lambda _titles: False,
) -> Prepared:
    """Bring the ledger to this code's schema, or say why not. Raises
    `instance_lock.LedgerBusy` when another process holds it.

    `confirm_lossy` is shown the titles of the migrations that cannot be
    undone; it declines unless the shell says otherwise.
    """
    from app import config, instance_lock, migrate, paths
    from scripts import backup as backup_script
    from scripts import upgrade as drill

    settings = config.settings
    lines: list[str] = []

    def tell(line: str) -> None:
        lines.append(line)
        say(line)

    # Held for as long as this process runs; the app's own startup takes it
    # again, counted, and the operating system lets go of it at exit.
    instance_lock.acquire(settings.data_dir)

    before = drill.stamped()
    if before is not None and before not in {one.revision for one in drill.chain()}:
        tell(f"the ledger is at {before}, which this version does not know: a newer one wrote it")
        return Prepared(NEWER, stamp_before=before, stamp_after=before, log=lines)

    upcoming = drill.pending(before)
    if not upcoming:
        return Prepared(READY, stamp_before=before, stamp_after=before, log=lines)

    lossy = [one.title for one in upcoming if one.reversible == "lossy"]
    if lossy and before is not None and not confirm_lossy(lossy):
        tell("declined: nothing was changed")
        return Prepared(DECLINED, stamp_before=before, stamp_after=before, lossy=lossy, log=lines)

    database = paths.database()
    backup: Path | None = None
    rows_before: dict[str, int] = {}
    # A ledger that was never stamped is new: there is nothing in it to keep.
    if before is not None and database is not None and database.exists():
        backup = backup_script.take(paths.backups())
        # `take` has reopened the copy and counted it against the live ledger;
        # its manifest holds the "before" of every table, as for the server's
        # drill.
        rows_before = json.loads((backup / "manifest.json").read_text())["rows"]
        tell(f"backed up to {backup}, reopened and counted")

    def done(outcome: str, **more) -> Prepared:
        after = drill.stamped()
        prepared = Prepared(
            outcome,
            stamp_before=before,
            stamp_after=after,
            migrated=[one.revision for one in upcoming],
            lossy=lossy,
            backup=backup,
            log=lines,
            **more,
        )
        if backup is not None:
            (backup / "upgrade.log").write_text("\n".join(lines) + "\n")
        return prepared

    tell(f"migrating {len(upcoming)} step(s) from {before or 'nothing'}")
    try:
        migrate.upgrade_to_head(say=tell)
    except Exception as problem:  # noqa: BLE001 - reported, and the backup is the way back
        tell(f"the migration failed: {problem}")
        return done(MIGRATION_FAILED)

    if rows_before and database is not None:
        now = _counts(database, rows_before)
        dropped = sorted(table for table, was in rows_before.items() if now[table] < was)
        if dropped:
            tell(f"fewer rows than the backup in: {', '.join(dropped)}; do not start")
            return done(ROWS_DROPPED, dropped=dropped)
    return done(READY)
