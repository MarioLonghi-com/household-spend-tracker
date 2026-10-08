"""Build the releases the end-to-end scenarios update between (design notes 15.5).

    python -m tests.self_update.stage registry --out DIR [--publish 0.0.0.0:443] [--network NET]
    python -m tests.self_update.stage build --out DIR --base <commit> [--only A,B]

Runs on the **build host**: a machine with the `docker` CLI and git -- the
runner, or Docker Desktop for a local run. The engine under test may be a
different one (rootless Podman, a dind container); it reaches what is built
here only through the registry.

`registry` starts two registries over one storage volume: plain HTTP on
`127.0.0.1:5000`, which the build host pushes to (a loopback registry needs
no certificate on any engine), and TLS for `ghcr.io` with a certificate made
here (`DIR/ghcr.crt`), which the engine under test pulls from once its
`/etc/hosts` and its certificate directory say so. A manifest's digest is
the same under either name, which is what lets the updater pull
`ghcr.io/...@sha256:...` from a registry that is not ghcr.io.

`build` makes every release the scenarios use, each as a release would be
built -- the Dockerfile, release labels (bare `X.Y.Z` and the revision), the
updater's own `--target updater` -- and then the **CI updater** on top of each
release's updater image (`ci-updater.Dockerfile`). It writes:

- `DIR/ci-trust.json`: the trust table `CiTrust` reads, every digest pushed;
- `DIR/releases.json`: per version, its digests, revision, migration chain,
  and what kind of release it is.

The releases, oldest first:

| Version | What | Used by |
|---|---|---|
| A `98.0.0` | the merge base | every scenario's starting point |
| B `99.0.0` | this change | E1, E5-E7, E9-E13 |
| B1 `99.0.1` | B plus a migration that inserts a sentinel row, commits, and raises | E3, E8 |
| B2 `99.0.2` | B plus a migration adding a column, and `/api/health` answering 500 | E4, E8 |
| B3 `99.0.3` | B, with an updater that fails its self-check as a successor | E11, E13 |
| B4 `99.1.0` | B plus a migration (the release E2 skips) | E2 |
| C `100.0.0` | B4 plus a migration | E2, E14 |

The variants are B's image with a few files copied over it (`FROM` B), not
branches: the version in `app/__init__.py`, a migration, `app/main.py`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_REPO = "ghcr.io/mariolonghi-com/household-spend-tracker"
UPDATER_REPO = APP_REPO + "-updater"
PUSH = "localhost:5000"
REGISTRY_IMAGE = "registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
VERSION_LABEL = "org.opencontainers.image.version"
REVISION_LABEL = "org.opencontainers.image.revision"

A, B, B1, B2, B3, B4, C = "98.0.0", "99.0.0", "99.0.1", "99.0.2", "99.0.3", "99.1.0", "100.0.0"
#: What a sentinel row looks like (E3): a login attempt nobody made.
SENTINEL_EMAIL = "ci-sentinel@example.test"
#: The column B2 adds (E4).
PROBE_COLUMN = ("households", "ci_probe")
BREAK_SUCCESSOR = "SPENDTRACKER_CI_BREAK_SUCCESSOR"


def sh(*args: str, cwd: Path | None = None, capture: bool = False, stdin: str | None = None) -> str:
    print("+", " ".join(args), flush=True)
    done = subprocess.run(args, cwd=cwd, check=True, text=True, input=stdin, capture_output=capture)
    return done.stdout if capture else ""


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def registry(out: Path, publish: str, network: str | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    key, crt = out / "ghcr.key", out / "ghcr.crt"
    sh(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-subj", "/CN=ghcr.io", "-addext", "subjectAltName=DNS:ghcr.io",
        "-keyout", str(key), "-out", str(crt),
    )  # fmt: skip
    key.chmod(0o644)
    for name in ("st-ci-registry", "st-ci-registry-push"):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    sh("docker", "volume", "create", "st-ci-registry")
    sh("docker", "pull", "-q", REGISTRY_IMAGE)
    certs = ["-v", f"{out}:/certs:ro"]
    tls = [
        "-e", "REGISTRY_HTTP_ADDR=0.0.0.0:443",
        "-e", "REGISTRY_HTTP_TLS_CERTIFICATE=/certs/ghcr.crt",
        "-e", "REGISTRY_HTTP_TLS_KEY=/certs/ghcr.key",
    ]  # fmt: skip
    where = ["-p", f"{publish}:443"] if publish else []
    net = ["--network", network] if network else []
    # `always`: an engine restart (E6, on the docker leg) must not take the
    # registry down with it.
    sh(
        "docker", "run", "-d", "--restart", "always", "--name", "st-ci-registry", *net, *where,
        "-v", "st-ci-registry:/var/lib/registry", *certs, *tls, REGISTRY_IMAGE,
    )  # fmt: skip
    sh(
        "docker", "run", "-d", "--restart", "always", "--name", "st-ci-registry-push",
        "-p", "127.0.0.1:5000:5000", "-v", "st-ci-registry:/var/lib/registry", REGISTRY_IMAGE,
    )  # fmt: skip
    for _ in range(60):
        ok = (
            subprocess.run(["curl", "-fsS", "http://127.0.0.1:5000/v2/"], capture_output=True).returncode
            == 0
        )
        if ok:
            break
        time.sleep(1)
    else:
        sys.exit("the push registry did not answer")
    ip = sh(
        "docker",
        "inspect",
        "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
        "st-ci-registry",
        capture=True,
    ).split()
    (out / "registry-ip").write_text((ip[0] if ip else "") + "\n")
    print(f"registries up; TLS registry at {ip[0] if ip else '?'} (container), certificate {crt}")


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def set_version(text: str, version: str) -> str:
    new, n = re.subn(r'^__version__ = ".*"$', f'__version__ = "{version}"', text, flags=re.M)
    if n != 1:
        raise SystemExit("app/__init__.py has no single __version__ line")
    return new


def revision_of(tree: Path) -> str:
    return sh("git", "-C", str(tree), "rev-parse", "HEAD", capture=True).strip()


def labels(version: str, revision: str) -> list[str]:
    return ["--label", f"{VERSION_LABEL}={version}", "--label", f"{REVISION_LABEL}={revision}"]


def build_release(tree: Path, version: str) -> tuple[str, str]:
    """The app and the real updater image from `tree`, as a release builds them. Local tags."""
    init = tree / "app" / "__init__.py"
    original = init.read_text()
    revision = revision_of(tree)
    try:
        sh(sys.executable, "-m", "scripts.build_stamp", cwd=tree)
        init.write_text(set_version(original, version))
        app = f"spend-tracker-ci:{version}"
        sh("docker", "build", "-q", "-t", app, *labels(version, revision), str(tree))
        base = f"spend-tracker-ci-base-updater:{version}"
        sh(
            "docker",
            "build",
            "-q",
            "--target",
            "updater",
            "-t",
            base,
            *labels(version, revision),
            str(tree),
        )
    finally:
        init.write_text(original)
        (tree / "app" / "build.json").unlink(missing_ok=True)
    return app, base


def ci_updater(base: str, version: str, revision: str) -> str:
    """The CI updater: the release's real updater image plus the test-only trust (ci-updater.Dockerfile)."""
    tag = f"spend-tracker-ci-updater:{version}"
    sh(
        "docker", "build", "-q", "-f", str(ROOT / "tests" / "self_update" / "ci-updater.Dockerfile"),
        "--build-arg", f"UPDATER_IMAGE={base}", "-t", tag, *labels(version, revision), str(ROOT),
    )  # fmt: skip
    return tag


def overlay(
    base: str, tag: str, version: str, revision: str, files: dict[str, str], env: dict | None = None
) -> str:
    """`base` with `files` copied over `/app` (or nothing), labelled as `version`."""
    with tempfile.TemporaryDirectory() as ctx:
        for rel, text in files.items():
            path = Path(ctx) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        lines = [f"FROM {base}"]
        if files:
            lines.append("COPY . /app/")
        for key, value in (env or {}).items():
            lines.append(f"ENV {key}={value}")
        sh(
            "docker",
            "build",
            "-q",
            "-t",
            tag,
            *labels(version, revision),
            "-f",
            "-",
            ctx,
            stdin="\n".join(lines) + "\n",
        )
    return tag


def migration(revision: str, down: str, title: str, reversible: str, body: str) -> str:
    return f'''"""{title}

CI only: built into a release variant by tests/self_update/stage.py, never
into the repository's migrations.

Reversible: {reversible}

Revision ID: {revision}
Revises: {down}
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "{revision}"
down_revision: str | None = "{down}"
branch_labels: str | None = None
depends_on: str | None = None


{body}
'''


FAIL_BODY = f"""def upgrade() -> None:
    # A sentinel row, committed on the driver's own connection before the
    # failure, so only the rollback's restore can take it away again.
    conn = op.get_bind()
    conn.exec_driver_sql(
        "INSERT INTO login_attempts (id, email_canonical, ip, kind, ok, at) "
        "VALUES ('{uuid.UUID(int=1).hex}', '{SENTINEL_EMAIL}', NULL, 'password', 0, '2026-01-01 00:00:00')"
    )
    conn.connection.driver_connection.commit()
    raise RuntimeError("CI: this migration fails after inserting a sentinel row")


def downgrade() -> None:
    pass
"""

COLUMN_BODY = f'''def upgrade() -> None:
    op.add_column("{PROBE_COLUMN[0]}", sa.Column("{PROBE_COLUMN[1]}", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("{PROBE_COLUMN[0]}") as batch:
        batch.drop_column("{PROBE_COLUMN[1]}")
'''


def table_body(name: str) -> str:
    return f'''def upgrade() -> None:
    op.create_table("{name}", sa.Column("id", sa.Integer(), primary_key=True))


def downgrade() -> None:
    op.drop_table("{name}")
'''


HEALTH_FROM = "def health() -> dict:\n"
HEALTH_TO = 'def health():\n    return JSONResponse({"status": "ci-broken"}, status_code=500)\n'


def chain_of(image: str) -> list[str]:
    out = sh(
        "docker", "run", "--rm", "--network", "none", "--entrypoint", "python", image, "-c",
        "import json; from scripts.upgrade import chain; print(json.dumps([m.revision for m in chain()]))",
        capture=True,
    )  # fmt: skip
    return json.loads(out.strip().splitlines()[-1])


def push(local: str, repo: str, version: str) -> str:
    """Push under the registry's loopback name; the digest, which ghcr.io serves identically."""
    remote = f"{PUSH}/{repo.split('/', 1)[1]}:{version}"
    sh("docker", "tag", local, remote)
    sh("docker", "push", "-q", remote)
    digests = json.loads(sh("docker", "inspect", "-f", "{{json .RepoDigests}}", remote, capture=True))
    prefix = remote.rsplit(":", 1)[0] + "@"
    return next(d.split("@", 1)[1] for d in digests if d.startswith(prefix))


def build(out: Path, base_commit: str, only: set[str] | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="st-ci-a-"))
    shutil.rmtree(work)
    sh("git", "-C", str(ROOT), "worktree", "add", "--detach", str(work), base_commit)
    releases: dict[str, dict] = {}
    try:
        a_app, a_upd = build_release(work, A)
        rev_a = revision_of(work)
    finally:
        sh("git", "-C", str(ROOT), "worktree", "remove", "--force", str(work))
    b_app, b_upd = build_release(ROOT, B)
    rev_b = revision_of(ROOT)

    made: dict[str, tuple[str, str, str, str]] = {
        A: (a_app, ci_updater(a_upd, A, rev_a), rev_a, "the merge base"),
        B: (b_app, ci_updater(b_upd, B, rev_b), rev_b, "this change"),
    }
    want = only or {"B1", "B2", "B3", "B4", "C"}
    head_b = chain_of(b_app)[-1]
    init_b = (ROOT / "app" / "__init__.py").read_text()
    main_b = (ROOT / "app" / "main.py").read_text()
    if HEALTH_FROM not in main_b:
        raise SystemExit("app/main.py's health() signature moved; stage.py cannot make B2 answer 500")

    def variant(version: str, files: dict[str, str], what: str, updater_env: dict | None = None) -> None:
        files = {"app/__init__.py": set_version(init_b, version), **files}
        app = overlay(b_app, f"spend-tracker-ci:{version}", version, rev_b, files)
        upd = overlay(made[B][1], f"spend-tracker-ci-updater:{version}", version, rev_b, {}, updater_env)
        made[version] = (app, upd, rev_b, what)

    if "B1" in want:
        rev = "c1fa11ed0001"
        variant(B1, {f"migrations/versions/{rev}_ci_fails_after_a_sentinel.py": migration(
            rev, head_b, "CI: a sentinel row, then a failure", "clean -- it never completes", FAIL_BODY)},
            "a migration that inserts a sentinel row, commits, then fails")  # fmt: skip
    if "B2" in want:
        rev = "c1c01a000002"
        variant(B2, {
            f"migrations/versions/{rev}_ci_a_probe_column.py": migration(
                rev, head_b, "CI: a column B2 adds", "clean -- drops the column", COLUMN_BODY),
            "app/main.py": main_b.replace(HEALTH_FROM, HEALTH_TO, 1),
        }, "a migration adding households.ci_probe, and /api/health answering 500")  # fmt: skip
    if "B3" in want:
        variant(
            B3, {}, "B, with an updater that fails its self-check as a successor", {BREAK_SUCCESSOR: "1"}
        )
    if "B4" in want or "C" in want:
        rev4 = "c1b4000000b4"
        mig4 = migration(
            rev4,
            head_b,
            "CI: the skipped release's table",
            "clean -- drops it",
            table_body("ci_release_b4"),
        )
        variant(B4, {f"migrations/versions/{rev4}_ci_b4.py": mig4}, "B plus a migration (skipped by E2)")
        rev_c = "c1c000000c00"
        mig_c = migration(rev_c, rev4, "CI: C's table", "clean -- drops it", table_body("ci_release_c"))
        variant(C, {f"migrations/versions/{rev4}_ci_b4.py": mig4, f"migrations/versions/{rev_c}_ci_c.py": mig_c},
                "B4 plus a migration")  # fmt: skip

    table: dict[str, dict] = {APP_REPO: {}, UPDATER_REPO: {}}
    for version, (app, upd, rev, what) in made.items():
        app_digest = push(app, APP_REPO, version)
        upd_digest = push(upd, UPDATER_REPO, version)
        table[APP_REPO][version] = {"digest": app_digest, "revision": rev, "size": 0}
        table[UPDATER_REPO][version] = {"digest": upd_digest, "revision": rev, "size": 0}
        releases[version] = {
            "what": what,
            "app": app_digest,
            "updater": upd_digest,
            "revision": rev,
            "chain": chain_of(app),
        }
    (out / "ci-trust.json").write_text(json.dumps(table, indent=2) + "\n")
    (out / "releases.json").write_text(
        json.dumps(
            {"releases": releases, "sentinel": SENTINEL_EMAIL, "probe_column": PROBE_COLUMN}, indent=2
        )
        + "\n"
    )
    print(json.dumps(releases, indent=2))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tests.self_update.stage")
    sub = p.add_subparsers(dest="what", required=True)
    r = sub.add_parser("registry")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--publish", default="", help="where to publish the TLS registry, e.g. 0.0.0.0:443")
    r.add_argument("--network", default=None)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--base", required=True, help="the commit release A is built from")
    b.add_argument("--only", default="", help="variants to build, e.g. B1,C; none for A and B alone")
    args = p.parse_args(argv)
    os.environ.setdefault("DOCKER_BUILDKIT", "1")
    if args.what == "registry":
        registry(args.out.resolve(), args.publish, args.network)
    else:
        only = None if args.only == "" else {x for x in args.only.split(",") if x and x != "none"}
        build(args.out.resolve(), args.base, only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
