"""Putting a backup back, for the desktop app's "Restore from backup…" (#62).

`scripts/restore.py` is the server's: a person at a terminal, a key to settle,
a port to check, a prompt to type into. The desktop has none of those, and the
same file is also how a ledger moves to a new computer. So this is the same
semantics without the conversation:

1. **hold the ledger** (`instance_lock`): the shell has stopped the app first,
   and this refuses if anything still has it open;
2. **read the backup before touching anything**: a `.sqlite3` this app wrote,
   or a `.zip` from the Application screen (unpacked by name, as the server
   does). It must pass `integrity_check`, carry a schema stamp, and hold this
   app's tables;
3. **refuse a newer ledger**: a stamp this code does not know means a newer
   version wrote it, and opening it here would be the "opened by a newer
   version" stop one launch later, after the swap. Nothing is changed;
4. **forget Touch ID** (#432) in the copy about to go in: every
   `device_unlocks` row goes, so a secret the shell holds -- this computer's
   or, when the file is how a ledger moves, another's -- opens nothing until
   the person turns it on again. The shell forgets its own copy beside this.
   Done before the swap, so a swapped-in ledger has never held one;
5. **mark the restore as under way** (`<ledger>.restoring`), flushed to the
   disk before anything moves;
6. **move the live ledger aside**, renamed `.before-restore-<stamp>`, never
   deleted, and only the newest such set is kept. The shell has made a fresh
   backup into the backups folder before asking for this, so the aside copy
   is the second way back, not the only one;
7. **copy the backup in** under a temporary name, flushed, then renamed onto
   the ledger's name in one step (`permissions.replace_private`).
   `secret.key` stays as it is: the desktop has no authenticators (#320), so
   there is nothing in a ledger the key must open;
8. **clear the mark.**

**A restore that is killed** -- the app quit, the computer lost power -- is
finished or undone by the next start, before anything opens the ledger
(`recover`, #442). The mark says a restore was under way: a ledger in place
beside its set-aside one is the restored one, and the rest is tidied; no
ledger means the copy never landed, and the set-aside files go back. A
set-aside set with no ledger and no mark -- what an older version left when
it was killed -- goes back too. Either way the next start never sees a
missing ledger and makes a new, empty one in its place.

An older backup is not migrated here. The next start does it the way every
start does (`launch.prepare`): backed up, migrated, counted.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

RESTORED = "restored"
REFUSED = "refused"
NEWER = "newer"

ASIDE = ".before-restore-"


@dataclass
class Restored:
    outcome: str
    reason: str = ""
    revision: str | None = None
    rows: int = 0
    aside: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == RESTORED


def _materialise(source: Path, scratch: Path) -> Path:
    from app.permissions import copy_private
    from app.services import backup_bundle

    if source.suffix.lower() == ".zip":
        return backup_bundle.unpack(source, scratch) / backup_bundle.DATABASE
    copied = scratch / backup_bundle.DATABASE
    copy_private(source, copied)
    return copied


#: Beside the ledger while a restore is moving files: what the next start
#: reads to finish or undo one that was killed (#442).
MARKER = ".restoring"

#: What `recover` did.
ROLLED_BACK = "rolled-back"
FINISHED = "finished"


def _marker(live: Path) -> Path:
    return live.with_name(live.name + MARKER)


def _the_set(live: Path) -> tuple[Path, ...]:
    from app import paths

    return (live, *paths.sidecars(live))


def _aside(path: Path, stamp: str) -> Path:
    return path.with_name(f"{path.name}{ASIDE}{stamp}")


def _step(_name: str) -> None:
    """Between two steps of the swap. Nothing, here; a test kills the process
    in it to leave on disk exactly what a kill at that point leaves."""


def _drop_older_asides(live: Path, keep_stamp: str) -> None:
    for old in live.parent.glob(f"*{ASIDE}*"):
        if not old.name.endswith(keep_stamp) and old.is_file():
            old.unlink()


def _write_marker(live: Path, stamp: str, had_ledger: bool) -> None:
    from app.permissions import sync_dir

    marker = _marker(live)
    with tempfile.NamedTemporaryFile("w", dir=live.parent, prefix=".restoring-", delete=False) as out:
        json.dump({"stamp": stamp, "had_ledger": had_ledger}, out)
        out.flush()
        os.fsync(out.fileno())
    written = Path(out.name)
    try:
        os.replace(written, marker)
    except BaseException:
        written.unlink(missing_ok=True)
        raise
    sync_dir(live.parent)


def _clear_marker(live: Path) -> None:
    from app.permissions import sync_dir

    _marker(live).unlink(missing_ok=True)
    sync_dir(live.parent)


def _newest_aside_stamp(live: Path) -> str | None:
    prefix = f"{live.name}{ASIDE}"
    stamps = sorted(one.name[len(prefix) :] for one in live.parent.glob(f"{prefix}*") if one.is_file())
    return stamps[-1] if stamps else None


def _put_back(live: Path, stamp: str) -> None:
    from app.permissions import sync_dir

    for path in _the_set(live):
        aside = _aside(path, stamp)
        if aside.exists() and not path.exists():
            aside.rename(path)
    sync_dir(live.parent)


def recover(live: Path) -> str | None:
    """Finish or undo a restore that did not get to its end (#442).

    Called with the ledger held, before anything opens it: by every start
    (`launch.prepare`) and by every restore before it moves anything, so a
    second restore never sets aside -- and later drops -- the files that hold
    the real ledger. Returns `FINISHED`, `ROLLED_BACK`, or None when there was
    nothing to do.
    """
    from app.permissions import partial_of

    marker = _marker(live)
    partial = partial_of(live)
    if marker.exists():
        try:
            said = json.loads(marker.read_text())
            stamp, had_ledger = str(said["stamp"]), bool(said.get("had_ledger", True))
        except (OSError, ValueError, KeyError, TypeError):
            stamp, had_ledger = _newest_aside_stamp(live), True
        partial.unlink(missing_ok=True)
        if live.exists() and stamp and (_aside(live, stamp).exists() or not had_ledger):
            # The copy was renamed into place: only the tidying was left.
            _drop_older_asides(live, stamp)
            _clear_marker(live)
            return FINISHED
        if not live.exists() and stamp:
            _put_back(live, stamp)
        # Otherwise nothing had moved yet: the ledger is the one it was.
        _clear_marker(live)
        return ROLLED_BACK
    if not live.exists():
        stamp = _newest_aside_stamp(live)
        if stamp is not None:
            # Set aside and never replaced, by a version without the mark.
            partial.unlink(missing_ok=True)
            _put_back(live, stamp)
            return ROLLED_BACK
    return None


def _forget_device_unlocks(database: Path) -> None:
    """Step 4. A ledger from before the table existed has nothing to forget;
    the next start's migration makes it."""
    import contextlib
    import sqlite3

    with contextlib.closing(sqlite3.connect(database)) as conn:
        try:
            conn.execute("DELETE FROM device_unlocks")
            conn.commit()
        except sqlite3.OperationalError as problem:
            if "no such table" not in str(problem):
                raise
        # Into the file itself: only the file is copied in, never a WAL.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def restore(source: Path, *, now: datetime | None = None) -> Restored:
    """Put `source` in place of the live ledger, or say why not.

    Raises `instance_lock.LedgerBusy` when another process holds the ledger.
    """
    from app import config, instance_lock, paths
    from app.permissions import private_dir, replace_private, sync_dir
    from app.services import backup_bundle
    from scripts import upgrade as drill

    source = Path(source)
    if not source.is_file():
        return Restored(REFUSED, f"{source.name} is not there any more")
    if source.suffix.lower() not in {".sqlite3", ".zip"}:
        return Restored(REFUSED, f"{source.name} is not a Spend Tracker backup (.sqlite3 or .zip)")

    settings = config.settings
    live = paths.database()
    if live is None:
        return Restored(REFUSED, "the desktop ledger is a SQLite file, and this one is not")

    with instance_lock.holding(settings.data_dir):
        private_dir(settings.data_dir)
        recover(live)
        with tempfile.TemporaryDirectory(dir=settings.data_dir, prefix=".restore-") as scratch:
            try:
                candidate = _materialise(source, Path(scratch))
            except backup_bundle.BundleRefused as refused:
                return Restored(REFUSED, str(refused))

            verdict = backup_bundle.integrity(candidate)
            if verdict != "ok":
                return Restored(REFUSED, f"{source.name} is damaged: {verdict}")
            revision = backup_bundle.revision(candidate)
            if revision is None:
                return Restored(REFUSED, f"{source.name} is not a Spend Tracker ledger, or the copy did not finish")
            rows = backup_bundle.counts(candidate)
            if not rows:
                return Restored(REFUSED, f"{source.name} holds none of this app's tables")
            if revision not in {one.revision for one in drill.chain()}:
                return Restored(
                    NEWER,
                    f"{source.name} was written by a newer version of Spend Tracker; update this one first",
                    revision=revision,
                )

            # Before the swap, so no ledger that is in place has ever held one.
            _forget_device_unlocks(candidate)

            stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
            # A second restore within the same second must not rename over
            # the first one's set-aside ledger.
            base, again = stamp, 2
            while any(_aside(path, stamp).exists() for path in _the_set(live)):
                stamp, again = f"{base}-{again}", again + 1
            moved: list[tuple[Path, Path]] = []
            _write_marker(live, stamp, had_ledger=live.exists())
            try:
                _step("marked")
                for path in _the_set(live):
                    if path.exists():
                        aside = _aside(path, stamp)
                        path.rename(aside)
                        moved.append((path, aside))
                        _step(f"moved {path.name}")
                sync_dir(live.parent)
                replace_private(candidate, live)
                _step("replaced")
                _drop_older_asides(live, stamp)
                _clear_marker(live)
            except BaseException:
                # Neither the old ledger nor the new one is the one outcome
                # worse than not restoring: put it back as it was -- or, if
                # the new one is already whole in place, finish.
                recover(live)
                raise
    return Restored(
        RESTORED,
        revision=revision,
        rows=sum(rows.values()),
        aside=[aside.name for _, aside in moved],
    )
