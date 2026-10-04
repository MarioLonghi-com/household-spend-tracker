"""Nothing this app writes is readable by another account on the machine. Issue #92.

Only `secret.key` and `setup-token` were created 0600. The ledger, its WAL and
every backup were `-rw-r--r--`, the logs `-rw-rw-r--`, the directories
`drwxrwxr-x` -- and `app.log` carries the setup token on a first boot.

Each test starts from a permissive umask on purpose. `app.config` tightens the
umask for the whole process as it loads, so any test run after another would
otherwise inherit 0o077 from it and pass whether or not the code under test
still sets it.
"""

from __future__ import annotations

import importlib
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import HEADERS, _setup_owner, _stamp_head

SHARED = stat.S_IRWXG | stat.S_IRWXO


@pytest.fixture()
def permissive_umask():
    was = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(was)


def _boot(monkeypatch, data: Path) -> TestClient:
    """A whole app on a data directory that does not exist yet."""
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(data))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{data / 'spendtracker.sqlite3'}")

    import app.config as config

    importlib.reload(config)
    import app.db as db

    importlib.reload(db)
    from app.models import Base

    Base.metadata.create_all(db.engine)
    _stamp_head(db.engine)
    import app.main as main

    importlib.reload(main)
    return TestClient(main.app, base_url="https://testserver")


def _shared(root: Path) -> list[str]:
    found = []
    for path in [root, *root.rglob("*")]:
        mode = path.lstat().st_mode
        if not stat.S_ISLNK(mode) and mode & SHARED:
            found.append(f"{stat.S_IMODE(mode):04o} {path.relative_to(root.parent)}")
    return found


def test_a_fresh_instance_its_backup_restore_and_snapshot_are_private(
    monkeypatch, tmp_path, permissive_umask
):
    data = tmp_path / "data"
    backups = tmp_path / "offsite"
    backups.mkdir(mode=0o755)
    backups.chmod(0o755)

    with _boot(monkeypatch, data) as client:
        # Seal the owner's authenticator with *this* data directory's key:
        # `crypto` binds `settings` at import, and restore now checks the key it
        # puts in place opens a real secret (#133).
        import app.config as config
        from app.auth import crypto

        monkeypatch.setattr(crypto, "settings", config.settings)
        _setup_owner(client)
        house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS)
        assert house.status_code == 201, house.text

        # Reloaded for the reason `_boot` reloads `app.*`: each binds `settings`
        # at import, and an earlier test in the same run has usually imported
        # it already -- the snapshot then landed in that test's data directory.
        from scripts import backup as backup_script
        from scripts import db_view
        from scripts import restore as restore_script

        for module in (backup_script, restore_script, db_view):
            importlib.reload(module)
        build, database_path, default_destination = (
            db_view.build,
            db_view.database_path,
            db_view.default_destination,
        )

        folder = backup_script.take(backups)
        build(database_path(), default_destination())

    # The premise: every kind of file this issue named was actually made.
    # (`setup-token` is not in the list only because `app.auth.setup` binds
    # the settings object at import, so under the suite's reloads it writes to
    # whichever data directory was current when it was first imported. It is
    # opened 0600 explicitly, and was before this issue.)
    names = {path.name for path in data.rglob("*")} | {p.name for p in backups.rglob("*")}
    for expected in (
        "spendtracker.sqlite3", "secret.key", "app.log", "access.log",
        "snapshot.sqlite3", "manifest.json",
    ):
        assert expected in names, expected

    # Restored over the live one, with the service stopped (port 1 answers nothing).
    assert restore_script.restore(folder, port=1, yes=True) == 0
    assert any(path.name.startswith("spendtracker.sqlite3.before-restore-") for path in data.iterdir())

    assert _shared(data) == []
    assert _shared(backups) == [f"0755 {backups.name}"], (
        "the folder --into names is the operator's; each backup inside it is private"
    )


def test_an_existing_loose_data_directory_is_tightened_and_its_files_named(
    monkeypatch, tmp_path, permissive_umask, caplog
):
    """A umask cannot reach back. What an older version wrote keeps its mode,
    so the directory is tightened and the files are named at boot."""
    data = tmp_path / "data"
    data.mkdir(mode=0o775)
    data.chmod(0o775)
    old_log = data / "left-over.log"
    old_log.write_text("from before\n")
    old_log.chmod(0o664)

    with caplog.at_level("WARNING", logger="spendtracker"), _boot(monkeypatch, data):
        pass

    assert stat.S_IMODE(data.stat().st_mode) == 0o700
    said = "\n".join(record.getMessage() for record in caplog.records)
    assert "can be read by other users" in said
    assert f"0664  {old_log}" in said
    assert "chmod -R go-rwx" in said
    # Warned, not changed behind the owner's back.
    assert stat.S_IMODE(old_log.stat().st_mode) == 0o664


def test_a_private_copy_is_never_created_readable(tmp_path, monkeypatch):
    """`shutil.copy2` creates the file under the umask and copies the mode on
    afterwards; for that moment the key was readable. The file is opened 0600."""
    from app import permissions

    source = tmp_path / "secret.key"
    source.write_text("k" * 44)
    source.chmod(0o644)

    opened: list[tuple[str, int]] = []
    real_open = os.open

    def recording(path, flags, mode=0o777, *args, **kwargs):
        if flags & os.O_CREAT:
            opened.append((os.fspath(path), mode))
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(permissions.os, "open", recording)
    was = os.umask(0)
    try:
        permissions.copy_private(source, tmp_path / "copy.key")
    finally:
        os.umask(was)

    assert opened == [(str(tmp_path / "copy.key"), 0o600)]
    assert stat.S_IMODE((tmp_path / "copy.key").stat().st_mode) == 0o600
    assert (tmp_path / "copy.key").read_text() == "k" * 44
