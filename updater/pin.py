"""The pin: what compose starts the next time it is run (design notes 9.1, S1).

`COMPOSE_ENV_FILES` set in a project's `.env` is ignored by Docker Compose and
both Podman composes (S1, S14), so **the `.env` write is the mechanism**: the
two image keys are replaced in the project's `.env` -- every other line left
as it was -- through a temporary file and a rename inside the mounted project
directory (`/project`). A rename needs the directory: a single-file mount of
`.env` cannot be renamed over (C12). `pin/release.env` is written beside it as
the human-readable record, which the launcher also passes as a second
`--env-file`.

`.env` holds other things -- `TS_AUTHKEY` on a server -- so its mode is kept,
and a fresh one is 0600.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

from updater import volume

APP_KEY = "SPENDTRACKER_IMAGE"
UPDATER_KEY = "SPENDTRACKER_UPDATER_IMAGE"
KEYS = (APP_KEY, UPDATER_KEY)

RECORD_HEADER = "# Written by the updater. Do not edit; the Application screen changes it.\n"


def image_ref(repository: str, version: str, digest: str) -> str:
    """`repo:X.Y.Z@sha256:…`: the tag a person reads, the digest an engine pulls."""
    return f"{repository}:{version}@{digest}"


def _replace(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
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
    volume.fsync_dir(path.parent)


def merged_env(existing: str, values: dict[str, str]) -> str:
    """`existing` with each key in `values` set: its first line replaced, later ones dropped."""
    out: list[str] = []
    done: set[str] = set()
    for line in existing.splitlines():
        key = line.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key in values and "=" in line and not line.lstrip().startswith("#"):
            if key not in done:
                out.append(f"{key}={values[key]}")
                done.add(key)
            continue
        out.append(line)
    for key in values:
        if key not in done:
            out.append(f"{key}={values[key]}")
    return "\n".join(out) + "\n"


def set_env(project_dir: Path, values: dict[str, str]) -> None:
    """Each key of `values` set in the project's `.env`, every other line kept, its mode too.

    The pin's two keys, and the launcher's per-engine settings (`updater.launch`).
    """
    env = Path(project_dir) / ".env"
    try:
        st = os.lstat(env)
        mode = stat.S_IMODE(st.st_mode) if stat.S_ISREG(st.st_mode) else 0o600
        existing = env.read_text(encoding="utf-8") if stat.S_ISREG(st.st_mode) else ""
    except FileNotFoundError:
        mode, existing = 0o600, ""
    _replace(env, merged_env(existing, values).encode(), mode)


def write(project_dir: Path, app: str | None, updater: str | None) -> dict[str, str]:
    """Pin `app` (and `updater`, when known) in `.env` and record them in `pin/release.env`.

    Returns what was pinned. Idempotent: writing the same pin twice leaves the
    same files (step 9 is repeated after a crash, 5.6). With `app` None only
    the updater's line changes -- an updater-only refresh or a handover (6.6)
    -- and the record keeps the app line it had.
    """
    values: dict[str, str] = {}
    if app:
        values[APP_KEY] = app
    if updater:
        values[UPDATER_KEY] = updater
    if not values:
        return {}
    set_env(project_dir, values)

    record_dir = Path(project_dir) / "pin"
    record_dir.mkdir(mode=0o775, exist_ok=True)
    recorded = {**_read_keys(record_dir / "release.env"), **values}
    lines = [RECORD_HEADER, *(f"{k}={recorded[k]}\n" for k in KEYS if k in recorded)]
    _replace(record_dir / "release.env", "".join(lines).encode(), 0o664)
    return values


def write_updater(project_dir: Path, updater: str) -> dict[str, str]:
    """Only the updater's line (6.6): the app's pin is left exactly as it was."""
    return write(project_dir, None, updater)


def read(project_dir: Path) -> dict[str, str]:
    """The pinned keys as `.env` holds them now."""
    return _read_keys(Path(project_dir) / ".env")


def _read_keys(path: Path) -> dict[str, str]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in KEYS:
            out[key.strip()] = value.strip()
    return out
