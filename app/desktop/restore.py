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
4. **move the live ledger aside**, renamed `.before-restore-<stamp>`, never
   deleted, and only the newest such set is kept. The shell has made a fresh
   backup into the backups folder before asking for this, so the aside copy
   is the second way back, not the only one;
5. **copy the backup in.** `secret.key` stays as it is: the desktop has no
   authenticators (#320), so there is nothing in a ledger the key must open.

An older backup is not migrated here. The next start does it the way every
start does (`launch.prepare`): backed up, migrated, counted.
"""

from __future__ import annotations

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


def _drop_older_asides(directory: Path, keep_stamp: str) -> None:
    for old in directory.glob(f"*{ASIDE}*"):
        if not old.name.endswith(keep_stamp) and old.is_file():
            old.unlink()


def restore(source: Path, *, now: datetime | None = None) -> Restored:
    """Put `source` in place of the live ledger, or say why not.

    Raises `instance_lock.LedgerBusy` when another process holds the ledger.
    """
    from app import config, instance_lock, paths
    from app.permissions import copy_private, private_dir
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

            stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
            moved: list[tuple[Path, Path]] = []
            for path in (live, *(live.with_name(live.name + tail) for tail in paths.SIDECARS)):
                if path.exists():
                    aside = path.with_name(f"{path.name}{ASIDE}{stamp}")
                    path.rename(aside)
                    moved.append((path, aside))
            try:
                copy_private(candidate, live)
            except BaseException:
                # Neither the old ledger nor the new one is the one outcome
                # worse than not restoring: put it back as it was.
                live.unlink(missing_ok=True)
                for path, aside in moved:
                    aside.rename(path)
                raise
            _drop_older_asides(live.parent, stamp)
    return Restored(
        RESTORED,
        revision=revision,
        rows=sum(rows.values()),
        aside=[aside.name for _, aside in moved],
    )
