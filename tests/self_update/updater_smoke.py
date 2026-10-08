"""The updater image, checked from inside a running updater container.

    docker exec -i [-e SHELL_FREE=1] <updater> python - < tests/self_update/updater_smoke.py

Run by the `image` jobs of tests.yml and release.yml after the container has
written its first heartbeat, so what it reads is the image *running*, as
compose runs it, against a fresh `update` volume. Standard library only: it
runs on the image's own Python, which has nothing else.

- **Not root:** uid 65532, with 65532 among its groups (C11).
- **The fresh volume is initialised from the image:** `/update` is
  65532:65532, mode 2770, so the volume is right whichever image mounts it
  first (design notes 6.3).
- **Nothing listens** (E10): no TCP socket in LISTEN, no unconnected UDP
  socket, no listening unix socket, in the container's network namespace.
  `ss` is not in the image, so `/proc/net` is read directly.
- **No shell, package manager or engine CLI** on its PATH, when
  `SHELL_FREE=1` -- the Chainguard runtime. The `python:3.12-slim` bypass
  has a shell and is not asked.

Prints each finding; exits 1 naming every one that failed.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys

RUNS_AS = 65532
MOUNT = "/update"
#: TCP state 0A is LISTEN (include/net/tcp_states.h).
TCP_LISTEN = "0A"
#: __SO_ACCEPTCON in /proc/net/unix's Flags column: a listening socket.
UNIX_ACCEPTCON = 0x10000
TOOLS = ("sh", "bash", "ash", "busybox", "apk", "apt-get", "dnf", "pip", "docker", "podman")


def _rows(name: str) -> list[list[str]]:
    try:
        with open(f"/proc/net/{name}") as f:
            return [line.split() for line in f.read().splitlines()[1:] if line.strip()]
    except FileNotFoundError:  # no IPv6 in this namespace
        return []


def listeners() -> list[str]:
    found = []
    for name in ("tcp", "tcp6"):
        found += [f"{name} {row[1]}" for row in _rows(name) if row[3] == TCP_LISTEN]
    for name in ("udp", "udp6"):
        # An unconnected UDP socket has an all-zero remote address: it is bound
        # to receive from anybody, which is what listening means for UDP.
        found += [f"{name} {row[1]}" for row in _rows(name) if set(row[2].split(":")[0]) == {"0"}]
    for row in _rows("unix"):
        if int(row[3], 16) & UNIX_ACCEPTCON:
            found.append("unix " + (row[7] if len(row) > 7 else "(unnamed)"))
    return found


def problems() -> list[str]:
    found = []
    if os.getuid() != RUNS_AS:
        found.append(f"runs as uid {os.getuid()}, not {RUNS_AS}")
    if RUNS_AS not in os.getgroups() and os.getgid() != RUNS_AS:
        found.append(f"group {RUNS_AS} is not among its groups {os.getgroups()}")
    st = os.stat(MOUNT)
    seen = (st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode))
    if seen != (RUNS_AS, RUNS_AS, 0o2770):
        found.append(f"{MOUNT} is {seen[0]}:{seen[1]} {oct(seen[2])}, not {RUNS_AS}:{RUNS_AS} 0o2770")
    found += [f"listens: {where}" for where in listeners()]
    if os.environ.get("SHELL_FREE") == "1":
        # pip included: the venv is on PATH, and the Dockerfile removes it.
        found += [f"{tool} is on PATH at {shutil.which(tool)}" for tool in TOOLS if shutil.which(tool)]
    return found


if __name__ == "__main__":
    if os.environ.get("SMOKE_LISTENERS_ONLY") == "1":
        # The end-to-end scenarios' E10, in whichever updater runs now and as
        # whichever user the engine needs: only what listens, as JSON.
        import json

        print(json.dumps(listeners()))
        sys.exit(0)
    print(f"uid {os.getuid()} gid {os.getgid()} groups {sorted(os.getgroups())}")
    print(f"listening: {listeners() or 'nothing'}")
    bad = problems()
    for line in bad:
        print("FAIL", line)
    sys.exit(1 if bad else 0)
