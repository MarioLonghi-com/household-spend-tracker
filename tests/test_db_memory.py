"""SQLite's memory, sized from the host rather than promised to every connection (#102).

The pool is unbounded on purpose (see `app/db.py`), and each connection used
to get a 32 MiB page cache whatever the machine. Measured on a 95 MiB ledger,
ten connections reading the register and a report at once held 456 MiB above
the process's baseline -- most of a 1 GB VM. These hold the two bounds that
replace that: a process-wide soft heap limit of an eighth of the host's
memory, and a per-connection cache of a sixty-fourth, between 2 and 32 MiB.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event as sa_event

from app import db

GIB = 1 << 30
MIB = 1 << 20


@pytest.mark.parametrize(
    ("memory", "heap", "cache"),
    [
        # A 1 GB VM: 128 MiB for SQLite in all, 16 MiB a connection.
        (1 * GIB, 128 * MIB, 16 * MIB),
        # 512 MiB: 64 MiB in all, 8 MiB a connection.
        (512 * MIB, 64 * MIB, 8 * MIB),
        # A tiny container: never below SQLite's own 2 MiB.
        (64 * MIB, 8 * MIB, 2 * MIB),
        # A large host: the cache stops at the 32 MiB every connection used to get.
        (16 * GIB, 2 * GIB, 32 * MIB),
    ],
)
def test_the_budget_follows_the_host(memory, heap, cache):
    assert db.memory_budget(memory) == (heap, cache)


def test_an_unknown_host_keeps_the_old_cache_and_no_heap_limit(monkeypatch):
    monkeypatch.setattr(db, "host_memory", lambda: None)
    assert db.memory_budget() == (0, 32 * MIB)


def test_a_containers_limit_wins_over_the_machines(monkeypatch, tmp_path):
    v2 = tmp_path / "memory.max"
    v2.write_text(f"{768 * MIB}\n")
    monkeypatch.setattr(db, "CGROUP_LIMITS", (v2,))
    assert db.host_memory() == 768 * MIB
    assert db.memory_budget() == (96 * MIB, 12 * MIB)


@pytest.mark.parametrize("written", ["max\n", f"{(1 << 63) - 4096}\n", "", "garbage"])
def test_no_limit_in_the_cgroup_falls_back_to_the_machine(monkeypatch, tmp_path, written):
    v2 = tmp_path / "memory.max"
    v2.write_text(written)
    monkeypatch.setattr(db, "CGROUP_LIMITS", (v2, tmp_path / "absent"))
    machine = db.host_memory()
    assert machine is not None
    assert machine < (1 << 60)
    assert machine != (1 << 63) - 4096


def test_each_connection_gets_the_sized_cache_and_the_heap_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "SOFT_HEAP_LIMIT", 96 * MIB)
    monkeypatch.setattr(db, "CACHE_BYTES", 12 * MIB)
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.sqlite3'}")
    with engine.connect() as conn:
        # Process-wide: put back whatever the rest of the suite runs under.
        before = conn.exec_driver_sql("PRAGMA soft_heap_limit").scalar()
    engine.dispose()
    sa_event.listen(engine, "connect", db._sqlite_pragmas)
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA cache_size").scalar() == -12 * 1024
            assert conn.exec_driver_sql("PRAGMA soft_heap_limit").scalar() == 96 * MIB
            conn.exec_driver_sql(f"PRAGMA soft_heap_limit={before}")
    finally:
        engine.dispose()
