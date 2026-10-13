"""The upgrade rehearsal, for the packaged desktop backend (#61).

    python desktop/rehearse_backend.py <frozen backend> <data directory>

The server's `rehearsal` job asks whether `alembic upgrade head` kept the rows.
This asks the same of what a desktop user actually runs: a ledger made by the
previous release is opened by the *frozen* backend of this one, which backs it
up, migrates it in-process and counts it (`app/desktop/launch.py`) before it
says it is ready. Then this counts every table itself, before and after, and
fails on any that holds fewer rows -- `exit 0` from a migration says it ran,
not that the ledger survived it.

The data directory is used in place: make it with the previous release's code
first (CI does, from the last tag). A lossy migration is accepted, as the shell
does after its dialog; the backup the launch takes is checked to exist.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.desktop.stdio_adapter import read_message  # noqa: E402


def counts(database: pathlib.Path) -> dict[str, int]:
    with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        tables = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('alembic_version', 'sqlite_sequence')"
            )
        ]
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in sorted(tables)}


def stamp(database: pathlib.Path) -> str | None:
    with contextlib.closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def main(executable: str, data: str) -> int:
    executable = str(pathlib.Path(executable).resolve())
    data_dir = pathlib.Path(data).resolve()
    database = data_dir / "spendtracker.sqlite3"
    if not database.exists():
        raise SystemExit(f"no ledger at {database}: make one with the previous release first")
    before, stamp_before = counts(database), stamp(database)
    sys.stderr.write(f"before: {stamp_before}, {sum(before.values())} rows in {len(before)} tables\n")

    env = {k: v for k, v in os.environ.items() if not k.startswith("SPENDTRACKER_") and k != "DATABASE_URL"}
    env.update(
        SPENDTRACKER_PROFILE="desktop",
        SPENDTRACKER_DATA_DIR=str(data_dir),
        SPENDTRACKER_ACCEPT_LOSSY="1",
    )
    log = data_dir.parent / f"{data_dir.name}-rehearsal.log"
    with log.open("wb") as said:
        child = subprocess.Popen([executable], cwd="/", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=said, env=env)
        events = []
        while True:
            message = read_message(child.stdout)
            if message is None:
                break
            events.append(message[0])
            if message[0].get("event") in {"ready", "stopped"}:
                break
        child.stdin.close()
        code = child.wait(timeout=120)

    last = events[-1] if events else {}
    sys.stderr.write(f"backend said: {json.dumps([e.get('event') for e in events])}, exit {code}\n")
    if last.get("event") != "ready":
        sys.stderr.write(log.read_text(encoding="utf-8", errors="replace")[-3000:])
        raise SystemExit(f"the packaged backend did not open the ledger: {json.dumps(last)}")
    launch = last["launch"]
    if code != 0:
        raise SystemExit(f"the backend did not stop cleanly (exit {code})")

    after, stamp_after = counts(database), stamp(database)
    lost = [
        f"{table}: the table is gone; it held {was} rows"
        if table not in after
        else f"{table}: {was} rows before, {after[table]} after"
        for table, was in before.items()
        if table not in after or after[table] < was
    ]
    if lost:
        raise SystemExit("THE UPGRADE LOST DATA:\n  " + "\n  ".join(lost))
    if launch["migrated"] and not (launch.get("backup") and pathlib.Path(launch["backup"]).is_dir()):
        raise SystemExit("the ledger was migrated with no backup taken first")
    sys.stderr.write(
        f"after: {stamp_after}, {len(launch['migrated'])} migration(s), backup {launch.get('backup')}; "
        f"{len(before)} tables, every row still present\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
