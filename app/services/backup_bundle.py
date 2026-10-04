"""One backup as a file you can carry: a zip that explains itself.

The screen's "Back up now" writes a bare `.sqlite3` into `data/backups`. That
is the right shape for a copy that stays beside the instance and the wrong one
for a copy that leaves it -- in a Drive folder, on a USB stick, in a drawer for
a year. Whoever opens it next may not be the person who made it, may not know
what Spend Tracker is, and will not have this screen to ask.

So the download is a zip with one folder in it, laid out exactly as
`scripts/backup.py` lays out a backup and `scripts/restore.py` reads one:

    spendtracker-<stamp>/
        spendtracker.sqlite3   the ledger, receipts included
        manifest.json          revision, row counts, sha256 -- what verify reads
        README.txt             for the future reader
        secret.key             only when the person asked for it

**The key is opt-in** (decided 2026-09-25, #133). Without it the ledger restores
and every authenticator is refused; with it, the zip is everything somebody
needs to sign in as anyone on the instance, sitting in a cloud folder. The
person downloading chooses, the screen says what each costs, and the README
says which one this is.

Nothing here imports the app's settings. The router passes in what it needs,
and `scripts/backup.py` shares the SQLite checks below without dragging a
configured app along with them.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

#: Counted on both sides of a copy. Not every table -- the point is a tripwire,
#: and these are the ones whose loss would matter and whose presence proves the
#: WAL was folded in.
COUNTED = (
    "transactions",
    "accounts",
    "households",
    "users",
    "batches",
    "changes",
    "receipts",
    "payees",
)

#: The names inside a backup folder. `restore` reads these and nothing else,
#: so a zip carrying `../../.ssh/authorized_keys` has nowhere to put it.
DATABASE = "spendtracker.sqlite3"
MANIFEST = "manifest.json"
README = "README.txt"
KEY = "secret.key"
MEMBERS = (DATABASE, MANIFEST, README, KEY)

#: What each member may inflate to. The ledger is the only large one; the rest
#: are a few hundred bytes to a few kilobytes. Checked against the zip's own
#: claim and then against the bytes actually written, so a zip bomb named
#: `spendtracker.sqlite3` stops at the ceiling rather than at a full disk (#225).
MAX_DATABASE_BYTES = 16 * 1024**3
MAX_SMALL_MEMBER_BYTES = 64 * 1024


class BundleRefused(RuntimeError):
    """A backup that will not be handed out, with the sentence saying why."""


# --------------------------------------------------------------------------- #
# Reading a copy
# --------------------------------------------------------------------------- #


def counts(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        present = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in COUNTED:
            if table in present:
                out[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return out


def revision(path: Path) -> str | None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
    return row[0] if row else None


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def integrity(path: Path) -> str:
    """`PRAGMA integrity_check`, which is the one a copy can fail on its own.

    Counting rows proves the tables are there. It does not prove the b-tree is
    intact, and a truncated or partly-written copy can answer a `count(*)` off
    a page it still has.
    """
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            return conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as broken:
        # Badly enough damaged and the pragma does not *report* a problem, it
        # raises one -- "database disk image is malformed". Left uncaught that
        # is a traceback where an operator needs a sentence.
        return f"it will not open: {broken}"


def a_sealed_secret(path: Path) -> tuple[str, bytes] | None:
    """One user's id and sealed TOTP secret, or None when nobody has enrolled.

    What a restore opens with the key it is about to put in place, because the
    wrong key leaves every row readable and every authenticator refused -- and
    nothing says so until somebody tries to sign in.
    """
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        try:
            row = conn.execute(
                "SELECT id, totp_secret FROM users WHERE totp_secret IS NOT NULL LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
    return (row[0], bytes(row[1])) if row else None


# --------------------------------------------------------------------------- #
# Building the zip
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Bundle:
    path: Path
    filename: str
    with_key: bool


def folder_name(backup: Path) -> str:
    """`spendtracker-20260925T112800Z.sqlite3` -> `spendtracker-20260925T112800Z`."""
    return backup.stem


def manifest(backup: Path, *, version: str, with_key: bool) -> dict:
    """Checked, then described. Raises `BundleRefused` on a copy that is not one.

    Read from the file itself, now. The in-app backup never had a manifest, so
    this is the first time anything writes its counts down -- and a file that
    fails `integrity_check` today is not handed to somebody to keep for a year
    on the strength of having passed when it was made.
    """
    verdict = integrity(backup)
    if verdict != "ok":
        raise BundleRefused(f"{backup.name} fails SQLite's integrity check ({verdict})")
    found = revision(backup)
    if found is None:
        raise BundleRefused(f"{backup.name} has no alembic_version, so it is not a ledger")
    made = datetime.fromtimestamp(backup.stat().st_mtime, UTC)
    return {
        "taken_at": made.isoformat(),
        "packaged_at": datetime.now(UTC).isoformat(),
        "packaged_by_version": version,
        "alembic_revision": found,
        "rows": counts(backup),
        "database_sha256": digest(backup),
        "database_bytes": backup.stat().st_size,
        "method": "vacuum",
        "secret_key": with_key,
    }


def readme(*, name: str, described: dict, repository: str) -> str:
    """What a stranger holding this zip in two years needs, in plain text.

    Plain text because it has to open on anything. Every command in it is one
    that exists, spelled as `scripts/restore.py` takes it; a test reads the
    restore steps back against the script's own arguments.
    """
    rows = "\n".join(
        f"    {table:<14}{count:>10,}" for table, count in sorted(described["rows"].items())
    )
    if described["secret_key"]:
        key_part = (
            "secret.key IS in this folder. It decrypts every member's authenticator\n"
            "(TOTP) secret. Treat this zip like a password: anybody holding it and a\n"
            "member's password can sign in as that member once it is restored.\n"
        )
        key_step = "   make restore FROM=/path/to/this.zip"
    else:
        key_part = (
            "secret.key is NOT in this folder. It was left out on purpose when the\n"
            "zip was made. The ledger restores without it, but every authenticator\n"
            "(TOTP) secret is encrypted with it: restored with a different key,\n"
            "nobody can sign in with their authenticator app. Restoring onto the\n"
            "same instance it came from keeps the key already there, which is the\n"
            "right one. Anywhere else, supply the key with KEY=.\n"
        )
        key_step = (
            "   make restore FROM=/path/to/this.zip\n"
            "       (onto the instance it came from: the key already there is kept)\n"
            "   make restore FROM=/path/to/this.zip KEY=/path/to/secret.key\n"
            "       (anywhere else)"
        )

    return f"""\
Spend Tracker backup: {name}
{"=" * (len(name) + 22)}

WHAT THIS IS

A complete copy of one Spend Tracker ledger, taken {described["taken_at"]}.
Spend Tracker is a self-hosted, multi-currency spend tracker for one
household: accounts, transactions, transfers, payees, categories and receipt
images. Source, licence (AGPL) and documentation:

    {repository}

This folder holds:

    spendtracker.sqlite3   the ledger itself, receipt images included
    manifest.json          when it was taken, its schema revision, row counts
                           and a SHA-256 of the database file
    README.txt             this file
{"    secret.key             the instance's encryption key (see below)" if described["secret_key"] else ""}
Schema revision {described["alembic_revision"]}. It held, when packaged:

{rows}

{key_part}

HOW TO OPEN IT

The ledger is an ordinary SQLite 3 database. Nothing about it is proprietary.

  - DB Browser for SQLite (https://sqlitebrowser.org), free on every
    platform: File > Open Database > spendtracker.sqlite3. Open it read-only
    if you only mean to look.
  - The sqlite3 command-line tool:
        sqlite3 spendtracker.sqlite3 ".tables"
        sqlite3 -header spendtracker.sqlite3 "SELECT * FROM accounts"

Reading the tables:

  - Money is stored as whole numbers of the currency's smallest unit, never
    as decimals. An amount of -1250 with currency EUR is -12.50 EUR.
  - Every row that changed carries its history in `changes`, grouped by
    `batches`: who did what, and when, with the values before and after.
  - Receipt images are in `receipt_blobs`, as the bytes of the image file.

To check the file is the one that was packaged, compare its SHA-256 with
database_sha256 in manifest.json:

    shasum -a 256 spendtracker.sqlite3      (macOS, Linux)
    certutil -hashfile spendtracker.sqlite3 SHA256      (Windows)


HOW TO RESTORE IT

A restore puts this ledger back as a running Spend Tracker. It replaces the
ledger that is there; the one it replaces is renamed aside, not deleted.

1. Get the code. It was packaged by version {described["packaged_by_version"]}, at schema
   revision {described["alembic_revision"]}; the newest code can read it:

       git clone {repository}
       cd household-spend-tracker
       make install-py

   To run exactly the code it was taken with instead, check out the commit
   that added that revision:

       git log --format=%h -1 -- 'migrations/versions/{described["alembic_revision"]}_*'

2. Stop the running service, if there is one. The restore refuses while
   its port answers.

3. Restore, straight from this zip -- no need to unzip it:

{key_step}

   It checks the database before touching anything, checks the key can open
   an authenticator secret in it, and asks you to type 'restore'.

4. Bring the ledger up to the code, start it, and sign in:

       make migrate
       make serve

   The server refuses to start on a ledger older than its code until
   `make migrate` has run, and says so.

In Docker, the same script runs inside the container; see
deploy/DOCKER.md and deploy/UPGRADING.md in the repository.
"""


def build(
    backup: Path,
    *,
    into: Path,
    version: str,
    repository: str,
    secret_key: str | None,
) -> Bundle:
    """Write the zip for `backup` into the directory `into`, private from birth.

    `secret_key` is the key's text when the person asked for it and None when
    they did not. The text rather than a path: an instance configured with
    `SPENDTRACKER_SECRET_KEY` has no file, and the key it runs with is the one
    that belongs in the zip.

    Deflated, because SQLite pages compress well, and ZIP64, because a ledger
    with receipts has already been measured at 725 MiB.
    """
    name = folder_name(backup)
    described = manifest(backup, version=version, with_key=secret_key is not None)
    root = PurePosixPath(name)

    # mkstemp would do, but the name should say what it is to anybody who
    # finds one left behind by a crash: `sweep_leftovers` removes these.
    fd, raw = _private_temp(into, prefix=f".download-{name}-", suffix=".zip")
    target = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle, zipfile.ZipFile(
            handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
        ) as archive:
            archive.writestr(
                str(root / README),
                readme(name=name, described=described, repository=_repo(repository)),
            )
            archive.writestr(str(root / MANIFEST), json.dumps(described, indent=2) + "\n")
            archive.write(backup, str(root / DATABASE))
            if secret_key is not None:
                archive.writestr(str(root / KEY), secret_key.strip() + "\n")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return Bundle(path=target, filename=f"{name}.zip", with_key=secret_key is not None)


def _repo(url: str) -> str:
    return url.rstrip("/")


def _private_temp(into: Path, *, prefix: str, suffix: str) -> tuple[int, str]:
    import tempfile

    # mkstemp opens 0600 from the start, which is the property that matters.
    return tempfile.mkstemp(dir=into, prefix=prefix, suffix=suffix)


#: A zip half-written when the process died, or one whose response never
#: finished. An hour is longer than any download of a ledger should take.
LEFTOVER_SECONDS = 3600


def sweep_leftovers(directory: Path, *, now: float | None = None) -> int:
    """Remove download zips a crash left behind. Returns how many went.

    Normally the response removes its own zip once it has been sent. This is
    for the times it did not get to -- and a full copy of the ledger lying in
    a directory, forgotten, is not a thing to leave to chance.
    """
    import time

    if not directory.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - LEFTOVER_SECONDS
    gone = 0
    for path in directory.glob(".download-*.zip"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                gone += 1
        except FileNotFoundError:  # pragma: no cover - removed while we looked
            pass
    return gone


# --------------------------------------------------------------------------- #
# Reading one back
# --------------------------------------------------------------------------- #


def unpack(archive: Path, into: Path) -> Path:
    """Extract a backup zip's known members into `into`, flat. Returns `into`.

    Only the four names in `MEMBERS`, matched on their last path component,
    each at most once. Nothing is extracted by the name the archive gives it,
    so there is no path for a hostile entry to climb out of.
    """
    try:
        opened = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as broken:
        raise BundleRefused(f"{archive} is not a zip file ({broken})") from broken
    with opened:
        seen: set[str] = set()
        for entry in opened.infolist():
            if entry.is_dir():
                continue
            base = PurePosixPath(entry.filename).name
            if base not in MEMBERS:
                continue
            if base in seen:
                raise BundleRefused(f"{archive} holds two files called {base}")
            seen.add(base)
            ceiling = MAX_DATABASE_BYTES if base == DATABASE else MAX_SMALL_MEMBER_BYTES
            if entry.file_size > ceiling:
                raise BundleRefused(
                    f"{archive} says its {base} is {entry.file_size:,} bytes, more than a "
                    f"backup's {base} can be ({ceiling:,})"
                )
            target = into / base
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as out, opened.open(entry) as source:
                    written = 0
                    # The claim above can lie; what is inflated cannot.
                    for chunk in iter(lambda: source.read(1 << 20), b""):
                        written += len(chunk)
                        if written > ceiling:
                            raise BundleRefused(
                                f"{archive}'s {base} inflates past {ceiling:,} bytes, more "
                                f"than a backup's {base} can be"
                            )
                        out.write(chunk)
            except (zipfile.BadZipFile, zlib.error, EOFError) as broken:
                # zipfile itself stops at the size the entry claims and then
                # fails its CRC; that is a damaged backup, not a crash.
                target.unlink(missing_ok=True)
                raise BundleRefused(f"{archive}'s {base} is damaged and cannot be unpacked") from broken
            except BaseException:
                target.unlink(missing_ok=True)
                raise
    if DATABASE not in seen:
        raise BundleRefused(f"{archive} has no {DATABASE} in it, so it is not a backup")
    return into
