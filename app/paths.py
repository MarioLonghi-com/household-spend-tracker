"""Every file this app keeps, named in one place (#317).

The data directory holds the whole installation's state: the ledger and its
WAL, the key, the backups, the logs. Its *location* is `config.resolve_data_dir`'s
question; what lives inside it is this module's. Each name used to be spelled
where it was needed -- `settings.data_dir / "secret.key"` in four places --
and a desktop build that moves the directory, or an operator looking for a
file, should have one list to read.

Everything is resolved per call, from the settings as they are then, never
captured at import: the test suite reloads the settings for each data
directory it makes, and a value taken at import pointed at whichever one came
first (see `api/dbview.snapshot_path`, which learned this first).

No import of `app.config` at module level, so `config` itself can use the
names below while it is being built.
"""

from __future__ import annotations

import os
from pathlib import Path

DATABASE = "spendtracker.sqlite3"
KEY = "secret.key"
SETUP_TOKEN = "setup-token"
SNAPSHOT = "snapshot.sqlite3"
BACKUPS = "backups"
LOGS = "logs"
#: SQLite's sidecars in WAL mode: committed writes not yet in the main file,
#: and the index into them. A copy of the database without its WAL is a copy
#: of an older ledger.
SIDECARS = ("-wal", "-shm")


def _settings():
    from . import config

    return config.settings


def data_dir() -> Path:
    return _settings().data_dir


def key_in(directory: Path) -> Path:
    return directory / KEY


def key() -> Path:
    return key_in(data_dir())


def database() -> Path | None:
    """The SQLite file, or None for any other database."""
    return database_of(_settings().database_url)


def database_of(url: str) -> Path | None:
    """The SQLite file a database URL names, or None for any other database.

    `sqlite:////abs/path` and `sqlite:///relative/path` are the two shapes, and
    the difference is one slash -- so it is parsed here once.
    """
    if not url.startswith("sqlite:"):
        return None
    _, _, tail = url.partition("sqlite:///")
    return Path(tail).resolve() if tail else None


def sidecars(database_file: Path) -> tuple[Path, ...]:
    return tuple(database_file.with_name(database_file.name + suffix) for suffix in SIDECARS)


def backups() -> Path:
    return data_dir() / BACKUPS


def logs() -> Path:
    return data_dir() / LOGS


def snapshot() -> Path:
    return data_dir() / SNAPSHOT


def setup_token() -> Path:
    return data_dir() / SETUP_TOKEN


# --------------------------------------------------------------------------- #
# Folders a sync client owns
# --------------------------------------------------------------------------- #

#: Under the home directory, on any system: where these clients keep the
#: folder they sync unless told otherwise.
_HOME_SYNC_ROOTS = (
    ("Library/Mobile Documents", "iCloud Drive"),
    ("Library/CloudStorage", "a cloud storage provider"),
    ("Dropbox", "Dropbox"),
    ("Google Drive", "Google Drive"),
    ("OneDrive", "OneDrive"),
)

#: Set by OneDrive on Windows to the folders it syncs. Known Folder Move puts
#: Documents and Desktop inside them, silently, so a path that says
#: `Documents` may well be OneDrive's.
_ONEDRIVE_VARIABLES = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except ValueError:  # pragma: no cover - different drives on Windows
        return False


def sync_root(path: Path, *, home: Path | None = None, environ: dict | None = None) -> str | None:
    """The sync service whose folder `path` is in, or None.

    For the live ledger only. SQLite in WAL mode keeps the database, its
    `-wal` and its `-shm` as three files that must change together on one
    local disk, and a sync client uploads and replaces them one at a time --
    the documented way to corrupt a SQLite database. A backup is one finished
    file and is fine anywhere.

    By location, not by asking each client: their folders are where they put
    them by default, and the variables OneDrive sets name its folders on
    Windows. A sync folder somewhere unusual is not caught -- a warning in the
    desktop app's help, not a guarantee.
    """
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    path = Path(os.path.abspath(path))
    for name in _ONEDRIVE_VARIABLES:
        root = (environ.get(name) or "").strip()
        if root and _inside(path, Path(os.path.abspath(root))):
            return "OneDrive"
    # The folder directly under home, for clients that name theirs after the
    # account: "OneDrive - Company", "Dropbox (Personal)".
    top = path.relative_to(home).parts[0] if _inside(path, home) and path != home else ""
    for relative, service in _HOME_SYNC_ROOTS:
        if _inside(path, home / relative):
            return service
        if "/" not in relative and top.startswith((relative + " ", relative + "-")):
            return service
    return None
