"""Is this instance healthy, and which ledger is it? A report that changes nothing.

    python -m scripts.doctor
    make doctor
    docker compose run --rm -T --entrypoint python app -m scripts.doctor

One line per check, each `ok`, `WARN` or `FAIL`, and an exit status of 1 when
anything failed, so a timer or a monitoring probe can run it as it is.

What it looks at:

- **The data directory**: where it resolved, and whether anyone but this
  account can read it.
- **The database**: there, its size and its WAL's, and `PRAGMA quick_check`.
- **The schema**: the revision stamped in the ledger against the one this
  code ends at. Behind or ahead, the app refuses to boot and says so; this
  says it without booting.
- **Which ledger**: the households by name, how many members, when the first
  signed up. Every checkout and every image opens whatever ledger the data
  directory holds, which is right and is also the surprise in
  `deploy/TROUBLESHOOTING.md`.
- **`secret.key`**: whether it opens every enrolled member's authenticator
  (a disabled member's is left out: they cannot sign in to be asked), and
  whether it is newer than the ledger's first member -- the mark a key left
  when the old one went missing and a new one was made in its place.
- **Recovery codes**: how many each member has left. None left is one lost
  phone away from `scripts.reset_authenticator`.
- **Backups**: the newest in the data directory's `backups/` and in
  `./backups`, and how old it is.

It opens the database read-only, so it is safe beside a running instance.
Run it from the checkout (or in the container) so it resolves the same data
directory the app does.

One thing it cannot help: reading the app's settings creates `secret.key`
when there is none, exactly as the app does at boot. So a missing key is
found by what it fails to open, not by its absence -- which is also the only
way to find one the app already replaced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sqlite3
import sys

#: Older than this and the newest backup is worth a warning. A week is what
#: `--keep 3` on a nightly timer comfortably beats.
STALE_BACKUP_DAYS = 7


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def ok(self, what: str, detail: str) -> None:
        print(f"  ok    {what:<16}{detail}")

    def warn(self, what: str, detail: str) -> None:
        self.warned += 1
        print(f"  WARN  {what:<16}{detail}")

    def fail(self, what: str, detail: str) -> None:
        self.failed += 1
        print(f"  FAIL  {what:<16}{detail}")


def _size(path: pathlib.Path) -> str:
    size = path.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""  # pragma: no cover - the loop always returns


def _newest_backup(places: list[pathlib.Path]) -> pathlib.Path | None:
    found = [
        entry
        for place in places
        if place.is_dir()
        for entry in place.iterdir()
        if entry.name.startswith(("2", "spendtracker-")) and not entry.name.endswith(".tmp")
    ]
    return max(found, key=lambda entry: entry.stat().st_mtime, default=None)


def run(say: Report, *, backups: list[pathlib.Path]) -> None:
    from app import schema_check
    from app.auth import keycheck
    from app.config import settings
    from app.permissions import not_private
    from app.services import backup_bundle

    data = settings.data_dir
    exposed = not_private(data)
    if exposed:
        say.warn("data directory", f"{data}: {len(exposed)} path(s) readable by other users")
    else:
        say.ok("data directory", f"{data}, private to this account")

    if not settings.database_url.startswith("sqlite:///"):
        say.warn("database", "not SQLite; nothing below applies")
        return
    db = pathlib.Path(settings.database_url.split("///", 1)[-1]).resolve()
    if not db.exists():
        say.fail("database", f"{db} is not there: no ledger has been created here yet")
        return
    wal = db.with_name(db.name + "-wal")
    say.ok("database", f"{db.name}, {_size(db)}" + (f", WAL {_size(wal)}" if wal.exists() else ""))

    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
        if verdict == "ok":
            say.ok("integrity", "PRAGMA quick_check: ok")
        else:
            say.fail("integrity", f"PRAGMA quick_check: {verdict}")

        at, head = backup_bundle.revision(db), schema_check.expected_head()
        if at is None:
            say.fail("schema", "never migrated. First start: SPENDTRACKER_AUTO_MIGRATE=1, or make migrate")
        elif at == head:
            say.ok("schema", f"{at}, this code's head")
        elif at in schema_check.known_revisions():
            say.fail("schema", f"{at}, behind this code's {head}. Upgrade deliberately: deploy/UPGRADING.md")
        else:
            say.fail("schema", f"{at}, which this code does not know: the ledger is newer than the code")

        try:
            houses = [row[0] for row in conn.execute("SELECT name FROM households ORDER BY name")]
            members = conn.execute(
                "SELECT count(*), min(created_at), sum(disabled_at IS NOT NULL) FROM users"
            ).fetchone()
            left = conn.execute(
                "SELECT u.email, count(r.id) FROM users u LEFT JOIN recovery_codes r"
                " ON r.user_id = u.id AND r.used_at IS NULL"
                " WHERE u.disabled_at IS NULL GROUP BY u.id ORDER BY u.created_at"
            ).fetchall()
        except sqlite3.OperationalError as missing:
            say.fail("ledger", f"cannot be read: {missing}")
            return

    count, since, disabled = members[0], members[1], members[2] or 0
    if count == 0:
        say.ok("ledger", "empty: setup has not been finished, so the app is showing /setup")
    else:
        names = ", ".join(houses) if houses else "no households yet"
        extra = f" ({disabled} disabled)" if disabled else ""
        say.ok("ledger", f"{len(houses)} household(s): {names}; {count} member(s){extra} since {since[:10]}")

    key = data / "secret.key"
    checked = keycheck.check(db, settings.secret_key)
    if checked.enrolled == 0:
        say.ok("secret.key", "nobody has enrolled an authenticator yet, so any key will do")
    elif checked.refused:
        say.fail(
            "secret.key",
            f"opens {checked.opened} of {checked.enrolled} authenticators; refused for "
            f"{', '.join(checked.refused)}. deploy/TROUBLESHOOTING.md, 'secret.key is lost or wrong'",
        )
    else:
        say.ok("secret.key", f"opens all {checked.enrolled} authenticator(s)")
    if key.exists() and since and checked.refused:
        # Both in UTC: the ledger stores naive UTC, the filesystem a timestamp.
        made = dt.datetime.fromtimestamp(key.stat().st_mtime, dt.UTC).replace(tzinfo=None)
        if made > dt.datetime.fromisoformat(since):
            say.warn("secret.key", f"was written {made:%Y-%m-%d %H:%M}, after the first member: probably recreated")

    for email, unused in left:
        if unused == 0:
            say.warn("recovery codes", f"{email} has none left; scripts.reset_authenticator if they lose their device")
    if left and all(unused for _, unused in left):
        say.ok("recovery codes", ", ".join(f"{email} {unused}" for email, unused in left))

    places = [data / "backups", *backups]
    newest = _newest_backup(places)
    if newest is None:
        say.warn("backups", "none found in " + ", ".join(str(place) for place in places))
    else:
        age = dt.datetime.now() - dt.datetime.fromtimestamp(newest.stat().st_mtime)
        line = f"newest {newest}, {age.days} day(s) old"
        if age.days > STALE_BACKUP_DAYS:
            say.warn("backups", line)
        else:
            say.ok("backups", line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--backups",
        type=pathlib.Path,
        action="append",
        default=None,
        metavar="DIR",
        help="also look for backups here (default: ./backups); repeatable",
    )
    args = parser.parse_args(argv)
    from app import __version__

    print(f"Spend Tracker {__version__}, {dt.datetime.now().astimezone().isoformat(timespec='seconds')}")
    say = Report()
    run(say, backups=args.backups or [pathlib.Path("backups").resolve()])
    print()
    if say.failed:
        print(f"{say.failed} check(s) failed. deploy/TROUBLESHOOTING.md has what each means.")
        return 1
    print("Nothing failed." + (f" {say.warned} warning(s)." if say.warned else ""))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
