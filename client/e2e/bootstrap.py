"""Build a throwaway instance for the end-to-end run, and say how to sign in.

Its own database in its own directory, reset every run: an end-to-end suite
that shares the developer's ledger either destroys it or fails depending on
what happens to be in it, and both are worse than slow.

The credentials go to a JSON file rather than being hardcoded in the spec,
because the seed mints a new TOTP secret every time. The spec computes codes
from that secret the way an authenticator would.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import re
import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent


def main() -> int:
    from scripts import seed_demo

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = seed_demo.main(["--reset", "--months", "3"])
    output = captured.getvalue()
    sys.stderr.write(output)
    if code != 0:
        return code

    secret = re.search(r"authenticator secret\s*:\s*(\S+)", output)
    if secret is None:
        sys.stderr.write("could not read the authenticator secret out of the seed\n")
        return 1

    (HERE / ".credentials.json").write_text(
        json.dumps(
            {
                "email": seed_demo.DEMO_EMAIL,
                "password": seed_demo.DEMO_PASSWORD,
                "totp_secret": secret.group(1),
            },
            indent=2,
        )
    )
    _update_backups()
    return 0


#: Seven update backups: the newest five are kept, the two older deletable.
UPDATE_BACKUPS = 7


def _update_backups() -> None:
    """The folders seven past updates would have left, for the Updates e2e (#166).

    An update backup is a `backups/<stamp>/` folder that a history record in
    the updater's volume names (design notes 8.7). Nothing here runs an
    updater: the records are written as the updater writes them, each already
    dismissed so the section does not open on an outcome. The copy in each
    folder is one `VACUUM INTO` of the seeded ledger, never a file copy of it.
    """
    from app import __version__, config
    from app.services import platform as platform_service

    settings = config.settings
    backups = platform_service.backup_dir()
    history = settings.update_dir / "history"
    backups.mkdir(parents=True, exist_ok=True)
    history.mkdir(parents=True, exist_ok=True)

    source = platform_service.database_path()
    copy = backups / ".e2e-copy.sqlite3"
    with contextlib.closing(sqlite3.connect(source)) as db:
        db.execute("VACUUM INTO ?", (str(copy),))
    revision = "2de003489b79"
    with contextlib.closing(sqlite3.connect(copy)) as db:
        row = db.execute("SELECT version_num FROM alembic_version").fetchone()
        revision = row[0] if row else revision
    data = copy.read_bytes()
    copy.unlink()

    seen: list[str] = []
    start = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)
    major, minor, _ = (int(part) for part in __version__.split("."))
    for at in range(UPDATE_BACKUPS):
        when = start + timedelta(days=4 * at)
        stamp = when.strftime("%Y%m%d-%H%M%S")
        folder = backups / stamp
        folder.mkdir()
        (folder / "spendtracker.sqlite3").write_bytes(data)
        # The releases before this one, oldest first, and the newest is the
        # running version exactly -- a patch release included.
        last = at == UPDATE_BACKUPS - 1
        version = __version__ if last else f"{major}.{max(minor - UPDATE_BACKUPS + 1 + at, 0)}.0"
        (folder / "manifest.json").write_text(
            json.dumps(
                {"taken_at": when.isoformat(), "app_version": version, "alembic_revision": revision},
                indent=2,
            )
        )
        record = str(uuid.uuid4())
        (history / f"{record}.json").write_text(
            json.dumps(
                {
                    "protocol": 1,
                    "id": record,
                    "kind": "apply",
                    "state": "succeeded",
                    "sentence": f"Updated to {version}.",
                    "finished_at": (when + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "started_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "backup": f"backups/{stamp}",
                    "duration_s": 160.0,
                    "gap_s": 0.0,
                    "log_tail": [],
                }
            )
        )
        seen.append(record)
    (settings.data_dir / "update-outcomes-seen.json").write_text(json.dumps({"seen": sorted(seen)}))


if __name__ == "__main__":
    raise SystemExit(main())
