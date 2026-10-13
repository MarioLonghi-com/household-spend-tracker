"""What only the desktop shell may ask of its backend (#62).

The page's requests arrive as HTTP and go through every check the server has.
These do not: they are frames the shell writes itself, with a `control` name
where a request has a method, so no page -- and nothing that can make a page
send a request -- can reach them. `stdio_adapter.serve_control` answers them.

- **`state`**: the newest position in the audit log, so the shell can tell
  whether anything changed since its last backup without asking anybody to
  sign in;
- **`backup`**: a consistent copy of the ledger, made the way every other
  backup here is (`VACUUM INTO`, then reopened, checked and counted), into
  `data_dir/.outgoing`. The shell moves it to the backups folder the person
  chose, which the Mac App Store sandbox lets the shell reach and not this
  process.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

#: Inside the data directory: private, on the ledger's own filesystem, and
#: emptied before every backup, so a copy the shell never collected (it crashed
#: between the two steps) is gone by the next one rather than kept forever.
OUTGOING = ".outgoing"


class ControlRefused(RuntimeError):
    """A control request that could not be done, with the sentence why."""


def _database() -> Path:
    from app import paths

    database = paths.database()
    if database is None:
        raise ControlRefused("the desktop ledger is a SQLite file, and this one is not")
    return database


def changes_head(database: Path) -> int:
    """The last `seq` in the audit log, 0 for a ledger nobody has touched."""
    with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        try:
            found = conn.execute("SELECT max(seq) FROM changes").fetchone()[0]
        except sqlite3.OperationalError:
            # Not migrated yet: the shell asks before the app has started.
            return 0
    return found or 0


def state() -> dict:
    database = _database()
    return {"changes_head": changes_head(database) if database.exists() else 0}


def backup(now: datetime | None = None) -> dict:
    """Write, check and count one copy. Raises `ControlRefused` with the reason."""
    from app import config
    from app.permissions import private_dir
    from app.services import backup_bundle

    database = _database()
    outgoing = config.settings.data_dir / OUTGOING
    if outgoing.exists():
        shutil.rmtree(outgoing)
    private_dir(outgoing)
    taken = (now or datetime.now(UTC)).astimezone(UTC)
    target = outgoing / f"spendtracker-{taken.strftime('%Y%m%dT%H%M%SZ')}.sqlite3"

    # Read before the copy, so a write landing during it can only make the
    # copy newer than what the shell records, never older.
    head = changes_head(database)
    with contextlib.closing(sqlite3.connect(database, isolation_level=None)) as conn:
        try:
            conn.execute("VACUUM INTO ?", (str(target),))
        except sqlite3.Error as problem:
            raise ControlRefused(f"the copy could not be written: {problem}") from problem

    verdict = backup_bundle.integrity(target)
    if verdict != "ok":
        raise ControlRefused(f"the copy fails its integrity check: {verdict}")
    revision = backup_bundle.revision(target)
    if revision is None:
        raise ControlRefused("the copy has no schema stamp, so it did not finish")
    rows = backup_bundle.counts(target)
    return {
        "path": str(target),
        "name": target.name,
        "bytes": target.stat().st_size,
        "taken_at": taken.isoformat(),
        "revision": revision,
        "rows": sum(rows.values()),
        "changes_head": head,
    }


COMMANDS = {"state": state, "backup": backup}
