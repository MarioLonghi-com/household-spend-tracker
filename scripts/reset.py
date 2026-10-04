"""Start again with an empty ledger, keeping the old one. On purpose, once.

    make reset
    python -m scripts.reset --into /mnt/offsite
    docker compose run --rm -T -v "$PWD/backups:/backups" \\
      --entrypoint python app -m scripts.reset --into /backups

A fresh clone, a rebuilt image or a new directory opens whatever ledger the
data directory already holds. That is deliberate -- the alternative looks
exactly like data loss -- and it means "start again" has to be an act of its
own rather than a side effect of deleting the code. This is that act:

1. Says which ledger is there: revision, households, members, row counts.
2. Takes a backup and reads it back (`scripts.backup`). If that fails,
   nothing else happens.
3. Asks you to type `reset`.
4. Moves the database, its `-wal` and `-shm`, and `secret.key` aside in the
   data directory, renamed `.before-reset-<stamp>`. Moved, not deleted.
5. Says how to start: the next start creates an empty ledger and a new key,
   and opens on the setup screen.

It refuses while the app answers on the port. Inside a one-off container that
check sees only its own network and always finds the port free, so stop the
app yourself first: `docker compose stop app`.

To undo: restore the backup it names (`scripts.restore`), or rename the
`.before-reset-` files back while the app is stopped.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

SIDECARS = ("-wal", "-shm")


def main(argv: list[str] | None = None, *, ask=input) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--into", type=pathlib.Path, help="where the backup goes (default: as scripts.backup)"
    )
    parser.add_argument("--port", type=int, default=8848, help="the port the app serves on")
    parser.add_argument("--yes", action="store_true", help="do not ask before moving the ledger")
    args = parser.parse_args(argv)

    from app.config import settings
    from app.services import backup_bundle
    from scripts import backup as backup_script
    from scripts import in_a_container
    from scripts.upgrade import port_is_busy

    if not settings.database_url.startswith("sqlite:///"):
        raise SystemExit("DATABASE_URL is not SQLite; reset a Postgres deployment with its own tools.")
    live = pathlib.Path(settings.database_url.split("///", 1)[-1]).resolve()
    key = settings.data_dir / "secret.key"
    if not live.exists():
        print(f"There is no ledger at {live}. Nothing to reset.")
        return 0
    if port_is_busy(args.port) and not in_a_container():
        raise SystemExit(
            f"Refusing: something answers on port {args.port}, probably the app. Stop it "
            "first; moving a database out from under a running server is how a WAL and "
            "its database start disagreeing."
        )

    rows = backup_bundle.counts(live)
    print(f"The ledger at {live}")
    print(f"  revision   {backup_bundle.revision(live)}")
    for table, count in sorted(rows.items()):
        print(f"  {count:>9,}  {table}")
    print()

    folder = backup_script.take(args.into)
    print(f"Backed up and read back: {folder}")
    if folder.is_relative_to(settings.data_dir):
        print("  That is inside the data directory. Copy it somewhere else before you")
        print("  rely on it: a backup on the same volume does not survive the volume.")
    print()

    if not args.yes and ask("Type 'reset' to move this ledger aside and start empty: ").strip() != "reset":
        print("Nothing was moved. The backup above is kept.")
        return 1

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for path in (live, *(live.with_name(live.name + tail) for tail in SIDECARS), key):
        if path.exists():
            aside = path.with_name(f"{path.name}.before-reset-{stamp}")
            path.rename(aside)
            print(f"  moved aside  {aside.name}")

    print()
    print("Done. The next start creates an empty ledger and a new secret.key.")
    if in_a_container():
        print("  Start it:   SPENDTRACKER_AUTO_MIGRATE=1 docker compose up -d")
        print("  Token:      docker compose logs app | grep -A2 \"one-time token\"")
    else:
        print("  Start it:   make migrate && make serve")
    print(f"  Undo:       restore {folder}, or rename the .before-reset-{stamp} files back")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
