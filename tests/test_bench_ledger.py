"""The bench ledger grower and the fsync probe behind `bench.yml` (#104)."""

from __future__ import annotations

import sqlite3

import pytest

from scripts import bench_ledger


def _tiny(path, emails=("demo@example.com",)):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id TEXT, email_canonical TEXT)")
    conn.executemany("INSERT INTO users VALUES (?, ?)", [(e, e) for e in emails])
    conn.execute(
        "CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, date DATE, "
        "amount INTEGER, memo TEXT, import_id TEXT UNIQUE, transfer_transaction_id TEXT, "
        "created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("t1", "eur", "2026-01-05", -1_250, "x" * 400, "bank:1", "t2", "2026-01-05"),
            ("t2", "gbp", "2026-01-05", 1_100, "y" * 400, None, "t1", "2026-01-05"),
        ],
    )
    conn.commit()
    conn.close()


def test_grow_doubles_the_rows_with_fresh_ids_and_older_dates(tmp_path):
    path = tmp_path / "bench.sqlite3"
    _tiny(path)
    result = bench_ledger.grow(path, target_mib=1)
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT id, date, amount, import_id, transfer_transaction_id FROM transactions").fetchall()
    assert result["transactions"] == len(rows) == 2 * 2 ** result["doublings"]
    assert len({r[0] for r in rows}) == len(rows), "every copy has its own id"
    keys = [r[3] for r in rows if r[3] is not None]
    assert len(keys) == len(set(keys)) == len(rows) // 2
    assert min(r[1] for r in rows) < "2024-01-05", "copies go back in time"
    copies = [r for r in rows if r[0] not in ("t1", "t2")]
    assert all(r[4] is None for r in copies), "a copy is nobody's transfer leg"
    # Both currencies' amounts survive in equal measure.
    assert sum(r[2] for r in rows) == (-1_250 + 1_100) * len(rows) // 2


def test_grow_refuses_a_ledger_with_real_users(tmp_path):
    path = tmp_path / "real.sqlite3"
    _tiny(path, emails=("demo@example.com", "someone@example.org"))
    with pytest.raises(SystemExit, match="other than the demo"):
        bench_ledger.grow(path, target_mib=1)
    assert sqlite3.connect(path).execute("SELECT count(*) FROM transactions").fetchone() == (2,)


def test_fsync_times_both_modes(tmp_path):
    result = bench_ledger.fsync(tmp_path, commits=5)
    for mode in ("normal", "full"):
        assert result[mode]["commits_per_second"] > 0
    assert list(tmp_path.iterdir()) == [], "the scratch databases are removed"
