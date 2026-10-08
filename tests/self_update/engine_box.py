"""Run scenarios against an engine that runs in a container: dind, or Podman's own image.

    python -m tests.self_update.engine_box --kind docker --image docker:dind \\
        --staged DIR --only E1,E11 [--version-out FILE] [--report FILE]

What the weekly engine canary (`.github/workflows/engine-canary.yml`, C3) and
a local run on Docker Desktop (`tests.self_update.local`) share. On the build
host -- the runner, or a Mac with Docker Desktop -- it:

1. starts the engine container, privileged, on the `st-ci` network beside
   the TLS registry, with `ghcr.io` resolving to that registry
   (`--add-host`) and the checkout and the staged files mounted **at the same
   paths** as on the host, so a bind the engine makes inside resolves to the
   same files the driver writes;
2. trusts the registry's certificate there, installs Python (and
   podman-compose, for Podman), and waits for the engine's socket;
3. runs `tests.self_update.scenarios` **inside** that container, where the
   engine's socket, its loopback (where the app's port is published) and the
   project directory all are.

Nothing here touches the build host's own engine beyond the one container.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOX = "st-ci-engine"
NETWORK = "st-ci"

KINDS = {
    # dind's entry point starts dockerd; no TLS, the unix socket only.
    "docker": {
        "socket": "/var/run/docker.sock",
        "certs": "/etc/docker/certs.d/ghcr.io",
        "compose": "docker compose",
        "setup": "apk add --no-cache python3 >/dev/null",
        "run": [],
        "after": "true",
        "env": ["-e", "DOCKER_TLS_CERTDIR="],
    },
    # Podman's upstream image: the API service as root inside it.
    "podman": {
        "socket": "/run/podman/podman.sock",
        "certs": "/etc/containers/certs.d/ghcr.io",
        "compose": "podman-compose",
        "setup": "dnf -y -q install python3 podman-compose >/dev/null",
        # The socket as podman.socket makes it on a host (SocketMode=0660,
        # S18): `podman system service` alone makes it 0600, root only.
        "run": ["sh", "-c", "mkdir -p /run/podman && exec podman system service --time=0 unix:///run/podman/podman.sock"],
        "after": "chmod 0660 /run/podman/podman.sock",
        # Its storage on a volume: overlay on the container's own overlay root fails.
        "env": ["-v", "st-ci-podman-storage:/var/lib/containers"],
    },
}


def sh(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print("+", " ".join(args), flush=True)
    return subprocess.run(args, check=check, text=True, capture_output=capture)


def inside(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return sh("docker", "exec", BOX, *args, check=check, capture=capture)


def start(kind: str, image: str, staged: Path) -> None:
    k = KINDS[kind]
    sh("docker", "rm", "-f", BOX, check=False, capture=True)
    sh("docker", "network", "create", NETWORK, check=False, capture=True)
    ip = (staged / "registry-ip").read_text().strip()
    if not ip:
        raise SystemExit("the TLS registry has no address; run `stage registry --network st-ci` first")
    sh(
        "docker", "run", "-d", "--privileged", "--name", BOX, "--network", NETWORK,
        "--add-host", f"ghcr.io:{ip}", *k["env"],
        "-v", f"{ROOT}:{ROOT}", "-v", f"{staged}:{staged}", "-w", str(ROOT),
        image, *k["run"],
    )  # fmt: skip
    inside("sh", "-c", f"mkdir -p {k['certs']} && cp {staged}/ghcr.crt {k['certs']}/ca.crt")
    inside("sh", "-c", k["setup"])
    for _ in range(90):
        if inside("test", "-S", k["socket"], check=False).returncode == 0:
            break
        time.sleep(1)
    else:
        sh("docker", "logs", BOX, check=False)
        raise SystemExit(f"{k['socket']} never appeared in {image}")
    inside("sh", "-c", k["after"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tests.self_update.engine_box")
    p.add_argument("--kind", choices=sorted(KINDS), required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--staged", type=Path, required=True)
    p.add_argument("--only", default="E1,E11")
    p.add_argument("--leg", default=None)
    p.add_argument("--version-out", type=Path, default=None)
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--keep", action="store_true", help="leave the engine container running afterwards")
    args = p.parse_args(argv)
    staged = args.staged.resolve()
    k = KINDS[args.kind]
    start(args.kind, args.image, staged)
    project = staged / f"project-{args.kind}"
    # The socket's group, which the updater is given (S12): `docker` in dind.
    gid = inside("stat", "-c", "%g", k["socket"], capture=True).stdout.strip()
    extra = []
    if args.version_out:
        extra += ["--version-out", str(args.version_out.resolve())]
    if args.report:
        extra += ["--report", str(args.report.resolve())]
    try:
        done = inside(
            "python3", "-m", "tests.self_update.scenarios",
            "--staged", str(staged), "--project-dir", str(project),
            "--leg", args.leg or f"{args.kind}:{args.image}", "--socket", k["socket"],
            "--compose", k["compose"], "--only", args.only, "--socket-gid", gid,
            "--skip", "E6=the engine is this container's main process; restarting it ends the container",
            *extra, check=False,
        )  # fmt: skip
    finally:
        if not args.keep:
            sh("docker", "rm", "-f", BOX, check=False, capture=True)
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
