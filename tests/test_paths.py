"""Where the app keeps its files, and when it first writes one. Issue #317.

The desktop app has to lock, back up and migrate a ledger before anything
opens it, and names the folder itself. So: importing the app writes nothing,
each data directory gets its own key, the desktop profile refuses to guess a
folder or to use one a sync client owns, and every path the app reports comes
from one list.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _python(code: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """A fresh interpreter, so "importing" means importing for the first time."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SPENDTRACKER_") and k != "DATABASE_URL"}
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env={**clean, **env}, capture_output=True, text=True, timeout=120
    )


def test_importing_the_app_writes_nothing(tmp_path):
    data = tmp_path / "not-yet"
    done = _python("import app.config, app.db, app.main", {"SPENDTRACKER_DATA_DIR": str(data)})
    assert done.returncode == 0, done.stderr
    assert not data.exists()


def test_the_first_connection_makes_the_folder_and_the_first_use_the_key(tmp_path):
    data = tmp_path / "ledger"
    done = _python(
        "import os, app.config, app.db\n"
        "from sqlalchemy import text\n"
        "with app.db.engine.connect() as c: c.execute(text('select 1'))\n"
        "print(sorted(os.listdir(os.environ['SPENDTRACKER_DATA_DIR'])))\n"
        "app.config.settings.key()\n"
        "print(sorted(os.listdir(os.environ['SPENDTRACKER_DATA_DIR'])))\n",
        {"SPENDTRACKER_DATA_DIR": str(data)},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["['spendtracker.sqlite3']", "['secret.key', 'spendtracker.sqlite3']"]
    if os.name != "nt":
        assert (data.stat().st_mode & 0o777) == 0o700
        assert ((data / "secret.key").stat().st_mode & 0o777) == 0o600


@pytest.fixture()
def reload_config(monkeypatch):
    import app.config as config

    def load(**env: str):
        for name in ("SPENDTRACKER_PROFILE", "SPENDTRACKER_SECRET_KEY", "DATABASE_URL"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return importlib.reload(config)

    yield load
    monkeypatch.undo()
    importlib.reload(config)


def test_two_data_directories_get_two_keys_and_neither_touches_the_other(tmp_path, reload_config):
    first, second = tmp_path / "first", tmp_path / "second"
    config = reload_config(SPENDTRACKER_DATA_DIR=str(first))
    first_key = config.settings.key()
    config = reload_config(SPENDTRACKER_DATA_DIR=str(second))
    second_key = config.settings.key()

    assert first_key != second_key
    assert (first / "secret.key").read_text() == first_key
    assert (second / "secret.key").read_text() == second_key
    assert sorted(p.name for p in first.iterdir()) == ["secret.key"]
    assert sorted(p.name for p in second.iterdir()) == ["secret.key"]
    # Asked again, the first directory's key is the same one, not a new one.
    config = reload_config(SPENDTRACKER_DATA_DIR=str(first))
    assert config.settings.key() == first_key


def test_a_key_from_the_environment_is_used_and_no_file_is_made(tmp_path, reload_config):
    config = reload_config(SPENDTRACKER_DATA_DIR=str(tmp_path / "data"), SPENDTRACKER_SECRET_KEY="a" * 44)
    assert config.settings.key() == "a" * 44
    assert not (tmp_path / "data").exists()


def test_the_desktop_profile_needs_its_folder_named(monkeypatch, tmp_path):
    from app.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPENDTRACKER_DATA_DIR", raising=False)
    monkeypatch.setenv("SPENDTRACKER_PROFILE", "desktop")
    with pytest.raises(ValueError, match="SPENDTRACKER_DATA_DIR must be set under the desktop profile"):
        Settings.from_env()
    # The server keeps its three answers, and found ./data here.
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("SPENDTRACKER_PROFILE", "server")
    assert Settings.from_env().data_dir == (tmp_path / "data").resolve()


def test_the_desktop_profile_refuses_a_ledger_a_sync_client_owns(monkeypatch, tmp_path):
    from app.config import Settings

    synced = tmp_path / "OneDrive - Example"
    monkeypatch.setenv("OneDriveCommercial", str(synced))
    monkeypatch.setenv("SPENDTRACKER_PROFILE", "desktop")
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(synced / "Documents" / "Spend Tracker"))
    with pytest.raises(ValueError, match="cannot live in a folder OneDrive syncs"):
        Settings.from_env()
    # The server profile is not the desktop's to police: an operator chose it.
    monkeypatch.setenv("SPENDTRACKER_PROFILE", "server")
    assert Settings.from_env().data_dir == (synced / "Documents" / "Spend Tracker").resolve()


@pytest.mark.parametrize(
    ("relative", "service"),
    [
        ("Library/Mobile Documents/com~apple~CloudDocs/Spend Tracker", "iCloud Drive"),
        ("Library/CloudStorage/GoogleDrive-jane@example.test/My Drive/ledger", "a cloud storage provider"),
        ("Library/CloudStorage/OneDrive-Personal/ledger", "a cloud storage provider"),
        ("Dropbox/ledger", "Dropbox"),
        ("Dropbox (Personal)/ledger", "Dropbox"),
        ("OneDrive/Documents/ledger", "OneDrive"),
        ("OneDrive - Example Ltd/Documents/ledger", "OneDrive"),
        ("Google Drive/ledger", "Google Drive"),
        ("Library/Containers/com.example.spendtracker/Data/Library/Application Support/Spend Tracker", None),
        ("AppData/Local/Spend Tracker", None),
        ("Documents/Spend Tracker", None),
        ("Dropboxes/ledger", None),
    ],
)
def test_which_folders_a_sync_client_owns(tmp_path, relative, service):
    from app.paths import sync_root

    assert sync_root(tmp_path / relative, home=tmp_path, environ={}) == service


def test_onedrive_is_found_where_windows_says_it_is(tmp_path):
    """Known Folder Move: Documents is OneDrive's, at a path that says neither."""
    from app.paths import sync_root

    elsewhere = tmp_path / "D" / "Work Sync"
    environ = {"OneDriveCommercial": str(elsewhere)}
    assert sync_root(elsewhere / "Documents" / "ledger", home=tmp_path / "home", environ=environ) == "OneDrive"
    assert sync_root(tmp_path / "D" / "ledger", home=tmp_path / "home", environ=environ) is None


def test_every_place_the_app_reports_is_named_from_one_list(client):
    from app import paths
    from app.services import platform

    found = {place.what: Path(place.path) for place in platform.places()}
    data = found["Data directory"]
    assert found["Secret key"] == data / paths.KEY
    assert found["Backups"] == data / paths.BACKUPS
    assert found["Log files"] == data / paths.LOGS
    # The viewer and the setup wizard keep the settings they were imported
    # with, which the suite's reloads can leave on another test's directory;
    # their names are still these.
    assert found["Browsable snapshot"].name == paths.SNAPSHOT
    assert found["Setup token"].name == paths.SETUP_TOKEN
    database = found["Database"]
    assert found["Write-ahead log"] == database.with_name(database.name + "-wal")
