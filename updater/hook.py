"""The pre-update hook: a command the operator runs on the host before an update (design notes 6.5, A3).

**Off unless configured, and its absence never blocks an update.** A server's
operator installs the runner from `deploy/updater/host-hook/` on the host and
mounts its directory into the updater at `/hook`. The command runs **on the
host**, never in a container: the motivating case is a hypervisor snapshot of
the machine the ledger lives on, which nothing inside a container can take.

**Configured** means `/hook/hook.json` exists. The mount alone is not a
configuration: a directory with no `hook.json` is skipped exactly as a missing
mount is, and the skip is logged.

## The handshake

| Who | Writes | When |
|---|---|---|
| updater | `<id>.request`: `{"id", "from", "to"}` | step 2 begins |
| runner | renames `<id>.request` to `<id>.taken` | it has picked the request up |
| runner | `<id>.result`: `{"id", "exit", "output"}` | the command ended (or was refused) |

The request names versions only. The runner never runs anything the request
says: it runs the command the operator configured in a root-owned file, and
hands it `from` and `to` as environment variables after checking each is
`X.Y.Z`.

## When it fails (A3)

Every failure is *not started*: nothing has been stopped or changed yet.

- **A non-zero exit**, or a request the runner refused (it writes a non-zero
  exit for those too).
- **No runner answered**: nothing took the request within `PICKUP_SECONDS`
  (or the whole timeout, if that is shorter). A systemd path unit picks a
  request up in well under a second; a minute without it means the units are
  not installed, not enabled, or not watching this directory.
- **A timeout**: no result within `timeout_seconds` from `hook.json`
  (default 300, at most `MAX_TIMEOUT_SECONDS`).
- **A result the updater will not read**: a symbolic link, not a regular
  file, larger than `MAX_RESULT_BYTES`, not JSON, or about another id.
- **A broken configuration**: a `hook.json` that is a symbolic link, not JSON,
  or too large. The operator configured a hook; a configuration that cannot
  be read is not the same as none, so it is not skipped.

Both waits are gap-aware (`updater.clock.Deadline`): a laptop -- or a VM --
that sleeps during the wait does not come back to a timed-out hook.

## Why no owner check on the result

The updater's other inputs are checked against the app's uid (65532),
because the app shares the `update` volume. Nothing but the updater and the
host's root can write in the hook directory: the app never mounts it. The
runner's uid is root on the host, which a rootful engine shows as 0 and a
rootless one as the overflow uid, so a fixed owner would break the second
without protecting the first -- and the updater, holding the engine socket,
is root-equivalent already. Symlinks and size are still refused, because
they cost nothing and a mistake by the operator is as likely as an attack.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from updater import volume
from updater.clock import Deadline, GapClock

CONFIG = "hook.json"
DEFAULT_TIMEOUT_SECONDS = 300
#: An hour. A snapshot of a large VM is minutes; anything longer is a hook
#: that has hung, and the owner is looking at a spinner the whole time.
MAX_TIMEOUT_SECONDS = 3600
#: How long a request may wait for a runner to take it.
PICKUP_SECONDS = 60
#: What a runner keeps of the command's output, and the most of a result the
#: updater reads: the output, JSON-escaped (up to six bytes a character for a
#: control character), plus the envelope.
MAX_OUTPUT_BYTES = 4096
MAX_RESULT_BYTES = 8 * MAX_OUTPUT_BYTES
MAX_CONFIG_BYTES = 4096
#: How much of the output goes into the history's notes.
TAIL_LINES = 20

SKIPPED = "No pre-update hook is configured; skipped."
#: Part 12, row 12. The owner's sentence for every failure; the detail is in the history.
FAILED = "the pre-update hook failed."


@dataclass(frozen=True)
class Config:
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    #: Set when `hook.json` exists but cannot be used: the hook then fails.
    problem: str | None = None


@dataclass(frozen=True)
class Outcome:
    ok: bool
    #: `succeeded`, `failed`, `refused`, `no_runner`, `timed_out`, `unreadable`, `unwritable`
    #: or `misconfigured`.
    reason: str
    #: For the history: what happened, and the end of the hook's output.
    sentence: str
    exit: int | None = None
    output: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "exit": self.exit, "output": self.output}


class Unreadable(Exception):
    """A file in the hook directory the updater will not read."""


def _read_capped(path: Path, limit: int) -> bytes | None:
    """A regular file, never through a symlink, never more than `limit` bytes. None if absent."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Unreadable(f"{path.name} is a symbolic link, which the updater never follows.") from e
        if e.errno == errno.ENXIO:
            raise Unreadable(f"{path.name} is not a regular file.") from e
        raise Unreadable(f"{path.name} could not be opened ({e.strerror}).") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Unreadable(f"{path.name} is not a regular file.")
        if st.st_size > limit:
            raise Unreadable(f"{path.name} is larger than {limit} bytes.")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise Unreadable(f"{path.name} is larger than {limit} bytes.")
        return data
    finally:
        os.close(fd)


def _json(data: bytes, name: str) -> dict:
    try:
        doc = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Unreadable(f"{name} is not JSON.") from None
    if not isinstance(doc, dict):
        raise Unreadable(f"{name} is not a JSON object.")
    return doc


def configured(hook_dir: Path | None) -> Config | None:
    """The hook's configuration, or None when there is no hook (6.5: step 2 is skipped)."""
    if hook_dir is None:
        return None
    path = Path(hook_dir) / CONFIG
    try:
        data = _read_capped(path, MAX_CONFIG_BYTES)
        if data is None:
            return None
        doc = _json(data, CONFIG)
    except Unreadable as e:
        return Config(problem=str(e))
    timeout = doc.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        return Config(
            problem=f"timeout_seconds in {CONFIG} is not a whole number of seconds from 1 to {MAX_TIMEOUT_SECONDS}."
        )
    return Config(timeout=timeout)


def paths(hook_dir: Path, request_id: str) -> tuple[Path, Path, Path]:
    """`<id>.request`, `<id>.taken`, `<id>.result`."""
    d = Path(hook_dir)
    return d / f"{request_id}.request", d / f"{request_id}.taken", d / f"{request_id}.result"


def read_result(path: Path, request_id: str) -> dict | None:
    """The runner's result, checked. None while there is none yet."""
    data = _read_capped(path, MAX_RESULT_BYTES)
    if data is None:
        return None
    doc = _json(data, path.name)
    if doc.get("id") != request_id:
        raise Unreadable(f"{path.name} is about another request.")
    code = doc.get("exit")
    if isinstance(code, bool) or not isinstance(code, int):
        raise Unreadable(f"{path.name} has no exit status.")
    output = doc.get("output", "")
    if not isinstance(output, str):
        raise Unreadable(f"{path.name} has output that is not text.")
    return {"exit": code, "output": output[-MAX_OUTPUT_BYTES:], "refused": doc.get("refused")}


def tail(output: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(output.rstrip("\n").splitlines()[-lines:])


def _said(output: str) -> str:
    end = tail(output)
    return f" Its output ended:\n{end}" if end else " It printed nothing."


def run(
    hook_dir: Path,
    config: Config,
    request_id: str,
    from_version: str,
    to_version: str,
    *,
    clock: GapClock,
    sleep: Callable[[float], None],
    poll: float = 1.0,
    pickup: float = PICKUP_SECONDS,
) -> Outcome:
    """Write the request, wait for the runner, and judge its result."""
    if config.problem:
        return Outcome(
            False, "misconfigured", f"The pre-update hook is configured but unusable: {config.problem}"
        )
    request, taken, result_path = paths(hook_dir, request_id)
    # Atomic: a temporary file, then a rename over the name -- which replaces
    # a symlink planted there rather than writing through it.
    try:
        volume.write_json(request, {"id": request_id, "from": from_version, "to": to_version})
    except OSError as e:
        return Outcome(
            False, "unwritable", f"The pre-update hook's request could not be written ({e.strerror or e})."
        )
    whole = Deadline(clock, config.timeout)
    first = Deadline(clock, min(pickup, config.timeout))
    while True:
        try:
            result = read_result(result_path, request_id)
        except Unreadable as e:
            return Outcome(False, "unreadable", f"The pre-update hook's result was refused: {e}")
        if result is not None:
            code, output = result["exit"], result["output"]
            if code == 0:
                return Outcome(True, "succeeded", "The pre-update hook succeeded.", 0, output)
            if result["refused"]:
                return Outcome(
                    False,
                    "refused",
                    f"The hook runner refused the request: {str(result['refused'])[:300]}",
                    code,
                    output,
                )
            return Outcome(
                False,
                "failed",
                f"The pre-update hook exited with status {code}." + _said(output),
                code,
                output,
            )
        answered = os.path.lexists(taken) or not os.path.lexists(request)
        if not answered and first.expired():
            # Not left for a runner installed later to find and act on.
            with contextlib.suppress(OSError):
                request.unlink()
            return Outcome(
                False,
                "no_runner",
                f"No hook runner picked the request up within {int(first.seconds)} seconds: "
                "check the path unit on the host is enabled and watches the mounted directory.",
            )
        if whole.expired():
            return Outcome(
                False, "timed_out", f"The pre-update hook did not finish within {config.timeout} seconds."
            )
        sleep(poll)
