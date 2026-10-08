"""A bench-sized ledger, and the timings taken on it (#104).

    python -m scripts.bench_ledger grow PATH [--target-mib 100]
    python -m scripts.bench_ledger fsync DIR [--commits 500]

**grow** takes a database the demo seed made and doubles its `transactions`
-- each copy shifted two years back, with fresh ids -- until the file reaches
the target size, then VACUUMs and ANALYZEs it. The seed builds a realistic
shape through the real services; this only makes it long. It reads the
columns from the table rather than naming them, so it grows a database made
by any earlier release, which is what the upgrade rehearsal needs: the bench
is built with the *previous* code and upgraded with this one.

Raw SQL on purpose, and on a throwaway copy only: the audit log of a bench is
not a record of anything anybody did. It refuses a database that has users
other than the demo's, so it cannot be pointed at a real ledger by mistake.

**fsync** times commits on the disk under DIR, one row each, under
`synchronous=NORMAL` (what the app runs) and `FULL`. Under WAL, NORMAL syncs
at checkpoints rather than every commit; the gap between the two lines is
what a power cut is traded for, on that disk.

Both print one JSON object, for the bench workflow's summary and for pasting
into an issue.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import tempfile
import time

MIB = 1024 * 1024

#: Columns a copied row must not keep: ids that point at another single row
#: (a transfer's other leg, a split's parent, a repaying payment) and keys
#: that must be unique. A copy is a plain row of its own.
_CLEARED = {
    "transfer_transaction_id": None,
    "transfer_account_id": None,
    "split_id": None,
    "reimbursed_by_id": None,
    "reimbursement": None,
    "import_alt_ids": None,
}
_DEMO_EMAIL = "demo@example.com"


def _refuse_a_real_ledger(conn: sqlite3.Connection) -> None:
    emails = [r[0] for r in conn.execute("SELECT email_canonical FROM users")]
    others = [e for e in emails if (e or "").lower() != _DEMO_EMAIL]
    if others:
        raise SystemExit(
            f"this database has {len(others)} user(s) other than the demo's; "
            "grow only a copy the demo seed made"
        )


def grow(path: pathlib.Path, target_mib: int = 100) -> dict:
    conn = sqlite3.connect(path)
    try:
        _refuse_a_real_ledger(conn)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(transactions)")]
        rounds = 0
        started = time.perf_counter()
        while path.stat().st_size < target_mib * MIB and rounds < 16:
            rounds += 1
            chosen = []
            for name in columns:
                if name == "id":
                    chosen.append("lower(hex(randomblob(16)))")
                elif name == "date":
                    chosen.append(f"date(date, '-{rounds * 731} days')")
                elif name == "import_id":
                    chosen.append(
                        "CASE WHEN import_id IS NULL THEN NULL "
                        "ELSE lower(hex(randomblob(12))) END"
                    )
                elif name in ("created_at", "updated_at"):
                    chosen.append(name)
                elif name in _CLEARED:
                    chosen.append("NULL")
                else:
                    chosen.append(name)
            listed = ", ".join(columns)
            conn.execute(
                f"INSERT INTO transactions ({listed}) "
                f"SELECT {', '.join(chosen)} FROM transactions"
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
        conn.commit()
        rows = conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
    finally:
        conn.close()
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "mib": round(path.stat().st_size / MIB, 1),
        "transactions": rows,
        "doublings": rounds,
        "seconds": round(time.perf_counter() - started, 1),
    }


def fsync(directory: pathlib.Path, commits: int = 500) -> dict:
    out: dict[str, object] = {"directory": str(directory), "commits": commits}
    with tempfile.TemporaryDirectory(dir=directory, prefix="tmp-fsync-") as scratch:
        for mode in ("NORMAL", "FULL"):
            db = pathlib.Path(scratch) / f"{mode.lower()}.sqlite3"
            conn = sqlite3.connect(db, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"PRAGMA synchronous={mode}")
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
            started = time.perf_counter()
            for n in range(commits):
                conn.execute("BEGIN")
                conn.execute("INSERT INTO t (body) VALUES (?)", (f"row {n}",))
                conn.execute("COMMIT")
            elapsed = time.perf_counter() - started
            conn.close()
            out[mode.lower()] = {
                "ms_per_commit": round(elapsed / commits * 1000, 3),
                "commits_per_second": round(commits / elapsed),
            }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    g = sub.add_parser("grow", help="make a demo database bench-sized")
    g.add_argument("path", type=pathlib.Path)
    g.add_argument("--target-mib", type=int, default=100)
    f = sub.add_parser("fsync", help="time commits on the disk under a directory")
    f.add_argument("directory", type=pathlib.Path)
    f.add_argument("--commits", type=int, default=500)
    args = parser.parse_args(argv)
    if args.command == "grow":
        result = grow(args.path, args.target_mib)
    else:
        result = fsync(args.directory, args.commits)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
