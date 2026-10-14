"""What the desktop app does with a ledger before the app opens it. Issue #318.

Each test asserts the ledger afterwards -- its stamp, its rows, the backup on
disk -- and two ledgers are used where one could be confused with the other.
The migrations are the real ones: a ledger is made at head through the app,
then taken back one step with Alembic, which is what an older version left.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.conftest import HEADERS, _setup_owner

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def ledger_at(monkeypatch):
    """Point the settings at `data`, and return a function that (re)loads them."""
    import app.config as config

    made: list[Path] = []

    def load(data: Path, database: Path | None = None):
        if database is None:
            monkeypatch.delenv("DATABASE_URL", raising=False)
        else:
            monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
        monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(data))
        made.append(data)
        return importlib.reload(config)

    yield load
    from app import instance_lock

    for data in made:
        while data.resolve() in instance_lock._held:
            instance_lock.release(data)
    monkeypatch.undo()
    importlib.reload(config)


def _alembic(database: Path):
    from alembic.config import Config

    from app.migrate import MIGRATIONS

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def _stamp(database: Path) -> str | None:
    with sqlite3.connect(database) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def _households(database: Path) -> list[str]:
    with sqlite3.connect(database) as conn:
        return sorted(name for (name,) in conn.execute("SELECT name FROM households"))


def _older_ledger(client, database: Path, *names: str) -> str:
    """A ledger with an owner and `names` households, one migration behind head."""
    from alembic import command

    from scripts.upgrade import chain

    _setup_owner(client)
    for name in names:
        made = client.post("/api/households", json={"name": name}, headers=HEADERS)
        assert made.status_code == 201, made.text
    previous = chain()[-2].revision
    client.__exit__(None, None, None)
    import app.db as db

    db.engine.dispose()
    command.downgrade(_alembic(database), previous)
    assert _stamp(database) == previous
    return previous


def test_a_new_ledger_is_migrated_and_nothing_is_backed_up(ledger_at, tmp_path, capfd):
    from app.desktop import launch
    from scripts.upgrade import chain

    ledger_at(tmp_path / "new")
    prepared = launch.prepare(say=lambda _line: None)

    assert prepared.outcome == launch.READY
    assert (prepared.stamp_before, prepared.stamp_after) == (None, chain()[-1].revision)
    assert prepared.backup is None
    assert _stamp(tmp_path / "new" / "spendtracker.sqlite3") == chain()[-1].revision
    assert not (tmp_path / "new" / "backups").exists()
    assert capfd.readouterr().out == "", "standard output is the shell's"


def test_an_older_ledger_is_backed_up_migrated_and_counted_and_its_neighbour_left_alone(
    client, ledger_at, tmp_path
):
    from app.desktop import launch
    from scripts.upgrade import chain

    older = Path(client.app_module.settings.database_url.split("///", 1)[-1])
    previous = _older_ledger(client, older, "Ours", "The flat")
    neighbour = tmp_path / "neighbour"
    ledger_at(neighbour)
    assert launch.prepare(say=lambda _line: None).outcome == launch.READY
    neighbour_stamp = _stamp(neighbour / "spendtracker.sqlite3")

    ledger_at(older.parent, older)
    asked: list[list[str]] = []
    prepared = launch.prepare(say=lambda _line: None, confirm_lossy=lambda titles: asked.append(titles) or True)

    assert prepared.outcome == launch.READY, prepared.log
    assert (prepared.stamp_before, prepared.stamp_after) == (previous, chain()[-1].revision)
    assert asked == [[chain()[-1].title]], "the one lossy step was put to the shell"
    assert _households(older) == ["Ours", "The flat"]
    assert prepared.backup is not None and prepared.backup.parent == older.parent / "backups"
    assert _stamp(prepared.backup / "spendtracker.sqlite3") == previous
    assert _households(prepared.backup / "spendtracker.sqlite3") == ["Ours", "The flat"]
    assert "migrating 1 step(s)" in (prepared.backup / "upgrade.log").read_text()
    assert _stamp(neighbour / "spendtracker.sqlite3") == neighbour_stamp


def test_declining_a_lossy_step_changes_nothing(client, ledger_at):
    from app.desktop import launch

    older = Path(client.app_module.settings.database_url.split("///", 1)[-1])
    previous = _older_ledger(client, older, "Ours")
    before = older.read_bytes()
    ledger_at(older.parent, older)

    prepared = launch.prepare(say=lambda _line: None, confirm_lossy=lambda _titles: False)

    assert prepared.outcome == launch.DECLINED
    assert _stamp(older) == previous
    assert older.read_bytes() == before
    assert not (older.parent / "backups").exists()


def test_a_ledger_a_newer_version_wrote_is_not_touched(ledger_at, tmp_path):
    from app.desktop import launch

    data = tmp_path / "newer"
    ledger_at(data)
    assert launch.prepare(say=lambda _line: None).outcome == launch.READY
    database = data / "spendtracker.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE alembic_version SET version_num = 'f00dfeedbeef'")
    before = database.read_bytes()

    prepared = launch.prepare(say=lambda _line: None)

    assert (prepared.outcome, prepared.stamp_after) == (launch.NEWER, "f00dfeedbeef")
    assert database.read_bytes() == before


def test_a_failed_migration_leaves_the_backup_and_says_so(client, ledger_at, monkeypatch):
    from app import migrate
    from app.desktop import launch

    older = Path(client.app_module.settings.database_url.split("///", 1)[-1])
    previous = _older_ledger(client, older, "Ours")
    ledger_at(older.parent, older)

    def boom(**_):
        raise RuntimeError("no such column: households.colours")

    monkeypatch.setattr(migrate, "upgrade_to_head", boom)
    prepared = launch.prepare(say=lambda _line: None, confirm_lossy=lambda _titles: True)

    assert prepared.outcome == launch.MIGRATION_FAILED
    assert _stamp(older) == previous
    assert _stamp(prepared.backup / "spendtracker.sqlite3") == previous
    assert "no such column" in (prepared.backup / "upgrade.log").read_text()


def test_a_ledger_that_arrived_without_its_key_is_still_upgraded(client, ledger_at):
    """Found on Windows: a ledger copied in by hand, no `secret.key` beside it,
    stopped the frozen backend at the backup. The key is made first now."""
    from app import paths
    from app.desktop import launch
    from scripts.upgrade import chain

    older = Path(client.app_module.settings.database_url.split("///", 1)[-1])
    _older_ledger(client, older, "Ours", "Theirs")
    key = paths.key_in(older.parent)
    key.unlink()
    ledger_at(older.parent, older)

    prepared = launch.prepare(say=lambda _line: None, confirm_lossy=lambda _titles: True)

    assert prepared.outcome == launch.READY, prepared.log
    assert _stamp(older) == chain()[-1].revision
    assert _households(older) == ["Ours", "Theirs"]
    assert key.exists()
    assert (prepared.backup / "secret.key").read_bytes() == key.read_bytes()


def test_a_backup_that_cannot_be_made_stops_the_start_unmigrated(client, ledger_at, monkeypatch):
    from app.desktop import launch
    from scripts import backup as backup_script

    older = Path(client.app_module.settings.database_url.split("///", 1)[-1])
    previous = _older_ledger(client, older, "Ours")
    ledger_at(older.parent, older)

    def refuse(*_args, **_kwargs):
        raise SystemExit("the disk is full")

    monkeypatch.setattr(backup_script, "take", refuse)
    prepared = launch.prepare(say=lambda _line: None, confirm_lossy=lambda _titles: True)

    assert prepared.outcome == launch.BACKUP_FAILED
    assert "the disk is full" in prepared.log[-1]
    assert _stamp(older) == previous
    assert _households(older) == ["Ours"]


def test_a_second_process_on_the_same_ledger_is_refused(ledger_at, tmp_path):
    """The lock is the kernel's, so it takes another process to see it."""
    from app import instance_lock
    from app.desktop import launch

    data = tmp_path / "shared"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            from app import instance_lock
            instance_lock.acquire({str(data)!r})
            print("held", flush=True)
            sys.stdin.read()
        """)],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={**os.environ, "SPENDTRACKER_DATA_DIR": str(data)},
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        assert instance_lock.held_elsewhere(data) is True
        ledger_at(data)
        with pytest.raises(instance_lock.LedgerBusy, match="another Spend Tracker is using the ledger"):
            launch.prepare(say=lambda _line: None)
        assert not (data / "spendtracker.sqlite3").exists(), "nothing was created"
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)
    # Released with the process that held it.
    assert instance_lock.held_elsewhere(data) is False
    assert launch.prepare(say=lambda _line: None).outcome == launch.READY


def _holder(data: Path, *, for_seconds: float | None) -> subprocess.Popen:
    """Another process holding the ledger: until stdin closes, or for a while."""
    let_go = "sys.stdin.read()" if for_seconds is None else f"time.sleep({for_seconds})"
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            from app import instance_lock
            instance_lock.acquire({str(data)!r})
            print("held", flush=True)
            {let_go}
        """)],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env={**os.environ, "SPENDTRACKER_DATA_DIR": str(data)},
    )
    assert holder.stdout.readline().strip() == "held"
    return holder


def test_a_ledger_being_let_go_is_waited_for(tmp_path):
    """The updater stops a previous version somebody started by hand, and its
    lock goes a moment later; a drill that tried once in that moment refused
    the whole update (self-update E7, rootless Podman, on the 0.11.0 release)."""
    import time

    from app import instance_lock

    data = tmp_path / "shared"
    holder = _holder(data, for_seconds=1.0)
    try:
        started = time.monotonic()
        instance_lock.acquire(data, wait=30)
        waited = time.monotonic() - started
        assert 0.3 < waited < 30, waited
        assert instance_lock.held_elsewhere(data) is False
    finally:
        instance_lock.release(data)
        holder.wait(timeout=30)


def test_a_ledger_still_held_after_the_wait_is_refused(tmp_path):
    import time

    from app import instance_lock

    data = tmp_path / "shared"
    holder = _holder(data, for_seconds=None)
    try:
        started = time.monotonic()
        with pytest.raises(instance_lock.LedgerBusy):
            instance_lock.acquire(data, wait=0.6)
        assert time.monotonic() - started >= 0.6
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


def test_a_ledger_the_server_holds_refuses_restore_and_reset_from_another_process(client, tmp_path):
    """The running app holds its ledger, so the scripts refuse from outside it."""
    data = Path(client.app_module.settings.data_dir)
    for script in ("scripts.reset", "scripts.restore"):
        args = ["--yes"] if script == "scripts.reset" else [str(tmp_path / "nothing"), "--yes"]
        done = subprocess.run(
            [sys.executable, "-m", script, *args, "--port", "1"],
            cwd=ROOT, capture_output=True, text=True, timeout=120,
            env={**os.environ, "SPENDTRACKER_DATA_DIR": str(data),
                 "DATABASE_URL": client.app_module.settings.database_url},
        )
        assert done.returncode != 0, (script, done.stdout, done.stderr)
        assert "another Spend Tracker is using the ledger" in done.stderr, (script, done.stderr)
