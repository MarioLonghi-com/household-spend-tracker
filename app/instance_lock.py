"""One process at a time on a ledger: a lock file in the data directory (#318).

Until this, "the port is busy" was how upgrade, restore and reset knew the app
was running. The desktop app has no port, and a second copy of it launched by
a double-click would open the same SQLite files as the first. So the app holds
an operating-system lock on `data_dir/.lock` while it runs, and those scripts
take it before they touch the ledger. The port probe stays as a second check on
a server, where something else may still be answering.

The lock is the kernel's: `flock` on POSIX, `msvcrt.locking` on Windows. It is
released when the process ends however it ends -- a crash leaves no stale lock
for somebody to delete by hand, which is why the file's mere existence means
nothing and is never checked.

Within one process it is counted rather than taken twice: the test suite starts
many apps on one data directory in turn, and nothing in one process needs
protecting from itself.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

NAME = ".lock"

_held: dict[Path, tuple[int, int]] = {}  # directory -> (file descriptor, count)
_guard = threading.Lock()


class LedgerBusy(RuntimeError):
    """Another process holds this ledger."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        super().__init__(
            f"another Spend Tracker is using the ledger in {directory}. Close it, or stop the "
            "service, and try again."
        )


def _try_lock(fd: int) -> bool:
    if os.name == "nt":  # pragma: no cover - exercised on the Windows CI leg
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def acquire(directory: Path, *, wait: float = 0.0) -> None:
    """Hold the ledger in `directory` for this process, or raise `LedgerBusy`.

    `wait` is how many seconds to keep trying while another process holds
    it. For a holder that is on its way out: the updater stops a previous
    version somebody started by hand, and its lock goes a moment after the
    stop -- a drill that tried once in that moment refused the whole update
    (the self-update E7 scenario on rootless Podman).
    """
    deadline = time.monotonic() + wait
    while True:
        try:
            return _acquire_once(directory)
        except LedgerBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _acquire_once(directory: Path) -> None:
    directory = Path(directory).resolve()
    with _guard:
        if directory in _held:
            fd, count = _held[directory]
            _held[directory] = (fd, count + 1)
            return
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / NAME, os.O_RDWR | os.O_CREAT, 0o600)
        if not _try_lock(fd):
            os.close(fd)
            raise LedgerBusy(directory)
        _held[directory] = (fd, 1)


def release(directory: Path) -> None:
    directory = Path(directory).resolve()
    with _guard:
        if directory not in _held:
            return
        fd, count = _held[directory]
        if count > 1:
            _held[directory] = (fd, count - 1)
            return
        del _held[directory]
        # Closing the descriptor releases the lock on both systems.
        os.close(fd)


@contextlib.contextmanager
def holding(directory: Path) -> Iterator[None]:
    acquire(directory)
    try:
        yield
    finally:
        release(directory)


def held_elsewhere(directory: Path) -> bool:
    """Whether another process holds the ledger -- asked without keeping it."""
    directory = Path(directory).resolve()
    with _guard:
        if directory in _held:
            return False
    if not (directory / NAME).exists():
        return False
    try:
        acquire(directory)
    except LedgerBusy:
        return True
    release(directory)
    return False
