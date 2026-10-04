"""Take a backup of a deployment, and prove it is one.

**Never copy the database file.** SQLite is in WAL mode, so recent writes live
in `spendtracker.sqlite3-wal` until a checkpoint folds them in. In the previous
build the main file was 4 KB while the `-wal` file held 1.7 MB -- copying the
one file would have produced an empty backup that nobody discovered was empty
until they needed it. `VACUUM INTO` takes a consistent read of the live
database *including* the WAL, which also means it can run **before** the
service is stopped: a failure to take the backup is then not also an outage.

`secret.key` travels with it, and that is not optional. It encrypts every TOTP
secret; recovery codes are hashes and are not encrypted with it. A backup
without it restores a ledger whose authenticators all refuse, and every member
has to re-enrol one, through a recovery code or `scripts.reset_authenticator`.

**A backup that was never reopened is a belief.** So this refuses to report
success until it has opened the copy, read its `alembic_version`, counted the
rows in a handful of tables and compared them against the live database. The
counts go in `manifest.json` beside the copy, along with the revision and the
app version -- which is what tells a future operator which `downgrade` target
matches this file.

    python -m scripts.backup                    # into ./backups/<stamp>/
    python -m scripts.backup --keep 3           # and prune to the newest three
    python -m scripts.backup --into /mnt/nas    # somewhere else
    python -m scripts.backup --method backup    # for an off-site copy: see below
    python -m scripts.backup --verify PATH      # check one that already exists

## Two methods, and the measurement that decides between them

From the project's hosting notes, measured against a real 725 MiB database
with two snapshots a simulated week apart -- 2.5 MiB of genuinely new data:

| method | blocks reused | new bytes for week two |
|---|---:|---:|
| `VACUUM INTO` | 8.1% | **666 MiB** |
| `.backup` (the online backup API) | **98.5%** | **11.2 MiB** |

**`VACUUM INTO` repacks the database.** Page contents shift, so almost nothing
lands where it did last week, and `restic`, `borg`, Time Machine and every
incremental cloud sync store the whole thing again -- 2.5 MiB of new receipts
costing 666 MiB of transfer. A 266x amplification.

That is not a reason to abandon it. It is compact, self-describing and carries
its own `alembic_version`, which is exactly what the local rolling three wants,
and it is the default here for that reason. It *is* the reason `--method
backup` exists: the online backup API takes an equally consistent copy and
**preserves page numbering**, so put that underneath anything that dedupes.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sqlite3
import sys

from app.permissions import PRIVATE_DIR, copy_private, private_dir

# The tables counted on both sides of a copy, and the checks that read one back.
# Shared with the screen's download (`app/services/backup_bundle.py`), so a zip
# made there and a folder made here are verified by the same code.
from app.services.backup_bundle import COUNTED  # noqa: F401 - re-exported
from app.services.backup_bundle import counts as _counts
from app.services.backup_bundle import digest as _digest
from app.services.backup_bundle import integrity as _integrity
from app.services.backup_bundle import revision as _revision


def _settings():
    # `app.config` sets the private umask as it loads, so from here on the
    # `VACUUM INTO` copy and the manifest are created 0600 as well.
    from app import __version__
    from app.config import settings

    return settings, __version__


def _sqlite_path(database_url: str) -> pathlib.Path | None:
    """The file behind a SQLite URL, or None for anything else.

    Postgres is not refused here for the sake of it: `VACUUM INTO` is a SQLite
    statement and `pg_dump` is a different program with different flags, and
    pretending one script covers both is how somebody ends up with a file that
    is not a backup of anything.
    """
    if not database_url.startswith("sqlite:"):
        return None
    tail = database_url.split("///", 1)[-1]
    return pathlib.Path(tail).resolve()


def verify(
    folder: pathlib.Path,
    *,
    against: dict[str, int] | None = None,
    require_key: bool = True,
) -> dict:
    """Open the copy and read it. Raises `SystemExit` with a sentence if it cannot.

    `against` is the live database's counts, when this is called right after a
    copy. Without it -- `--verify` on an old backup -- the counts recorded in
    the manifest are what the file is checked against instead, which catches a
    file that has been truncated or partly overwritten since.

    `require_key=False` is for `restore` alone, which has a key of its own to
    offer when the backup carries none (a zip downloaded without one) and
    checks whichever key it settles on against the database itself.
    """
    copy = folder / "spendtracker.sqlite3"
    if not copy.exists():
        raise SystemExit(f"{copy} is not there, so there is nothing to verify")

    verdict = _integrity(copy)
    if verdict != "ok":
        raise SystemExit(f"{copy} fails PRAGMA integrity_check: {verdict}")

    revision = _revision(copy)
    if revision is None:
        raise SystemExit(
            f"{copy} has no alembic_version table. It is not a Spend Tracker database, "
            "or the copy did not finish."
        )

    counts = _counts(copy)
    if not counts:
        raise SystemExit(f"{copy} opened but holds none of the tables this app uses")

    expected = against
    manifest_path = folder / "manifest.json"
    if expected is None and manifest_path.exists():
        expected = json.loads(manifest_path.read_text()).get("rows") or None

    if expected is not None:
        wrong = {
            table: (expected.get(table), counts.get(table))
            for table in set(expected) | set(counts)
            if expected.get(table) != counts.get(table)
        }
        if wrong:
            lines = "\n".join(
                f"  {table}: expected {was}, the copy has {now}"
                for table, (was, now) in sorted(wrong.items())
            )
            raise SystemExit(f"the copy does not match what it was taken from:\n{lines}")

    if require_key and not (folder / "secret.key").exists():
        raise SystemExit(
            f"{folder} has no secret.key. The ledger would restore and nobody could "
            "pass the authenticator step -- every TOTP secret is encrypted with that "
            "file, so every member would have to re-enrol, through a recovery code "
            "(those are not encrypted with it) or scripts.reset_authenticator."
        )
    return {"revision": revision, "rows": counts}


def prune(root: pathlib.Path, keep: int) -> list[pathlib.Path]:
    """Keep the newest `keep` backups under `root` and remove the rest.

    **After** the new one has been verified, never before. A script that prunes
    first and then discovers its fresh copy is unreadable has turned one bad
    backup into no backups, which is the mistake most rolling-backup scripts
    make and the one worth not making.
    """
    folders = sorted(
        (one for one in root.iterdir() if one.is_dir() and (one / "manifest.json").exists()),
        key=lambda one: one.name,
    )
    removed = []
    for old in folders[:-keep] if keep > 0 else []:
        shutil.rmtree(old)
        removed.append(old)
    return removed


def _default_backup_root() -> pathlib.Path:
    """`./backups`, or inside the data directory when the tree is read-only.

    In a checkout `./backups` is right: beside the code, gitignored, easy to
    find. **In the container it is not writable at all** -- `compose.yaml` sets
    `read_only: true` on the root filesystem, which is deliberate and worth
    keeping, and the working directory is `/app`.

    Without this, `docker compose exec app python -m scripts.backup` dies with
    a `PermissionError` on `mkdir`, and the reason is nowhere in the traceback.
    Falling back to the data directory is not the *best* place for a backup --
    it is the same volume as the thing being backed up, which protects against
    a bad migration and not against losing the volume -- so it says so, and the
    documented container flow bind-mounts somewhere else with `--into`.
    """
    from app.config import settings
    from scripts import in_a_container

    if in_a_container():
        # **Never the working directory in a container**, even when it is
        # writable. `docker run --rm` and `docker compose run --rm` both throw
        # the container's filesystem away when the command ends, so a backup
        # written to `/app/backups` is reported as a success and is gone a
        # second later. The volume is the only thing that outlives the process.
        #
        # It is still the same volume as the database, so it protects against a
        # bad migration and not against losing the volume. The documented flow
        # bind-mounts somewhere on the host and passes --into.
        inside = settings.data_dir / "backups"
        print(f"in a container, so this is going to {inside} -- which is in the volume.")
        print("  A backup written to the container's own filesystem would be thrown")
        print("  away with the container. For a copy that survives losing the volume,")
        print("  mount a host directory and pass --into.")
        return inside

    here = pathlib.Path("backups")
    try:
        here.mkdir(parents=True, exist_ok=True)
        probe = here / ".write-probe"
        probe.touch()
        probe.unlink()
        return here
    except OSError:
        pass

    fallback = settings.data_dir / "backups"
    print(
        f"the working directory is not writable, so this is going to "
        f"{fallback} instead.\n"
        "  That is the same volume as the database, which protects you from a bad "
        "migration and not\n"
        "  from losing the volume. For a copy that survives that, mount somewhere "
        "else and use --into.",
    )
    return fallback


def take(into: pathlib.Path | None = None, *, method: str = "vacuum") -> pathlib.Path:
    settings, version = _settings()
    live = _sqlite_path(settings.database_url)
    if live is None:
        raise SystemExit(
            "DATABASE_URL is not SQLite. This takes a `VACUUM INTO` copy, which is a "
            "SQLite statement; back a Postgres deployment up with pg_dump."
        )
    if not live.exists():
        raise SystemExit(f"{live} is not there. Is SPENDTRACKER_DATA_DIR pointing at it?")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    # Resolved, because this path is printed and then handed straight back to
    # `scripts.restore` -- and a relative one is only correct from the
    # directory it happened to be produced in.
    root = ((into / stamp) if into is not None else (_default_backup_root() / stamp)).resolve()
    # Never `exist_ok=True`: writing into a directory that already holds a
    # backup would half-overwrite it, and a half-overwritten backup is worse
    # than no backup because it still looks like one. Two runs in the same
    # second get a suffix instead of a collision.
    # The folder that holds every backup is private too, not only each one:
    # a listable `backups/` still says how often and how big. Only the default
    # one -- a directory named with --into is the operator's (a NAS mount, a
    # shared drive), and each backup made inside it is private regardless.
    if into is None:
        private_dir(root.parent)
    suffix = 0
    while True:
        try:
            root.mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=False)
            break
        except FileExistsError:
            suffix += 1
            root = root.with_name(f"{stamp}-{suffix}")
    root.chmod(PRIVATE_DIR)
    copy = root / "spendtracker.sqlite3"

    live_counts = _counts(live)
    # Both are consistent across the WAL and both can run while the service is
    # still up. They differ only in whether the copy's pages land where the
    # original's did -- see the table in this module's docstring.
    if method == "backup":
        with sqlite3.connect(live) as source, sqlite3.connect(copy) as target:
            source.backup(target)
    else:
        with sqlite3.connect(live) as conn:
            conn.execute("VACUUM INTO ?", (str(copy),))

    key = settings.data_dir / "secret.key"
    if key.exists():
        # Through a 0600 open, never `shutil.copy2`: that creates the file
        # under the umask and copies the mode across afterwards, so the key
        # existed for a moment with whatever the umask allowed.
        copy_private(key, root / "secret.key")

    checked = verify(root, against=live_counts)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "taken_at": dt.datetime.now().astimezone().isoformat(),
                "app_version": version,
                "alembic_revision": checked["revision"],
                "rows": checked["rows"],
                "database_sha256": _digest(copy),
                "method": method,
                "source": str(live),
                "secret_key": (root / "secret.key").exists(),
            },
            indent=2,
        )
        + "\n"
    )
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--into", type=pathlib.Path, help="parent directory for the copy")
    parser.add_argument(
        "--method", choices=("vacuum", "backup"), default="vacuum",
        help="vacuum (default, compact) or backup (preserves page numbering, for off-site)",
    )
    parser.add_argument(
        "--keep", type=int, default=0, metavar="N",
        help="after verifying, remove all but the newest N backups",
    )
    parser.add_argument(
        "--verify", type=pathlib.Path, metavar="PATH", help="check an existing backup instead"
    )
    args = parser.parse_args(argv)

    if args.verify:
        checked = verify(args.verify)
        print(f"{args.verify} is readable, at revision {checked['revision']}")
        for table, count in sorted(checked["rows"].items()):
            print(f"  {count:>9,}  {table}")
        return 0

    folder = take(args.into, method=args.method)
    manifest = json.loads((folder / "manifest.json").read_text())
    print(f"backed up to {folder}")
    print(f"  version    {manifest['app_version']}")
    print(f"  revision   {manifest['alembic_revision']}")
    print(f"  secret.key {'yes' if manifest['secret_key'] else 'MISSING'}")
    for table, count in sorted(manifest["rows"].items()):
        print(f"  {count:>9,}  {table}")
    print(
        "Verified: the copy passed PRAGMA integrity_check, its revision was read "
        "and its rows counted."
    )
    for gone in prune(folder.parent, args.keep):
        print(f"  pruned     {gone.name}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
