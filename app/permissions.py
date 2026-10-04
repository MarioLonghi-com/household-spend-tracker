"""What the files this app writes are allowed to be read by: its own user.

Only `secret.key` and `setup-token` were ever created 0600. Everything else took
the default umask, so on the machine this runs on the ledger, its WAL and every
backup were `-rw-r--r--`, the logs `-rw-rw-r--` and the directories
`drwxrwxr-x` -- readable by every local account. `app.log` holds the setup
token on a first boot. And `scripts/backup.py` copied the key with
`shutil.copy2`, which creates the file under the default umask and only then
copies the mode across, so for a moment the key was world-readable too.

Three layers, because each covers what the one before cannot:

1. **A 0o077 umask for the whole process**, set when the settings are first
   read. Every file anything creates from then on -- SQLite's database and its
   `-wal` and `-shm`, `VACUUM INTO`'s copy, a rotated log -- is private without
   each of them having to remember. SQLite in particular cannot be asked.
2. **The data and backup directories created, or tightened, to 0700.** A
   private file in a listable directory still leaks its name and size.
3. **A warning at startup for anything already there that is not private.** A
   umask only governs what is created after it; the files an older version made
   keep their modes, and chmodding someone's files behind their back on boot is
   not this app's call.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from pathlib import Path

#: Owner read, write, execute; nobody else anything.
PRIVATE_UMASK = 0o077
PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600

#: Group or other may do anything at all.
_SHARED_BITS = stat.S_IRWXG | stat.S_IRWXO


def tighten_umask() -> None:
    """Make every file this process creates from now on private.

    Only ever *narrows*: an operator who already runs with 0o077, or something
    stricter, keeps what they chose.
    """
    current = os.umask(PRIVATE_UMASK)
    os.umask(current | PRIVATE_UMASK)


def private_dir(path: Path) -> Path:
    """Create `path` (and parents) and make it 0700 whether or not it existed.

    Tightened when it already exists, because the data directory of every
    install made before this is `drwxrwxr-x`. A directory this process does not
    own is left as it is rather than failing the boot: the chmod is a
    hardening, and a container volume owned by somebody else is a deployment
    decision.
    """
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(PermissionError):
        path.chmod(PRIVATE_DIR)
    return path


def copy_private(source: Path, destination: Path) -> None:
    """Copy a file so that the copy is never, even briefly, readable by others.

    `shutil.copy2` creates the destination under the process umask and only
    afterwards copies the mode across. Opening with 0600 from the start leaves
    no window. Timestamps are carried over as `copy2` would.
    """
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE)
    with os.fdopen(fd, "wb") as out, source.open("rb") as src:
        shutil.copyfileobj(src, out)
    # O_CREAT's mode is ignored for a file that already existed.
    destination.chmod(PRIVATE_FILE)
    # Times only. `shutil.copystat` would also copy the source's mode across,
    # which is the very thing this function exists not to do.
    seen = source.stat()
    os.utime(destination, ns=(seen.st_atime_ns, seen.st_mtime_ns))


def not_private(root: Path, *, depth: int = 2, limit: int = 20) -> list[tuple[Path, int]]:
    """Paths under `root` that a group or other user can read, list or write,
    each with its permission bits as they were found.

    Bounded in depth, because the check runs on every boot and the data
    directory is where anything can pile up. Two levels reach every file this
    app writes: the database and its sidecars, the key, `logs/*`, `backups/*`.
    """
    found: list[tuple[Path, int]] = []

    def visit(path: Path, level: int) -> None:
        if len(found) >= limit:
            return
        try:
            mode = path.lstat().st_mode
        except OSError:
            return
        if stat.S_ISLNK(mode):
            return
        if mode & _SHARED_BITS:
            found.append((path, stat.S_IMODE(mode)))
        if stat.S_ISDIR(mode) and level < depth:
            try:
                children = sorted(path.iterdir())
            except OSError:
                return
            for child in children:
                visit(child, level + 1)

    visit(root, 0)
    return found
