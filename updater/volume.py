"""The shared `update` volume: its layout, and how anything is read or written in it.

Two rules, both from the design notes (5.1, C11):

- **Writes are atomic.** A file is written to a temporary name in the same
  directory, `fsync`ed, renamed over the real name, and then the directory is
  `fsync`ed, so a reader -- or the updater itself after a power cut -- sees the
  old file or the new one, never half of one.
- **Permissions are by group.** Every directory is group 65532, mode 2770 with
  setgid, and every file 0660. Ownership alone cannot work: under a rootless
  engine the updater is in-container uid 0, and with `cap_drop: ALL` that uid 0
  has no `CAP_DAC_OVERRIDE`, so it could not write a directory owned by 65532.
  The app, the maintenance page and the updater all carry group 65532.

What the updater *reads* as input is held to more: never through a symlink
(`O_NOFOLLOW`), only a regular file, only one owned by uid 65532 (the app),
and never more than 4 KiB of it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

#: The group every directory and file in the volume belongs to, and the uid
#: the app runs as -- the only owner a request may have.
UPDATE_GID = 65532
REQUEST_OWNER_UID = 65532

DIR_MODE = 0o2770
FILE_MODE = 0o660

#: The most of a request the updater will read. A real one is well under 1 KiB.
MAX_REQUEST_BYTES = 4096

#: The directories under the volume's root, in the order 5.1 lists them.
SUBDIRS = ("prepared", "history", "journal", "handover", "recovery", "work")


class UnsafeFile(Exception):
    """An input file the updater will not read. The message is the sentence."""


@dataclass(frozen=True)
class Volume:
    """Where everything lives, relative to the volume's mount point."""

    root: Path

    @property
    def request(self) -> Path:
        return self.root / "request.json"

    @property
    def recovery_request(self) -> Path:
        return self.root / "recovery" / "request.json"

    @property
    def heartbeat(self) -> Path:
        return self.root / "updater.json"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    def prepared(self, report_id: str) -> Path:
        return self.root / "prepared" / f"{report_id}.json"

    def history(self, request_id: str) -> Path:
        return self.root / "history" / f"{request_id}.json"

    def journal(self, request_id: str) -> Path:
        return self.root / "journal" / f"{request_id}.json"

    def taken(self, request_id: str) -> Path:
        return self.root / "journal" / f"{request_id}.taken"

    def handover(self, request_id: str, part: str) -> Path:
        return self.root / "handover" / f"{request_id}.{part}"

    def work(self, request_id: str) -> Path:
        return self.root / "work" / request_id

    def seen(self, request_id: str) -> bool:
        """Whether an id has been used before: taken, journalled, recorded or reported."""
        return any(
            os.path.lexists(p)
            for p in (
                self.taken(request_id),
                self.journal(request_id),
                self.history(request_id),
                self.prepared(request_id),
            )
        )

    def init(self, gid: int = UPDATE_GID) -> None:
        """Create the layout, each directory group-shared. Safe to call on every start."""
        make_shared_dir(self.root, gid)
        for name in SUBDIRS:
            make_shared_dir(self.root / name, gid)


def _set_group(fd_or_path: int | Path, gid: int) -> bool:
    """Give a file or directory the volume's group, if this process may.

    Returns whether the group is now `gid`. Changing a group needs either
    membership of it or privilege; a test on a developer's laptop has neither,
    and is told so rather than failing.
    """
    try:
        if isinstance(fd_or_path, int):
            if os.fstat(fd_or_path).st_gid != gid:
                os.fchown(fd_or_path, -1, gid)
        else:
            if os.lstat(fd_or_path).st_gid != gid:
                os.chown(fd_or_path, -1, gid, follow_symlinks=False)
    except PermissionError:
        return False
    return True


def make_shared_dir(path: Path, gid: int = UPDATE_GID) -> bool:
    """Make `path` a directory of the volume: group `gid`, mode 2770, setgid.

    Returns whether the group could be applied. The mode is applied either way.
    A symlink where a directory should be is refused, not followed.
    """
    path = Path(path)
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o770)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeFile(f"{path.name} in the update volume is not a directory.")
    grouped = _set_group(path, gid)
    # The group first, then the mode: changing a group can clear setgid.
    os.chmod(path, DIR_MODE)
    return grouped


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_bytes(path: Path, data: bytes, gid: int = UPDATE_GID) -> None:
    """Atomically replace `path` with `data`, mode 0660, group `gid` where allowed."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(tmp, flags, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)  # whatever the umask took away
        _set_group(fd, gid)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, path)
    fsync_dir(path.parent)


def write_json(path: Path, data: dict, gid: int = UPDATE_GID) -> None:
    """Atomically write one JSON document. Keys sorted, so a diff of two reads."""
    write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode(), gid)


def rename(src: Path, dst: Path) -> None:
    """Rename within the volume and make the rename durable."""
    os.rename(src, dst)
    fsync_dir(Path(dst).parent)
    if Path(src).parent != Path(dst).parent:
        fsync_dir(Path(src).parent)


def read_untrusted(path: Path, owner_uid: int = REQUEST_OWNER_UID, limit: int = MAX_REQUEST_BYTES) -> bytes:
    """Read an input file the app wrote, on the updater's terms.

    Never through a symlink, only a regular file, only one owned by
    `owner_uid`, and never more than `limit` bytes. `O_NONBLOCK` so that a FIFO
    put where a request should be cannot hang the updater on open.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise UnsafeFile("The request is a symbolic link, which the updater never follows.") from e
        if e.errno == errno.ENXIO:  # a FIFO with no writer, or a socket
            raise UnsafeFile("The request is not a regular file.") from e
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeFile("The request is not a regular file.")
        if st.st_uid != owner_uid:
            raise UnsafeFile(f"The request is owned by uid {st.st_uid}, not by the app's uid {owner_uid}.")
        if st.st_size > limit:
            raise UnsafeFile(f"The request is larger than {limit} bytes.")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise UnsafeFile(f"The request is larger than {limit} bytes.")
        return data
    finally:
        os.close(fd)


def read_own_json(path: Path) -> dict | None:
    """Read a file the updater itself wrote (journal, report, handover). None if absent.

    Still never through a symlink: the app shares the volume and could have
    replaced one.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise UnsafeFile(f"{Path(path).name} is a symbolic link, which the updater never follows.") from e
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = json.load(fh)
    finally:
        os.close(fd)
    return data if isinstance(data, dict) else None
