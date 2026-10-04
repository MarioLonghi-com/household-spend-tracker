"""What the container runs, and the one guard it has to carry.

**It does not migrate unattended.** A container restart is not a decision to
change a schema, and a restart policy of `unless-stopped` would otherwise make
every crash a fresh chance to run a migration nobody was watching. So:

- `SPENDTRACKER_AUTO_MIGRATE=1` runs `alembic upgrade head` and then serves.
  For a first run against an empty volume, and for anybody who has decided they
  want it.
- Unset -- the default -- serves without migrating. `app/schema_check.py` then
  refuses to boot against a database that is behind or ahead, and says which
  revision each side is at. That refusal is exactly the right behaviour for a
  restart, and it is free: the guard already exists.

The backup rule does not change inside a container. `docker compose exec` into
it and run `python -m scripts.backup` **before** you pull a new image, or the
volume's contents are the only copy you have.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

#: uid the image runs as. Only used to say a useful sentence, never to decide.
RUNS_AS = 65532


def _data_dir_is_writable() -> str | None:
    """None when the data directory can be written, or a sentence saying why not.

    Checked here because the failure is otherwise a `PermissionError` traceback
    out of `_load_or_create_secret_key`, three frames into config import, and
    the cause is not in it.

    **Docker initialises a fresh named volume from the image's content at that
    path, ownership included.** The image now carries
    `/var/lib/spend-tracker` owned by 65532 so a new volume is too -- but a
    volume created by an *older* image, or by a failed first run, was made
    root-owned and keeps that ownership for ever. `docker compose up` will not
    fix it and will not mention it. The only sentence worth printing is the one
    that says how to.
    """
    where = pathlib.Path(os.environ.get("SPENDTRACKER_DATA_DIR") or "/var/lib/spend-tracker")
    try:
        where.mkdir(parents=True, exist_ok=True)
        probe = where / ".write-probe"
        probe.touch()
        probe.unlink()
    except OSError as denied:
        try:
            owner = where.stat().st_uid
        except OSError:
            owner = "unknown"
        return (
            f"{where} is not writable by uid {os.getuid()} ({denied.strerror}).\n"
            f"It is owned by uid {owner}; this image runs as {RUNS_AS}.\n"
            "\n"
            "A named volume keeps the ownership it was created with, so a volume\n"
            "made by an earlier build of this image stays root-owned however many\n"
            "times you rebuild.\n"
            "\n"
            "If the ledger in it does not matter yet -- a first run that failed:\n"
            "    docker compose down -v && docker compose up -d\n"
            "\n"
            "If it does, fix the ownership in place instead:\n"
            "    docker compose run --rm --user root app --fix-ownership"
        )
    return None


def fix_ownership() -> int:
    """`--fix-ownership`: chown the data directory to the uid this image runs as.

    Here rather than in the message as a `chown` one-liner because **the
    runtime image has no `chown`**. It has no shell and no coreutils either --
    that is most of the point of the Chainguard base -- so
    `--entrypoint chown` fails with "executable file not found in $PATH" and
    the person following the instructions is now debugging the instructions.

    Python is in the image by definition. So the recovery is a flag on the
    entrypoint, run as root for the one command that needs it:

        docker compose run --rm --user root app --fix-ownership

    It changes ownership and exits. It never goes on to start the server,
    because that invocation is running as root and serving from it would undo
    the reason the image drops privileges at all.
    """
    where = pathlib.Path(os.environ.get("SPENDTRACKER_DATA_DIR") or "/var/lib/spend-tracker")
    if os.getuid() != 0:
        print(
            f"--fix-ownership changes who owns {where}, which needs root.\n"
            "Add --user root:\n"
            "    docker compose run --rm --user root app --fix-ownership",
            file=sys.stderr,
        )
        return 1

    changed = 0
    try:
        for path in [where, *where.rglob("*")]:
            os.chown(path, RUNS_AS, RUNS_AS)
            changed += 1
    except OSError as denied:
        print(f"could not change {where}: {denied}", file=sys.stderr)
        return 1

    print(f"{changed} paths under {where} now belong to {RUNS_AS}:{RUNS_AS}.")
    print("Start it again:  docker compose up -d")
    return 0


#: What uvicorn binds when `SPENDTRACKER_HOST` is unset.
DEFAULT_HOST = "0.0.0.0"


def bind_host() -> str:
    """The address uvicorn listens on: `SPENDTRACKER_HOST`, else 0.0.0.0.

    **0.0.0.0 is right for compose.yaml and wrong for a Tailscale sidecar**, and
    the difference is where the exposure boundary sits.

    Under compose.yaml the boundary is the *published* port, `127.0.0.1:8848`
    on the host, so inside the container everything-interfaces is the only
    binding that lets the publish reach it at all.

    Under deploy/tailnet/compose.yaml there is no published port. The app
    shares the sidecar's network namespace, and that namespace *is* the tailnet
    node: 0.0.0.0 there puts plain HTTP on `<node>.ts.net:8848` right next to
    the HTTPS that `tailscale serve` terminates on 443. A tailnet ACL that
    allows only tcp:443 closes it, but that is one policy edit away from
    reopening. Binding 127.0.0.1 closes it in the app, where it stays closed:
    `tailscale serve` proxies to loopback inside the same namespace, so that is
    the only listener it needs.
    """
    return (os.environ.get("SPENDTRACKER_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST


def main() -> int:
    if "--fix-ownership" in sys.argv[1:]:
        return fix_ownership()

    problem = _data_dir_is_writable()
    if problem is not None:
        print(problem, file=sys.stderr, flush=True)
        return 1

    if (os.environ.get("SPENDTRACKER_AUTO_MIGRATE") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        print("SPENDTRACKER_AUTO_MIGRATE is on: running alembic upgrade head", flush=True)
        done = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"])
        if done.returncode != 0:
            print(
                "The migration failed, so the app is not being started against a "
                "half-migrated database.",
                file=sys.stderr, flush=True,
            )
            return done.returncode

    port = (os.environ.get("PORT") or "8848").strip()
    host = bind_host()
    os.execvp(
        sys.executable,
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", host, "--port", port,
        ],
    )


if __name__ == "__main__":
    sys.exit(main())
