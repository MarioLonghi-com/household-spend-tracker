"""The end-to-end scenarios' world: one engine, one compose project, release A running.

The scenarios (`scenarios.py`) say *what* must hold; this is everything they
act through:

- `Leg`: which engine, how compose is run on it, how it is restarted, and the
  updater's user and socket group there (6.3, S12, S17, S18);
- `Stack`: the compose project in its own directory -- the compose file, `.env`,
  `pin/`, and `ci-trust.json`, the table the CI updater verifies against --
  brought up at release A and seeded, then torn down without trace;
- `Volumes`: the `update` and ledger volumes, read and written through a
  helper container of A's image, so the same code serves an engine whose
  volumes the driver cannot open directly (rootless Podman's are owned by
  sub-uids, a dind engine's are inside its container);
- requests, written as the app writes them (uid 65532, 0660), and the
  history, journal and heartbeat files read back;
- health and HTTP *from where requests arrive*: the published loopback port,
  or, in the sidecar layout, from inside the stand-in that holds the
  namespace -- where `tailscale serve` would connect.

The updater runs in its container throughout -- the CI updater image, which
is the release's real updater image plus the test-only trust policy -- as the
compose file starts it.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from tests.self_update import api

ROOT = Path(__file__).resolve().parents[2]
PROJECT = "spend-tracker"
APP_REPO = "ghcr.io/mariolonghi-com/household-spend-tracker"
UPDATER_REPO = APP_REPO + "-updater"
PROJECT_LABELS = ("com.docker.compose.project", "io.podman.compose.project")
SERVICE_LABELS = ("com.docker.compose.service", "io.podman.compose.service")
ONEOFF_LABEL = "com.docker.compose.oneoff"
APP_UID = 65532
PORT = 8848
HELPER = "st-ci-volumes"
UPDATE_VOLUME = f"{PROJECT}_update"
LEDGER_VOLUME = f"{PROJECT}_ledger"
LEDGER = "/l/spendtracker.sqlite3"
TERMINAL = (
    "succeeded",
    "rolled_back",
    "not_started",
    "refused",
    "needs_recovery",
    "recovered",
    "left_for_operator",
)
#: The stand-in for the Tailscale sidecar: busybox `wget` for the same
#: healthcheck, and Python to make the driver's requests from inside the
#: namespace. Pinned by digest, like every image a workflow pulls.
STANDIN_IMAGE = "python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4"

#: Crockford's base 32, as the app makes a recovery code (R20).
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class Failure(AssertionError):
    pass


# --------------------------------------------------------------------------- #
# What a leg is
# --------------------------------------------------------------------------- #


@dataclass
class Leg:
    name: str
    #: The engine socket on this machine, which the updater mounts.
    socket: str
    #: How compose runs here: `docker compose` or `podman-compose`.
    compose: list[str]
    #: Restarts the engine (E6). None where the leg cannot.
    restart: list[str] | None
    updater_user: str = "65532:65532"
    socket_gid: str = "0"
    layout: str = "loopback"
    #: Scenario -> why this leg does not run it.
    skips: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# The recovery code (R10, R20), as app.services.updates makes it
# --------------------------------------------------------------------------- #


def new_code() -> str:
    symbols = "".join(secrets.choice(CROCKFORD) for _ in range(28))
    return "-".join(symbols[i : i + 4] for i in range(0, 28, 4))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii").rstrip("=")


def hash_code(code: str) -> str:
    text = code.strip().upper().replace("-", "").replace(" ", "")
    text = text.replace("O", "0").replace("I", "1").replace("L", "1")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        text.encode("ascii"), salt=salt, n=1 << 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32
    )
    return f"scrypt$ln=15,r=8,p=1${_b64(salt)}${_b64(digest)}"


def iso(t: float | None = None) -> str:
    t = time.time() if t is None else t
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1e6):06d}Z"


# --------------------------------------------------------------------------- #
# HTTP, from wherever requests arrive
# --------------------------------------------------------------------------- #

#: One request; argv[1] is JSON {method, url, host, form, cookie}. Prints
#: JSON {status, headers, body}. Standard library only, so it runs in the
#: stand-in as well as here. Never follows a redirect: the 303 is the answer.
HTTP_SCRIPT = r"""
import http.client, json, sys, urllib.parse
a = json.loads(sys.argv[1])
u = urllib.parse.urlsplit(a["url"])
c = http.client.HTTPConnection(u.hostname, u.port, timeout=a.get("timeout", 10))
h = {"Host": a["host"]}
body = None
if a.get("form") is not None:
    body = urllib.parse.urlencode(a["form"])
    h["Content-Type"] = "application/x-www-form-urlencoded"
if a.get("cookie"):
    h["Cookie"] = a["cookie"]
try:
    c.request(a["method"], u.path or "/", body=body, headers=h)
    r = c.getresponse()
    out = {"status": r.status, "headers": [[k, v] for k, v in r.getheaders()], "body": r.read().decode("utf-8", "replace")}
except OSError as e:
    out = {"status": 0, "headers": [], "body": str(e)}
print(json.dumps(out))
"""


# --------------------------------------------------------------------------- #
# The volumes, through a helper container
# --------------------------------------------------------------------------- #

VOLUME_SCRIPT = r"""
import json, os, sqlite3, sys
op, args = sys.argv[1], json.loads(sys.argv[2])
def out(x): print(json.dumps(x))
if op == "read":
    try:
        out(json.load(open(args["path"] if args["path"].startswith("/") else "/u/" + args["path"])))
    except (FileNotFoundError, ValueError):
        out(None)
elif op == "list":
    p = args["path"]
    out(sorted(os.listdir(p)) if os.path.isdir(p) else None)
elif op == "exists":
    out(os.path.lexists(args["path"]))
elif op == "request":
    path = "/u/" + args["path"]
    tmp = path + ".ci-tmp"
    with open(tmp, "w") as f:
        f.write(args["text"])
    os.chown(tmp, 65532, 65532)
    os.chmod(tmp, 0o660)
    os.replace(tmp, path)
    out(True)
elif op == "corrupt":
    with open(args["path"], "r+b") as f:
        f.seek(0)
        f.write(b"\0" * 4096)
    out(True)
elif op == "counts":
    c = sqlite3.connect("file:" + args["db"] + "?mode=ro", uri=True)
    names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    counts = {n: c.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    stamp = c.execute("SELECT version_num FROM alembic_version").fetchone()[0] if "alembic_version" in names else None
    out({"counts": counts, "stamp": stamp})
elif op == "query":
    c = sqlite3.connect("file:" + args["db"] + "?mode=ro", uri=True)
    out([list(r) for r in c.execute(args["sql"], args.get("params", []))])
elif op == "columns":
    c = sqlite3.connect("file:" + args["db"] + "?mode=ro", uri=True)
    out([r[1] for r in c.execute(f'PRAGMA table_info("{args["table"]}")')])
elif op == "write":
    c = sqlite3.connect(args["db"])
    c.execute(args["sql"], args.get("params", []))
    c.commit()
    out(True)
"""


class Volumes:
    """The `update` volume at `/u` and the ledger at `/l`, in a helper of A's image, as root."""

    def __init__(self, engine: api.Engine, image: str) -> None:
        self.engine = engine
        self.image = image

    def start(self) -> None:
        self.engine.run(
            HELPER,
            self.image,
            ["-c", "import time\nwhile True: time.sleep(3600)"],
            entrypoint=["python"],
            user="0:0",
            binds=[f"{UPDATE_VOLUME}:/u", f"{LEDGER_VOLUME}:/l"],
        )

    def stop(self) -> None:
        self.engine.remove(HELPER)

    def _do(self, op: str, **args: object):
        for attempt in range(30):
            try:
                code, output = self.engine.exec(
                    HELPER, ["python", "-c", VOLUME_SCRIPT, op, json.dumps(args)]
                )
                break
            except (api.Unreachable, api.Failed) as e:
                # The engine restarting (E6), or the helper with it.
                if attempt == 29:
                    raise
                print(f"   (volume helper: {e}; again)", flush=True)
                time.sleep(2)
                with contextlib.suppress(api.Unreachable, api.Failed):
                    if not self.engine.inspect(HELPER)["State"]["Running"]:
                        self.engine.start(HELPER)
        if code != 0:
            raise Failure(f"volume helper {op} {args}: exit {code}: {output[-2000:]}")
        return json.loads(output.strip().splitlines()[-1])

    def read(self, rel: str):
        """A JSON file of the `update` volume, by its path there; None when absent or unreadable."""
        return self._do("read", path=rel)

    def read_abs(self, path: str):
        """A JSON file anywhere in the helper (`/u/...`, `/l/...`)."""
        return self._do("read", path=path)

    def listdir(self, path: str) -> list[str] | None:
        return self._do("list", path=path)

    def exists(self, path: str) -> bool:
        return bool(self._do("exists", path=path))

    def request(self, doc: dict, rel: str = "request.json") -> None:
        self._do("request", path=rel, text=json.dumps(doc))

    def corrupt(self, path: str) -> None:
        self._do("corrupt", path=path)

    def ledger(self, db: str = LEDGER) -> dict:
        """`{"counts": {table: rows}, "stamp": revision}` of the live ledger (or a backup's copy)."""
        return self._do("counts", db=db)

    def query(self, sql: str, params: list | None = None, db: str = LEDGER) -> list:
        return self._do("query", db=db, sql=sql, params=params or [])

    def columns(self, table: str, db: str = LEDGER) -> list[str]:
        return self._do("columns", db=db, table=table)

    def write(self, sql: str, params: list | None = None, db: str = LEDGER) -> None:
        self._do("write", db=db, sql=sql, params=params or [])

    def backups(self) -> list[str]:
        return [n for n in self.listdir("/l/backups") or [] if not n.startswith(".")]


# --------------------------------------------------------------------------- #
# The stack
# --------------------------------------------------------------------------- #


class Stack:
    def __init__(self, leg: Leg, engine: api.Engine, project_dir: Path, staged: Path) -> None:
        self.leg = leg
        self.engine = engine
        self.projects = project_dir
        self.dir = project_dir / "run-0"
        self.runs = 0
        self.staged = staged
        self.table = json.loads((staged / "ci-trust.json").read_text())
        manifest = json.loads((staged / "releases.json").read_text())
        self.releases: dict[str, dict] = manifest["releases"]
        self.sentinel = manifest["sentinel"]
        self.probe_column = tuple(manifest["probe_column"])
        self.app_name = ""
        self.updater_name = ""
        self.standin = ""
        self.vol: Volumes | None = None

    # ---- releases --------------------------------------------------------- #

    def app_ref(self, version: str) -> str:
        return f"{APP_REPO}@{self.releases[version]['app']}"

    def updater_ref(self, version: str) -> str:
        return f"{UPDATER_REPO}@{self.releases[version]['updater']}"

    def head(self, version: str) -> str:
        return self.releases[version]["chain"][-1]

    def image_id(self, ref: str) -> str:
        return str(self.engine.image(ref)["Id"])

    # ---- containers ------------------------------------------------------- #

    def project_containers(self) -> list[dict]:
        seen: dict[str, dict] = {}
        for key in PROJECT_LABELS:
            for c in self.engine.containers(f"{key}={PROJECT}"):
                seen[c["Id"]] = c
        return list(seen.values())

    @staticmethod
    def service(c: dict) -> str | None:
        labels = c.get("Labels") or {}
        return next((labels[k] for k in SERVICE_LABELS if labels.get(k)), None)

    @staticmethod
    def name(c: dict) -> str:
        names = c.get("Names") or [""]
        return str(names[0]).lstrip("/")

    def running(self, service: str) -> list[dict]:
        """The project's running, non-one-off containers of `service`."""
        return [
            c
            for c in self.project_containers()
            if self.service(c) == service
            and (c.get("Labels") or {}).get(ONEOFF_LABEL) != "True"
            and c.get("State") == "running"
        ]

    # ---- lifecycle -------------------------------------------------------- #

    def compose(self, *args: str, env: dict | None = None) -> None:
        cmd = [*self.leg.compose, *args]
        print("+", " ".join(cmd), flush=True)
        subprocess.run(cmd, cwd=self.dir, check=True, env={**os.environ, **(env or {})})

    def reset(self) -> None:
        """Nothing of a previous scenario left: containers, volumes, the pin."""
        with contextlib.suppress(api.Failed):
            self.engine.remove(HELPER)
        for c in self.project_containers():
            self.engine.remove(c["Id"])
        for c in self.engine.containers():
            if self.name(c).startswith((f"{PROJECT}-", f"{PROJECT}_")):
                self.engine.remove(c["Id"])
        for v in self.engine.volumes():
            if str(v.get("Name", "")).startswith((f"{PROJECT}_", f"{PROJECT}-")):
                self.engine.remove_volume(v["Name"])
        for net in ("default",):
            with contextlib.suppress(api.Failed):
                self.engine.call("DELETE", f"/networks/{PROJECT}_{net}")
        # A new project directory for each stack: what the updater wrote into
        # the last one (`pin/`, as uid 65532 on a rootful engine) need not be
        # removable by the driver. The compose project's name is fixed, so the
        # directory's own name does not matter to compose.
        self.runs += 1
        self.dir = self.projects / f"run-{self.runs}"
        self.dir.mkdir(parents=True, exist_ok=True)

    def write_project(self, version: str) -> None:
        text = sidecar_compose() if self.leg.layout == "sidecar" else (ROOT / "compose.yaml").read_text()
        (self.dir / "compose.yaml").write_text(text)
        (self.dir / "ci-trust.json").write_text(json.dumps(self.table))
        env = [
            f"SPENDTRACKER_VERSION={version}",
            f"SPENDTRACKER_ENGINE_SOCKET={self.leg.socket}",
            f"SPENDTRACKER_UPDATER_USER={self.leg.updater_user}",
            f"SPENDTRACKER_SOCKET_GID={self.leg.socket_gid}",
        ]
        if self.leg.layout == "sidecar":
            env.append(f"SPENDTRACKER_PUBLIC_URL=http://localhost:{PORT}")
        envfile = self.dir / ".env"
        envfile.write_text("\n".join(env) + "\n")
        # R34: the project directory and `.env` writable by the updater's
        # group -- the socket's -- where it does not run as the user (rootful).
        if self.leg.socket_gid != "0":
            gid = int(self.leg.socket_gid)
            for path in (self.dir, envfile):
                with contextlib.suppress(PermissionError):
                    os.chown(path, -1, gid)
            os.chmod(self.dir, 0o2775)
        envfile.chmod(0o664)

    def up(self, version: str = "98.0.0", seed: bool = True) -> None:
        self.reset()
        self.write_project(version)
        self.compose("up", "-d", "--no-build")
        apps = self.wait_for(lambda: self.running("app"), "the app container", 120)
        self.app_name = self.name(apps[0])
        if self.leg.layout == "sidecar":
            self.standin = self.name(
                self.wait_for(lambda: self.running("tailscale"), "the stand-in", 60)[0]
            )
        self.wait_for(lambda: self.health().get("version") == version, f"health to say {version}", 180)
        if seed:
            code, output = self.engine.exec(
                self.app_name, ["python", "-m", "scripts.seed_demo", "--months", "1"]
            )
            if code != 0:
                raise Failure(f"the demo seed failed: {output[-2000:]}")
        self.vol = Volumes(self.engine, self.app_ref(version))
        self.vol.start()
        beat = self.wait_for(
            lambda: (lambda b: b if b and b.get("socket") == "ok" and b.get("role") == "current" else None)(
                self.vol.read("updater.json")
            ),
            "the updater's first heartbeat",
            120,
        )
        self.updater_name = str(beat.get("container") or "")
        print(f"   up: {self.app_name} at {version}, updater {self.updater_name} ({beat.get('engine')} "
              f"{beat.get('engine_version')}, API {beat.get('api_version')})", flush=True)  # fmt: skip

    def down(self) -> None:
        self.reset()

    # ---- waiting ---------------------------------------------------------- #

    @staticmethod
    def wait_for(probe, what: str, seconds: float, every: float = 1.0):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            try:
                last = probe()
            except (api.Unreachable, api.Failed, Failure) as e:
                last = None
                print(f"   (waiting for {what}: {e})", flush=True)
            if last:
                return last
            time.sleep(every)
        raise Failure(f"timed out after {seconds:.0f} s waiting for {what}")

    # ---- HTTP from where requests arrive ---------------------------------- #

    def http(self, method: str, path: str, form: dict | None = None, cookie: str | None = None) -> dict:
        args = json.dumps(
            {
                "method": method,
                "url": f"http://127.0.0.1:{PORT}{path}",
                "host": f"localhost:{PORT}",
                "form": form,
                "cookie": cookie,
            }
        )
        if self.leg.layout == "sidecar":
            code, output = self.engine.exec(self.standin, ["python", "-c", HTTP_SCRIPT, args])
        else:
            done = subprocess.run([sys.executable, "-c", HTTP_SCRIPT, args], capture_output=True, text=True)
            code, output = done.returncode, done.stdout + done.stderr
        if code != 0:
            return {"status": 0, "headers": [], "body": output}
        return json.loads(output.strip().splitlines()[-1])

    def health(self) -> dict:
        answer = self.http("GET", "/api/health")
        if answer["status"] != 200:
            return {"status_code": answer["status"]}
        try:
            return json.loads(answer["body"])
        except ValueError:
            return {"status_code": answer["status"]}

    # ---- requests --------------------------------------------------------- #

    @staticmethod
    def base(kind: str, created: float | None = None) -> dict:
        return {
            "protocol": 1,
            "id": str(uuid.uuid4()),
            "kind": kind,
            "created_at": iso(created),
            "requested_by": "usr_ci",
        }

    def send(self, doc: dict) -> None:
        assert self.vol is not None
        self.wait_for(lambda: not self.vol.exists("/u/request.json"), "the request slot to be free", 120)
        self.vol.request(doc)

    def outcome(self, rid: str, seconds: float = 900) -> dict:
        assert self.vol is not None
        return self.wait_for(
            lambda: (lambda r: r if r and r.get("state") in TERMINAL else None)(
                self.vol.read(f"history/{rid}.json")
            ),
            f"the outcome of {rid}",
            seconds,
            every=2,
        )

    def prepare(self, frm: str, to: str) -> dict:
        req = {**self.base("prepare"), "from_version": frm, "to_version": to}
        self.send(req)
        record = self.outcome(req["id"], 600)
        if record.get("state") != "succeeded":
            raise Failure(f"prepare {frm} -> {to}: {record.get('state')}: {record.get('sentence')}")
        report = self.vol.read(f"prepared/{req['id']}.json")
        if not report:
            raise Failure("prepare succeeded and wrote no report")
        return report

    def apply_request(self, report: dict, code: str | None = None) -> dict:
        lossy = sorted(
            str(m.get("revision"))
            for m in report.get("pending") or []
            if isinstance(m, dict) and m.get("reversible") != "clean"
        )
        return {
            **self.base("apply"),
            "from_version": report["from_version"],
            "to_version": report["to_version"],
            "prepared_id": report["id"],
            "digest": report["digest"],
            "updater_digest": report["updater_digest"],
            "accepted_lossy": lossy,
            "recovery_hash": hash_code(code or new_code()),
        }

    def update(self, frm: str, to: str, code: str | None = None, during=None) -> tuple[dict, dict]:
        """Prepare and apply `frm` -> `to`. `during(apply_id)` runs once the apply is sent.

        Returns (the apply request, its history record)."""
        report = self.prepare(frm, to)
        req = self.apply_request(report, code)
        started = time.monotonic()
        self.send(req)
        if during is not None:
            during(req["id"])
        record = self.outcome(req["id"])
        print(f"   apply {frm} -> {to}: {record.get('state')} in {time.monotonic() - started:.0f} s: "
              f"{record.get('sentence')}", flush=True)  # fmt: skip
        return req, record

    def journal(self, rid: str) -> dict:
        return self.vol.read(f"journal/{rid}.json") or {}

    def wait_step(self, rid: str, steps: tuple[str, ...], seconds: float = 600) -> str:
        return self.wait_for(
            lambda: (lambda s: s if s in steps else None)(self.journal(rid).get("step")),
            f"step {'/'.join(steps)} of {rid}",
            seconds,
            every=0.25,
        )

    def dump_all(self) -> None:
        ids = []
        with contextlib.suppress(Exception):
            ids = [n[: -len(".json")] for n in self.vol.listdir("/u/journal") or [] if n.endswith(".json")]
        self.dump(*ids)

    def dump(self, *ids: str) -> None:
        """What a failure needs read: records, containers, logs."""
        if self.vol is not None:
            for rid in ids:
                for kind in ("history", "journal", "prepared"):
                    with contextlib.suppress(Exception):
                        doc = self.vol.read(f"{kind}/{rid}.json")
                        if doc is not None:
                            print(f"--- {kind}/{rid}.json\n{json.dumps(doc, indent=2)[:6000]}")
            with contextlib.suppress(Exception):
                print("--- updater.json\n" + json.dumps(self.vol.read("updater.json"), indent=2))
        with contextlib.suppress(Exception):
            for c in self.engine.containers():
                print(f"--- {self.name(c)}  {c.get('Image')}  {c.get('Status')}")
                if self.name(c).startswith((PROJECT, "st-ci")) and self.name(c) != HELPER:
                    print(self.engine.logs(c["Id"], 40))


def sidecar_compose() -> str:
    """deploy/tailnet/compose.yaml with the Tailscale container replaced by a stand-in.

    The stand-in keeps the service's name, its hostname, its restart policy and
    **its healthcheck, verbatim**; it has no Tailscale, no state, no device and
    no capability. Requests arrive where `tailscale serve` would make them:
    from inside the stand-in, to the namespace's loopback (`Stack.http`).
    Everything else in the file -- the app in the stand-in's namespace, the
    updater -- is the real file's.
    """
    import yaml

    doc = yaml.safe_load((ROOT / "deploy" / "tailnet" / "compose.yaml").read_text())
    real = doc["services"]["tailscale"]
    doc["services"]["tailscale"] = {
        "image": STANDIN_IMAGE,
        "hostname": real.get("hostname"),
        "restart": real.get("restart"),
        "healthcheck": real["healthcheck"],
        "command": ["python", "-c", "import time\nwhile True: time.sleep(3600)"],
    }
    doc["volumes"].pop("ts-state", None)
    return yaml.safe_dump(doc, sort_keys=False)
