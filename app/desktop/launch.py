"""What the desktop app does with its ledger before the app is imported (#318).

On a server an operator runs the upgrade drill by hand, reads it, and starts
the service. The desktop app has nobody at a terminal: each launch of a newer
version must do the same work itself, in this order, before `app.main` opens
the database --

1. **hold the ledger** (`instance_lock`): a second copy of the app on the same
   folder stops here;
   then **finish or undo a restore that was killed** (`restore.recover`,
   #442), so a ledger a restore had set aside is never mistaken for no ledger
   and replaced by a new, empty one; and empty `.outgoing` of the copies a
   shell that crashed never collected (`control.OUTGOING`). A ledger file
   that is not a database stops here, untouched;
2. **compare** the ledger's stamp with this code's migrations. A stamp this
   code has never heard of means a newer version wrote it: nothing is touched,
   and the shell says "opened by a newer version";
3. **ask**, when a migration cannot be undone (`Reversible: lossy`): the shell
   shows its own dialog, and declining changes nothing;
4. **back up** with `VACUUM INTO`, reopened and counted, into the ledger's own
   `backups/` -- and a backup that cannot be made stops here, unmigrated;
5. **migrate** in this process (`app.migrate`);
6. **count again**: a table holding fewer rows than the backup counted means
   the app must not start, and the backup is where the ledger is;
7. **write `upgrade.log`** beside the backup.

It writes nothing to standard output, which belongs to the shell.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

READY = "ready"
NEWER = "newer"
DECLINED = "declined"
MIGRATION_FAILED = "migration-failed"
BACKUP_FAILED = "backup-failed"
ROWS_DROPPED = "rows-dropped"
DAMAGED = "damaged"
RESTORE_UNFINISHED = "restore-unfinished"


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
    #: What `restore.recover` did before anything else: `rolled-back` (the
    #: last restore did not finish, and the ledger is the one from before
    #: it), `finished`, or None.
    recovered: str | None = None

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
    starting: Callable[[int], None] = lambda _steps: None,
) -> Prepared:
    """Bring the ledger to this code's schema, or say why not. Raises
    `instance_lock.LedgerBusy` when another process holds it.

    `confirm_lossy` is shown the titles of the migrations that cannot be
    undone; it declines unless the shell says otherwise. `starting` is told
    how many steps there are just before the backup and the migration begin,
    so the shell can say "Upgrading your ledger…" rather than show nothing; a
    ledger being made for the first time is not announced.
    """
    from app import config, instance_lock, migrate, paths
    from app.desktop import control, restore
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

    database = paths.database()
    recovered: str | None = None
    if database is not None:
        try:
            recovered = restore.recover(database)
        except OSError as problem:
            tell(f"the last restore did not finish, and the ledger could not be put back: {problem}")
            tell(f"the ledger as it was is beside {database.name}, named {restore.ASIDE}<time>")
            return Prepared(RESTORE_UNFINISHED, log=lines)
        if recovered == restore.ROLLED_BACK:
            tell("the last restore did not finish: the ledger is the one from before it")
        elif recovered == restore.FINISHED:
            tell("the last restore had put the backup in place; tidied up after it")
    outgoing = settings.data_dir / control.OUTGOING
    if outgoing.exists():
        # No backup can be under way: this process has not served one yet.
        shutil.rmtree(outgoing, ignore_errors=True)

    try:
        before = drill.stamped()
    except sqlite3.DatabaseError as problem:
        # A partial or damaged file: never a new ledger, and never migrated.
        tell(f"{database.name if database else 'the ledger'} is not a readable database ({problem}); nothing was changed")
        return Prepared(DAMAGED, log=lines, recovered=recovered)
    if before is not None and before not in {one.revision for one in drill.chain()}:
        tell(f"the ledger is at {before}, which this version does not know: a newer one wrote it")
        return Prepared(NEWER, stamp_before=before, stamp_after=before, log=lines, recovered=recovered)

    upcoming = drill.pending(before)
    if not upcoming:
        return Prepared(READY, stamp_before=before, stamp_after=before, log=lines, recovered=recovered)

    lossy = [one.title for one in upcoming if one.reversible == "lossy"]
    if lossy and before is not None and not confirm_lossy(lossy):
        tell("declined: nothing was changed")
        return Prepared(DECLINED, stamp_before=before, stamp_after=before, lossy=lossy, log=lines, recovered=recovered)

    if before is not None:
        # A new ledger is being made, not upgraded: nothing to announce.
        starting(len(upcoming))
    backup: Path | None = None
    rows_before: dict[str, int] = {}
    # A ledger that was never stamped is new: there is nothing in it to keep.
    if before is not None and database is not None and database.exists():
        # The key first, made if this ledger arrived without one -- moved by
        # hand, or restored from a server. The backup refuses to be taken
        # without it, and on the desktop it opens nothing: there are no
        # authenticators (#320), so a new key loses nothing an old one held.
        settings.key()
        try:
            backup = backup_script.take(paths.backups())
        except SystemExit as refused:
            # The drill's sentence, said to the shell rather than ending the
            # process: nothing has been migrated, so nothing is at risk.
            tell(f"the backup before the upgrade could not be made: {refused}")
            return Prepared(BACKUP_FAILED, stamp_before=before, stamp_after=before, log=lines, recovered=recovered)
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
            recovered=recovered,
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
