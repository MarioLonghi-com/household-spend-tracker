"""Restore from a backup, as the desktop app does it. Issue #62.

Every test reads the live ledger afterwards. Two households throughout: one in
the backup and one added after it, so "the backup came back" and "nothing
changed" can never be the same observation.
"""

from __future__ import annotations

import contextlib
import importlib
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from tests.conftest import HEADERS, PASSWORD


@pytest.fixture()
def desktop_ledger(monkeypatch, tmp_path):
    """A desktop data directory with a set-up ledger holding "Before", a
    backup of it, then "After" added. Yields (live database, backup)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SPENDTRACKER_PROFILE", "desktop")
    data = tmp_path / "ledger"
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(data))
    import app.config as config

    importlib.reload(config)
    import app.db as db

    importlib.reload(db)
    from app.models import Base
    from tests.conftest import _stamp_head

    Base.metadata.create_all(db.engine)
    _stamp_head(db.engine)
    import app.main as main

    importlib.reload(main)
    from fastapi.testclient import TestClient

    from app.desktop import control

    with TestClient(main.app, base_url="https://localhost") as client:
        who = {"token": "", "email": "jane@example.test", "display_name": "Jane", "password": PASSWORD}
        begun = client.post("/api/setup/begin", json=who, headers=HEADERS)
        assert begun.status_code == 200, begun.text
        done = client.post("/api/setup/complete", json={"blob": begun.json()["blob"], "codes_saved": True}, headers=HEADERS)
        assert done.status_code == 200, done.text
        assert client.post("/api/households", json={"name": "Before"}, headers=HEADERS).status_code == 201
        kept = tmp_path / "kept.sqlite3"
        Path(control.backup()["path"]).rename(kept)
        assert client.post("/api/households", json={"name": "After"}, headers=HEADERS).status_code == 201
    db.engine.dispose()
    yield data / "spendtracker.sqlite3", kept

    from app import instance_lock

    while data.resolve() in instance_lock._held:
        instance_lock.release(data)
    monkeypatch.undo()
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(main)


def _households(database: Path) -> list[str]:
    with contextlib.closing(sqlite3.connect(database)) as conn:
        return sorted(name for (name,) in conn.execute("SELECT name FROM households"))


def test_a_sqlite_backup_replaces_the_ledger_and_the_old_one_is_set_aside(desktop_ledger):
    from app.desktop import restore

    live, kept = desktop_ledger
    assert _households(live) == ["After", "Before"]
    done = restore.restore(kept, now=datetime(2026, 10, 13, 20, 0, 0))
    assert (done.outcome, done.reason) == ("restored", "")
    assert _households(live) == ["Before"]
    aside = live.with_name("spendtracker.sqlite3.before-restore-20261013-200000")
    assert aside.name in done.aside
    assert _households(aside) == ["After", "Before"]
    assert done.rows > 0


def test_a_zip_from_the_application_screen_restores_too(desktop_ledger, tmp_path):
    from app.desktop import restore
    from app.services import backup_bundle

    live, kept = desktop_ledger
    bundle = backup_bundle.build(kept, into=tmp_path, version="0.10.1", repository="https://example.test/repo", secret_key=None)
    done = restore.restore(bundle.path)
    assert done.ok, done.reason
    assert _households(live) == ["Before"]


def test_a_newer_ledger_is_refused_and_nothing_changes(desktop_ledger):
    from app.desktop import restore

    live, kept = desktop_ledger
    with contextlib.closing(sqlite3.connect(kept)) as conn:
        conn.execute("UPDATE alembic_version SET version_num = 'f00dfeedbeef'")
        conn.commit()
    done = restore.restore(kept)
    assert (done.outcome, done.revision) == ("newer", "f00dfeedbeef")
    assert "newer version" in done.reason
    assert _households(live) == ["After", "Before"]
    assert not list(live.parent.glob("*.before-restore-*"))


@pytest.mark.parametrize(
    ("name", "content", "says"),
    [
        ("notes.sqlite3", b"not a database at all, just words" * 200, "damaged"),
        ("notes.txt", b"hello", "not a Spend Tracker backup"),
        ("empty.zip", b"PK\x05\x06" + b"\x00" * 18, "no spendtracker.sqlite3"),
    ],
)
def test_what_is_not_a_backup_is_refused_and_nothing_changes(desktop_ledger, tmp_path, name, content, says):
    from app.desktop import restore

    live, _ = desktop_ledger
    wrong = tmp_path / name
    wrong.write_bytes(content)
    done = restore.restore(wrong)
    assert done.outcome == "refused"
    assert says in done.reason
    assert _households(live) == ["After", "Before"]


def test_a_database_that_is_not_this_apps_is_refused(desktop_ledger, tmp_path):
    from app.desktop import restore

    live, _ = desktop_ledger
    other = tmp_path / "other.sqlite3"
    with contextlib.closing(sqlite3.connect(other)) as conn:
        conn.execute("CREATE TABLE recipes (name TEXT)")
        conn.commit()
    done = restore.restore(other)
    assert done.outcome == "refused"
    assert "not a Spend Tracker ledger" in done.reason
    assert _households(live) == ["After", "Before"]


def test_only_the_newest_set_aside_ledger_is_kept(desktop_ledger):
    from app.desktop import restore

    live, kept = desktop_ledger
    assert restore.restore(kept, now=datetime(2026, 10, 13, 20, 0, 0)).ok
    assert restore.restore(kept, now=datetime(2026, 10, 13, 21, 0, 0)).ok
    left = sorted(p.name for p in live.parent.glob("*.before-restore-*"))
    assert left and all(name.endswith("20261013-210000") for name in left)
    # The second restore set aside what the first put in: "Before" only.
    assert _households(live.with_name("spendtracker.sqlite3.before-restore-20261013-210000")) == ["Before"]


# --------------------------------------------------------------------------- #
# A restore that is killed part-way (#442)
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent.parent

#: Run as its own process, so the kill is a real one: nothing after it runs,
#: no `except` arm, no `finally`. Killed in `restore._step` at the step named,
#: or, for "copying", half-way through writing the copy.
_KILLED_RESTORE = """
import os, sys
from pathlib import Path
from app import permissions
from app.desktop import restore

source, at = Path(sys.argv[1]), sys.argv[2]

def step(name):
    if name == at:
        os._exit(9)

def half_written(source, destination):
    data = source.read_bytes()
    permissions.partial_of(destination).write_bytes(data[: len(data) // 2])
    os._exit(9)

restore._step = step
if at == "copying":
    permissions.replace_private = half_written
restore.restore(source)
os._exit(0)
"""


def _killed_during_restore(live: Path, kept: Path, at: str) -> None:
    import os
    import subprocess
    import sys

    from app import instance_lock

    data = live.parent
    while data.resolve() in instance_lock._held:
        instance_lock.release(data)
    env = {key: value for key, value in os.environ.items() if not key.startswith("SPENDTRACKER_") and key != "DATABASE_URL"}
    env.update(SPENDTRACKER_PROFILE="desktop", SPENDTRACKER_DATA_DIR=str(data), PYTHONPATH=str(ROOT))
    ran = subprocess.run([sys.executable, "-c", _KILLED_RESTORE, str(kept), at], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert ran.returncode == 9, ran.stderr


def _left_over(live: Path) -> list[str]:
    return sorted(p.name for p in live.parent.iterdir() if p.name.endswith((".restoring", ".partial")))


@pytest.mark.parametrize(
    ("at", "recovered", "households"),
    [
        # Marked, nothing moved yet.
        ("marked", "rolled-back", ["After", "Before"]),
        # The ledger set aside: no file of its name at all.
        ("moved spendtracker.sqlite3", "rolled-back", ["After", "Before"]),
        # Half of the backup written, under its temporary name.
        ("copying", "rolled-back", ["After", "Before"]),
        # The backup whole in place, the tidying not done.
        ("replaced", "finished", ["Before"]),
    ],
)
def test_the_start_after_a_killed_restore_opens_the_old_ledger_or_the_restored_one(desktop_ledger, at, recovered, households):
    from app.desktop import launch

    live, kept = desktop_ledger
    _killed_during_restore(live, kept, at)
    assert live.with_name(live.name + ".restoring").exists(), "the mark outlives the kill"

    prepared = launch.prepare(say=lambda _line: None)
    assert prepared.outcome == launch.READY, prepared.log
    assert prepared.recovered == recovered
    assert prepared.migrated == [], "never made from nothing"
    assert _households(live) == households
    assert _left_over(live) == []
    asides = sorted(live.parent.glob("spendtracker.sqlite3.before-restore-*"))
    if recovered == "finished":
        assert [_households(one) for one in asides] == [["After", "Before"]]
    else:
        assert asides == []


def test_a_ledger_an_older_version_set_aside_and_never_replaced_goes_back(desktop_ledger):
    """No mark: what a kill left before there was one."""
    from app.desktop import launch

    live, _ = desktop_ledger
    live.rename(live.with_name("spendtracker.sqlite3.before-restore-20261013-200000"))
    prepared = launch.prepare(say=lambda _line: None)
    assert (prepared.outcome, prepared.recovered, prepared.migrated) == (launch.READY, "rolled-back", [])
    assert _households(live) == ["After", "Before"]
    assert not list(live.parent.glob("*.before-restore-*"))


def test_a_partly_written_ledger_stops_the_start_and_is_left_as_it_is(desktop_ledger):
    from app.desktop import launch

    live, _ = desktop_ledger
    half = live.read_bytes()[:4096] + b"\x00" * 4096
    live.write_bytes(half)
    prepared = launch.prepare(say=lambda _line: None)
    assert prepared.outcome == launch.DAMAGED
    assert "not a readable database" in prepared.log[-1]
    assert live.read_bytes() == half


def test_a_restore_after_a_killed_one_keeps_the_real_ledger_aside(desktop_ledger):
    """The second restore used to set aside the empty ledger the killed one
    left, and drop the set-aside copy holding the real one."""
    from app.desktop import restore

    live, kept = desktop_ledger
    _killed_during_restore(live, kept, "copying")
    done = restore.restore(kept, now=datetime(2031, 1, 2, 3, 4, 5))
    assert done.ok, done.reason
    assert _households(live) == ["Before"]
    asides = sorted(live.parent.glob("spendtracker.sqlite3.before-restore-*"))
    assert [one.name for one in asides] == ["spendtracker.sqlite3.before-restore-20310102-030405"]
    assert _households(asides[0]) == ["After", "Before"]
    assert _left_over(live) == []


def test_a_failure_inside_the_swap_puts_the_ledger_back(desktop_ledger, monkeypatch):
    from app import permissions
    from app.desktop import restore

    live, kept = desktop_ledger

    def full_disk(source, destination):
        permissions.partial_of(destination).write_bytes(b"half")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(permissions, "replace_private", full_disk)
    with pytest.raises(OSError):
        restore.restore(kept)
    assert _households(live) == ["After", "Before"]
    assert not list(live.parent.glob("*.before-restore-*"))
    assert _left_over(live) == []


def test_a_second_restore_in_the_same_second_sets_aside_under_a_name_of_its_own(desktop_ledger):
    from app.desktop import restore

    live, kept = desktop_ledger
    when = datetime(2026, 10, 13, 20, 0, 0)
    assert restore.restore(kept, now=when).ok
    second = restore.restore(kept, now=when)
    assert second.ok
    assert second.aside == ["spendtracker.sqlite3.before-restore-20261013-200000-2"]
    assert _households(live.with_name(second.aside[0])) == ["Before"]
