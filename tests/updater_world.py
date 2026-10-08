"""A small simulated installation for the orchestration's tests (design notes 15.1).

The recording fake engine (`tests/updater_fake_engine.py`) keeps containers;
this gives them behaviour. A `World` holds:

- **a ledger**: the stamp it is at (named by the release whose schema it
  holds), its rows, and the backup folders the drill took;
- **the app**, a container of release A's image in the loopback or sidecar
  layout, answering health when -- and only when -- its image's version
  matches the ledger's stamp, as `schema_check` would let it;
- **the one-offs**: the drill (`scripts.upgrade --yes --report`) backs up and
  migrates the ledger and writes its report into the `update` volume; the
  restore puts a backup back; the check, the measurement, the backup search
  and the pruning print what the real programs print;
- **two releases' images** in a fake registry, and a `FakeTrust` that resolves
  and verifies them, and refuses anything else.

Two of everything: two releases (A and B), two images per release (app and
updater), and -- where a test needs it -- two backups and two projects.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from tests.updater_fake_engine import FakeEngine, Running, engine_fixture
from updater import contract, journal, verify, volume
from updater import engine as eng
from updater.clock import GapClock
from updater.detect import PROTOCOL_LABEL, REVISION_LABEL, VERSION_LABEL
from updater.handover import PROTOCOLS_LABEL
from updater.journal import Owner
from updater.service import Service
from updater.site import Kit, Site
from updater.volume import Volume

PROJECT = "spend-tracker"
APP = eng.REPOSITORIES[0]
UPD = eng.REPOSITORIES[1]
A, B, C = "0.7.1", "0.8.0", "0.9.0"
REVISION = {A: "1a2b3c4d5e6f" + "0" * 28, B: "2b3c4d5e6f7a" + "1" * 28, C: "3c4d5e6f7a8b" + "2" * 28}
#: The alembic head each release's code is at.
HEAD = {A: "aaaaaaaaaaa1", B: "bbbbbbbbbbb2", C: "ccccccccccc3"}


def digest(repo: str, version: str) -> str:
    seed = {APP: "a", UPD: "u"}[repo] + version.replace(".", "")
    return "sha256:" + (seed * 64)[:64].encode().hex()[:64]


def ref(repo: str, version: str) -> str:
    return f"{repo}@{digest(repo, version)}"


IMAGE_ENV = [
    "PATH=/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/usr/sbin:/sbin:/bin",
    "PYTHONUNBUFFERED=1",
    "SPENDTRACKER_DATA_DIR=/var/lib/spend-tracker",
]
HEALTHCHECK = {
    "Test": ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0)"],
    "Interval": 30000000000,
    "Timeout": 5000000000,
    "StartPeriod": 20000000000,
    "Retries": 3,
}
RECOVERY_HASH = "scrypt$ln=15,r=8,p=1$" + "c2FsdHNhbHRzYWx0c2FsdA" + "$" + "A" * 43


def image_doc(repo: str, version: str, *, protocols: str = "1-1") -> dict:
    labels = {
        VERSION_LABEL: version,
        REVISION_LABEL: REVISION[version],
        "org.opencontainers.image.title": "Spend Tracker",
    }
    if repo == APP:
        labels[PROTOCOL_LABEL] = "1"
    else:
        labels[PROTOCOLS_LABEL] = protocols
    r = ref(repo, version)
    return {
        "Id": "sha256:" + digest(repo, version).split(":")[1][::-1],
        "RepoDigests": [r],
        "Config": {
            "Labels": labels,
            "Env": list(IMAGE_ENV),
            "Entrypoint": ["python", "deploy/entrypoint.py"]
            if repo == APP
            else ["python", "-m", "updater"],
            "WorkingDir": "/app",
            "Healthcheck": dict(HEALTHCHECK),
            "User": "65532:65532",
        },
    }


def app_inspect(name: str, image: dict, *, layout: str, sidecar_id: str | None, version: str) -> dict:
    cid = os.urandom(32).hex()
    compose = {
        "com.docker.compose.project": PROJECT,
        "com.docker.compose.service": "app",
        "com.docker.compose.oneoff": "False",
        "com.docker.compose.container-number": "1",
        "com.docker.compose.depends_on": "tailscale:service_healthy:false" if layout == "sidecar" else "",
        "com.docker.compose.config-hash": "f" * 64,
    }
    host = {
        "Binds": [f"{PROJECT}_ledger:/var/lib/spend-tracker:rw"],
        "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
        "Memory": 805306368,
        "ReadonlyRootfs": True,
        "Tmpfs": {"/tmp": ""},
        "SecurityOpt": ["no-new-privileges:true"],
        "CapDrop": ["ALL"],
        "LogConfig": {"Type": "json-file", "Config": {}},
    }
    networks: dict = {}
    if layout == "sidecar":
        host["NetworkMode"] = f"container:{sidecar_id}"
        host["PortBindings"] = {}
    else:
        host["NetworkMode"] = f"{PROJECT}_default"
        host["PortBindings"] = {"8848/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8848"}]}
        networks = {f"{PROJECT}_default": {"Aliases": [name, "app"], "IPAMConfig": None}}
    return {
        "Id": cid,
        "Name": f"/{name}",
        "Image": image["Id"],
        "Created": "2026-10-01T09:00:00.000000000Z",
        "Config": {
            "Image": f"{APP}:{version}",
            "User": "65532:65532",
            "Hostname": cid[:12],
            "Env": [
                "SPENDTRACKER_AUTO_MIGRATE=1",
                "SPENDTRACKER_COOKIE_SECURE=on",
                "SPENDTRACKER_PUBLIC_URL=http://localhost:8848",
                *IMAGE_ENV,
            ],
            "Labels": {**image["Config"]["Labels"], **compose},
            "Healthcheck": dict(HEALTHCHECK),
            "Entrypoint": ["python", "deploy/entrypoint.py"],
            "WorkingDir": "/app",
            "ExposedPorts": {"8848/tcp": {}},
        },
        "HostConfig": host,
        "Mounts": [
            {
                "Type": "volume",
                "Name": f"{PROJECT}_ledger",
                "Source": f"/var/lib/docker/volumes/{PROJECT}_ledger/_data",
                "Destination": "/var/lib/spend-tracker",
                "RW": True,
            }
        ],
        "NetworkSettings": {"Networks": networks, "Ports": {}},
        "State": {"Status": "running", "Running": True, "ExitCode": 0},
    }


@dataclass
class Ledger:
    """The ledger volume, as far as the tests need it."""

    stamp: str
    rows: int = 12
    #: Backup folder name -> the stamp it holds.
    backups: dict[str, str] = field(default_factory=dict)
    #: Databases a restore moved aside (`.before-restore-<stamp>`).
    aside: list[str] = field(default_factory=list)
    drills: int = 0


class FakeTime:
    """Two clocks: the wall clock, and a monotonic one that stands still while "asleep"."""

    def __init__(self) -> None:
        self.t = time.time()
        self.away = 0.0
        #: Called on every sleep: a test's way in while the updater polls.
        self.on_sleep = None

    def __call__(self) -> float:
        return self.t + self.away

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(seconds, 0.01)
        if self.on_sleep is not None:
            self.on_sleep()

    def doze(self, seconds: float) -> None:
        """The machine sleeps: the wall clock moves, the monotonic one does not (8.6)."""
        self.away += seconds


class FakeTrust:
    """Resolves and verifies exactly the images the world publishes."""

    def __init__(self, world: World) -> None:
        self.world = world

    def resolve(self, repository: str, version: str, arch: str) -> verify.Resolved:
        if self.world.offline:
            raise verify.Refused("unreachable", "the registry could not be reached (URLError)")
        if version not in self.world.published:
            raise verify.Refused("attestation", "the registry answered 404")
        return verify.Resolved(
            repository, version, digest(repository, version), 120 * 1024 * 1024, f"linux/{arch}"
        )

    def verify(self, repository: str, d: str, version: str) -> verify.Verified:
        if (repository, version) in self.world.forged:
            raise verify.Refused("identity", "the certificate names another workflow")
        if d != digest(repository, version):
            raise verify.Refused("subject", "the attestation is about another digest")
        commit = REVISION[version]
        if (repository, version) in self.world.relabelled:
            commit = "9" * 40
        return verify.Verified(repository, d, version, commit, "embedded")


class World:
    def __init__(
        self,
        tmp_path: Path,
        *,
        layout: str = "loopback",
        engine: str = "docker-engine",
        fixture: str = "docker-engine-rootful",
        updater_version: str = A,
        updater_protocols: str = "1-1",
    ) -> None:
        self.tmp = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.fake = FakeEngine(engine_fixture(fixture), engine_fixture(fixture, "info"))
        self.fake.on_start = self._on_start
        self.fake.on_exec = self._on_exec
        self.engine = engine
        self.layout = layout
        self.ledger = Ledger(stamp=A)
        self.published = {A, B, C}
        self.offline = False
        self.forged: set[tuple[str, str]] = set()
        self.relabelled: set[tuple[str, str]] = set()
        #: Versions whose app answers health with a 500 (E4's B'').
        self.broken: set[str] = set()
        #: How the next drill behaves: ok, no-backup, migration-fails,
        #: rows-dropped, crash (no report, the container dies), vanish
        #: (the container is gone, as after an engine restart), hang.
        self.drill = "ok"
        self.restore_fails = 0
        #: Called when an app container (not a one-off) starts.
        self.on_app_start = None
        #: Called when the drill starts, before it does anything.
        self.on_drill = None
        self.placard_runs = True
        self.measured = {"free": 40 * 1024**3, "mem_available": 3 * 1024**3, "database": 20 * 1024**2}
        self.time = FakeTime()
        self.clock = GapClock(self.time, self.time.monotonic)
        self.volume = Volume(tmp_path / "update")
        self.volume.init()
        self.project_dir = tmp_path / "project"
        self.project_dir.mkdir()
        (self.project_dir / ".env").write_text(
            "SPENDTRACKER_VERSION=0.7.1\nTS_AUTHKEY=tskey-auth-placeholder\n"
        )
        os.chmod(self.project_dir / ".env", 0o600)

        for version in (A, B, C):
            for repo in (APP, UPD):
                protocols = updater_protocols if version != A else "1-1"
                self.fake.registry[ref(repo, version)] = image_doc(repo, version, protocols=protocols)
        for repo in (APP, UPD):
            self.fake.images[ref(repo, A)] = image_doc(repo, A)

        self.sidecar_id = None
        #: Each sidecar restart is a new network namespace; an app that joined
        #: an older one is stranded (#37, 8.4).
        self.epoch = 0
        if layout == "sidecar":
            sidecar = {
                "Id": os.urandom(32).hex(),
                "Name": f"/{PROJECT}-tailscale-1",
                "Image": "sha256:" + "5" * 64,
                "Config": {
                    "Image": "tailscale/tailscale:v1.102.5",
                    "Labels": {
                        "com.docker.compose.project": PROJECT,
                        "com.docker.compose.service": "tailscale",
                    },
                    "Env": ["TS_AUTHKEY=<redacted>"],
                },
                "HostConfig": {"NetworkMode": f"{PROJECT}_default", "CapAdd": ["CAP_NET_ADMIN"]},
                "NetworkSettings": {"Networks": {f"{PROJECT}_default": {"Aliases": ["tailscale"]}}},
                "State": {"Status": "running", "Running": True},
            }
            self.sidecar_id = self.fake.add_inspected(sidecar)
        app_image = self.fake.images[ref(APP, A)]
        self.app_name = f"{PROJECT}-app-1"
        self.app_id = self.fake.add_inspected(
            app_inspect(self.app_name, app_image, layout=layout, sidecar_id=self.sidecar_id, version=A)
        )
        upd_image = self.fake.images[ref(UPD, A)]
        self.updater_id = self.fake.add_inspected(
            {
                "Name": f"/{PROJECT}-updater-1",
                "Image": upd_image["Id"],
                "Config": {
                    "Image": f"{UPD}:{A}",
                    "Labels": {
                        "com.docker.compose.project": PROJECT,
                        "com.docker.compose.service": "updater",
                    },
                },
                "HostConfig": {"NetworkMode": f"{PROJECT}_default"},
                "Mounts": [{"Type": "volume", "Name": f"{PROJECT}_update", "Destination": "/update"}],
                "State": {"Status": "running", "Running": True},
            }
        )
        self.me = Owner(
            image_digest=digest(UPD, updater_version),
            version=updater_version,
            container=f"{PROJECT}-updater-1",
        )
        self.running: Running | None = None
        self.client: eng.EngineClient | None = None

    # ------------------------------------------------------------------ #

    def __enter__(self) -> World:
        self.running = Running(self.fake).__enter__()
        return self

    def __exit__(self, *exc) -> None:
        assert self.running is not None
        self.running.__exit__(*exc)

    def kit(self, **kw) -> Kit:
        assert self.running is not None
        client = eng.EngineClient(self.running.socket_path, eng.Scope(project=PROJECT))
        client.negotiate()
        self.client = client
        site = Site(
            project=PROJECT,
            volume=self.volume,
            update_volume=f"{PROJECT}_update",
            project_dir=self.project_dir,
            me=kw.pop("me", self.me),
            engine=self.engine,
            hook_dir=kw.pop("hook_dir", None),
        )
        return Kit(
            client=client,
            site=site,
            trust=FakeTrust(self),
            clock=self.clock,
            sleep=self.time.sleep,
            poll=1.0,
            **kw,
        )

    def service(self, **kw) -> Service:
        return Service(self.kit(**kw), owner_uid=os.getuid())

    # ------------------------------------------------------------------ #
    # Requests
    # ------------------------------------------------------------------ #

    def write_request(self, doc: dict) -> None:
        volume.write_json(self.volume.request, doc)

    def base(self, kind: str) -> dict:
        import uuid

        return {
            "protocol": 1,
            "id": str(uuid.uuid4()),
            "kind": kind,
            "created_at": contract.iso(self.clock.now()),
            "requested_by": "usr_owner",
        }

    def prepare_request(self, to: str = B, frm: str = A) -> dict:
        return {**self.base("prepare"), "from_version": frm, "to_version": to}

    def apply_request(self, report: dict) -> dict:
        return {
            **self.base("apply"),
            "from_version": report["from_version"],
            "to_version": report["to_version"],
            "prepared_id": report["id"],
            "digest": report["digest"],
            "updater_digest": report["updater_digest"],
            "accepted_lossy": sorted(contract.lossy_revisions(report)),
            "recovery_hash": RECOVERY_HASH,
        }

    def prepared(self, service: Service, to: str = B, frm: str = A) -> dict:
        req = self.prepare_request(to, frm)
        self.write_request(req)
        service.tick()
        report = volume.read_own_json(self.volume.prepared(req["id"]))
        assert report is not None, volume.read_own_json(self.volume.history(req["id"]))
        return report

    def history(self, rid: str) -> dict | None:
        return volume.read_own_json(self.volume.history(rid))

    def journal(self, rid: str) -> journal.Journal | None:
        return journal.load(self.volume, rid)

    # ------------------------------------------------------------------ #
    # What the containers do
    # ------------------------------------------------------------------ #

    def version_of(self, c: dict) -> str | None:
        seen = self.fake.inspect_of(c)
        image = self.fake.images.get(seen["Config"].get("Image")) or self.fake.image_by_id(
            seen.get("Image", "")
        )
        labels = ((image or {}).get("Config") or {}).get("Labels") or {}
        return labels.get(VERSION_LABEL)

    def answer(self, c: dict) -> tuple[int, str]:
        """The app's own health answer: only at the stamp its code matches."""
        if c["State"] != "running":
            return 1, ""
        version = self.version_of(c)
        if version is None or version != self.ledger.stamp or version in self.broken:
            return 1, ""
        return 0, json.dumps(
            {"status": "ok", "version": version, "commit": REVISION[version][:7], "setup_required": False}
        )

    def apps(self) -> list[dict]:
        return [
            c
            for c in self.fake.containers.values()
            if c["Labels"].get("com.docker.compose.service") == "app" and eng.ROLE_LABEL not in c["Labels"]
        ]

    def running_apps(self) -> list[dict]:
        return [c for c in self.apps() if c["State"] == "running"]

    def restart_sidecar(self) -> None:
        """The sidecar restarts (not by the updater): same id, a new namespace."""
        c = self.fake.containers[self.sidecar_id]
        self.fake.set_state(c, "exited")
        self.fake.set_state(c, "running")
        self.epoch += 1

    def _on_exec(self, fake: FakeEngine, c: dict, cmd: list) -> tuple[int, str]:
        if cmd and cmd[0] == "wget":
            inside = [
                a
                for a in self.running_apps()
                if (fake.inspect_of(a)["HostConfig"].get("NetworkMode") or "") == f"container:{c['Id']}"
                and a.get("_epoch", 0) == self.epoch
            ]
            return self.answer(inside[0]) if inside else (1, "")
        return self.answer(c)

    def _on_start(self, fake: FakeEngine, c: dict) -> None:
        role = c["Labels"].get(eng.ROLE_LABEL)
        if role is None:
            c["_epoch"] = self.epoch
            if self.on_app_start is not None:
                self.on_app_start(c)
            return
        seen = fake.inspect_of(c)
        cmd = [*(seen["Config"].get("Entrypoint") or []), *(seen["Config"].get("Cmd") or [])]
        image_version = self.version_of(c)
        getattr(self, "_" + role.replace("-", "_"))(fake, c, cmd, image_version)

    def _new_stamp(self) -> str:
        n = len(self.ledger.backups) + 1
        return f"20261008-1000{n:02d}"

    def _drill(self, fake: FakeEngine, c: dict, cmd: list, version: str) -> None:
        self.ledger.drills += 1
        if self.on_drill is not None:
            self.on_drill(c)
        report_path = Path(
            str(self.volume.root)
            + cmd[cmd.index("--report") + 1].removeprefix("/var/lib/spend-tracker-update")
        )
        mode = self.drill
        before = self.ledger.stamp
        report = {
            "exit": None,
            "outcome": None,
            "app_version": version,
            "backup": {"folder": None, "verified": False},
            "migrated": False,
            "stamp": {"before": HEAD[before], "after": None},
            "rows": {"before": {"transactions": self.ledger.rows}, "after": {}},
            "dropped": [],
            "log": [f"line {i}" for i in range(60)],
        }
        if mode == "no-backup":
            report.update(exit=1, outcome="stopped")
            volume.write_json(report_path, report)
            fake.finish(c, 1)
            return
        stamp = self._new_stamp()
        self.ledger.backups[stamp] = before
        report["backup"] = {"folder": f"/var/lib/spend-tracker/backups/{stamp}", "verified": True}
        if mode == "hang":
            self.ledger.stamp = "migrating"
            return
        if mode == "migration-fails":
            self.ledger.stamp = "half-migrated"
            report.update(exit=3, outcome="migration-failed")
        elif mode == "rows-dropped":
            self.ledger.stamp = version
            self.ledger.rows -= 1
            report.update(exit=6, outcome="rows-dropped", migrated=True, dropped=["transactions"])
        else:
            self.ledger.stamp = version
            report.update(exit=0, outcome="done", migrated=True)
            report["stamp"]["after"] = HEAD[version]
            report["rows"]["after"] = {"transactions": self.ledger.rows}
        if mode in ("crash", "vanish"):
            if mode == "vanish":
                del fake.containers[c["Id"]]
            else:
                fake.finish(c, 137)
            return
        volume.write_json(report_path, report)
        fake.finish(c, report["exit"])

    def finish_hung_drill(self, version: str = B) -> None:
        """The drill that hung completes, as it would after a gap."""
        c = next(c for c in self.fake.containers.values() if c["Labels"].get(eng.ROLE_LABEL) == "drill")
        self.drill = "ok"
        self.ledger.stamp = version
        seen = self.fake.inspect_of(c)
        cmd = [*(seen["Config"].get("Entrypoint") or []), *(seen["Config"].get("Cmd") or [])]
        report_path = Path(
            str(self.volume.root)
            + cmd[cmd.index("--report") + 1].removeprefix("/var/lib/spend-tracker-update")
        )
        stamp = sorted(self.ledger.backups)[-1]
        volume.write_json(
            report_path,
            {
                "exit": 0,
                "outcome": "done",
                "backup": {"folder": f"/var/lib/spend-tracker/backups/{stamp}", "verified": True},
                "log": ["done"],
            },
        )
        self.fake.finish(c, 0)

    def _restore(self, fake: FakeEngine, c: dict, cmd: list, version: str) -> None:
        if self.restore_fails:
            self.restore_fails -= 1
            fake.finish(c, 1, "", "the copy failed")
            return
        folder = cmd[cmd.index("scripts.restore") + 1]
        name = folder.rsplit("/", 1)[1]
        self.ledger.aside.append(self.ledger.stamp)
        self.ledger.stamp = self.ledger.backups[name]
        fake.finish(c, 0, f"restored {name}\n")

    def _check(self, fake: FakeEngine, c: dict, cmd: list, version: str) -> None:
        doc = {
            "database_stamped": HEAD[A],
            "code_head": HEAD.get(version or ""),
            "pending": [
                {
                    "revision": "b2c3d4e5f6a1",
                    "title": "Two columns",
                    "reversible": "clean",
                    "note": "nothing lost",
                },
                {
                    "revision": "c3d4e5f6a1b2",
                    "title": "A rewrite",
                    "reversible": "lossy",
                    "note": "themes reset",
                },
            ],
            "lossy": True,
        }
        fake.finish(c, 0, json.dumps(doc), "a warning on stderr\n")

    def _measure(self, fake: FakeEngine, c: dict, cmd: list, version: str) -> None:
        fake.finish(c, 0, json.dumps(self.measured) + "\n")

    def _find_backup(self, fake: FakeEngine, c: dict, cmd: list, version: str) -> None:
        newest = sorted(self.ledger.backups)[-1] if self.ledger.backups else None
        folder = f"/var/lib/spend-tracker/backups/{newest}" if newest else None
        fake.finish(c, 0, json.dumps({"folder": folder}) + "\n")

    def _prune(self, fake: FakeEngine, c: dict, cmd: list, version: str) -> None:
        names = cmd[3:]
        for n in names:
            self.ledger.backups.pop(n, None)
        fake.finish(c, 0, json.dumps({"removed": names}) + "\n")

    def _placard(self, fake: FakeEngine, c: dict, cmd: list, version: str) -> None:
        if not self.placard_runs:
            # `scripts.placard` is #163's: until it exists the page exits at once.
            fake.finish(c, 1, "", "No module named scripts.placard\n")

    def _probe(self, fake: FakeEngine, c: dict, cmd: list, version: str) -> None:
        apps = [a for a in self.running_apps() if a["Names"] == [f"/{self.app_name}"]]
        code, out = self.answer(apps[0]) if apps else (1, "")
        fake.finish(c, code, out)

    # ------------------------------------------------------------------ #
    # Assertions
    # ------------------------------------------------------------------ #

    def by_name(self, name: str) -> dict | None:
        return next((c for c in self.fake.containers.values() if c["Names"] == [f"/{name}"]), None)

    def no_running_app_at_a_mismatched_stamp(self) -> None:
        for c in self.running_apps():
            assert self.version_of(c) == self.ledger.stamp, (self.version_of(c), self.ledger.stamp)
