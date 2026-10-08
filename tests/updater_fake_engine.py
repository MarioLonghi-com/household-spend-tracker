"""A recording fake container engine, on a real unix socket, for the updater's tests.

It serves canned answers to the Docker-compatible API in a thread, so the
updater's client is exercised end to end -- `http.client`, the socket, the
paths, the bodies -- and it records every request it receives, including
the ones it has no route for. A test that says "the client made no call"
asserts on `calls`, which is what the engine would have seen.

It answers like a real engine where the tests depend on it: a versioned path
below the engine's `MinAPIVersion` is a 400 naming the floor, as Docker 29
does, and a container listing honours the `label` filter.

**Since #161 it also keeps containers the way an engine does**, enough for the
orchestration to run against it: a created container has an inspect view
built from its create body and its image (environment and labels merged, the
named volumes in `Mounts`, the networks and aliases, `State` with
`StartedAt`), a start or a stop changes that state, a rename changes the
name, and a container seeded from a recorded inspect fixture answers with
that fixture. What a container *does* when started, and what an exec prints,
is the test's: `on_start` and `on_exec` (see `tests/updater_world.py`).
Output is framed the way an engine frames it without a TTY.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import re
import secrets
import socketserver
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ENGINES = Path(__file__).parent / "fixtures" / "updater" / "engines"


def engine_fixture(engine: str, what: str = "version") -> dict:
    return json.loads((ENGINES / engine / f"{what}.json").read_text())


def frame(text: str, stream: int = 1) -> bytes:
    """One frame of a non-TTY output stream: header, then the bytes."""
    data = text.encode()
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


@dataclass
class Call:
    method: str
    path: str
    #: The API version in the path, or None for an unversioned call.
    version: str | None
    #: The path without its version prefix.
    bare: str
    query: dict
    body: object


_clock = itertools.count(1)


def engine_time() -> str:
    """A strictly increasing engine timestamp, so a restart is a different `StartedAt`."""
    n = next(_clock)
    return f"2026-10-08T10:{n // 3600 % 60:02d}:{n // 60 % 60:02d}.{n % 60:06d}000Z"


@dataclass
class FakeEngine:
    version_doc: dict
    info_doc: dict = field(default_factory=lambda: {"OSType": "linux"})
    containers: dict[str, dict] = field(default_factory=dict)
    images: dict[str, dict] = field(default_factory=dict)
    #: What a pull by reference fetches: the "registry". A reference not here
    #: pulls a blank image, as before #161.
    registry: dict[str, dict] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    execs: dict[str, dict] = field(default_factory=dict)
    #: What a started container does: `on_start(engine, container)`.
    on_start: Callable[[FakeEngine, dict], None] | None = None
    #: What an exec prints: `on_exec(engine, container, cmd) -> (exit code, output)`.
    on_exec: Callable[[FakeEngine, dict, list], tuple[int, str]] | None = None
    #: Refuse a create: `refuse_create(name, body)` returns the engine's message, or None.
    refuse_create: Callable[[str, dict], str | None] | None = None
    #: Every request closes without an answer, as if the engine went away.
    gone: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)

    def add_container(self, name: str, project: str, label: str = "com.docker.compose.project", **extra) -> str:
        cid = secrets.token_hex(32)
        self.containers[cid] = {
            "Id": cid,
            "Names": [f"/{name}"],
            "Labels": {label: project, **extra.pop("labels", {})},
            "State": "running",
            **extra,
        }
        return cid

    def add_inspected(self, inspect: dict, image_id: str | None = None) -> str:
        """A container seeded from an inspect document (a recorded fixture)."""
        seen = copy.deepcopy(inspect)
        cid = seen.get("Id") or secrets.token_hex(32)
        seen["Id"] = cid
        state = seen.setdefault("State", {})
        state.setdefault("Running", True)
        state.setdefault("Status", "running" if state["Running"] else "exited")
        state.setdefault("StartedAt", engine_time())
        if image_id is not None:
            seen["Image"] = image_id
        self.containers[cid] = {
            "Id": cid,
            "Names": [seen.get("Name") if str(seen.get("Name", "")).startswith("/") else "/" + seen["Name"]],
            "Labels": dict((seen.get("Config") or {}).get("Labels") or {}),
            "State": state["Status"],
            "ImageID": _image_id(seen.get("Image")),
            "_inspect": seen,
        }
        self._ports(self.containers[cid])
        return cid

    def add_image(self, ref: str, labels: dict | None = None, env: list | None = None, **config) -> str:
        """An image present on the engine, known by `ref` (a repo digest) and by its id."""
        image_id = "sha256:" + hashlib.sha256(ref.encode()).hexdigest()
        doc = {
            "Id": image_id,
            "RepoDigests": [ref],
            "Config": {"Labels": dict(labels or {}), "Env": list(env or []), **config},
        }
        self.images[ref] = doc
        return image_id

    def by_name(self, name: str) -> dict:
        return next(c for c in self.containers.values() if f"/{name}" in c["Names"])

    def inspect_of(self, c: dict) -> dict:
        if "_inspect" in c:
            seen = c["_inspect"]
            seen["Name"] = c["Names"][0]
            seen["Id"] = c["Id"]
            return seen
        return {**c, "Name": c["Names"][0]}

    def image_by_id(self, image_id: str) -> dict | None:
        bare = _image_id(image_id)
        return next((d for d in self.images.values() if _image_id(d.get("Id")) == bare), None)

    def set_state(self, c: dict, status: str, exit_code: int = 0) -> None:
        c["State"] = status
        if "_inspect" in c:
            state = c["_inspect"].setdefault("State", {})
            state["Status"] = status
            state["Running"] = status == "running"
            if status == "running":
                state["StartedAt"] = engine_time()
                state["ExitCode"] = 0
            else:
                state["FinishedAt"] = engine_time()
                state["ExitCode"] = exit_code
            self._ports(c)

    def finish(self, c: dict, exit_code: int, output: str = "", stderr: str = "") -> None:
        """A one-off that ran and exited, leaving its output behind."""
        c["_logs"] = frame(output, 1) + (frame(stderr, 2) if stderr else b"")
        self.set_state(c, "exited", exit_code)

    def _ports(self, c: dict) -> None:
        seen = c.get("_inspect")
        if seen is None:
            return
        running = (seen.get("State") or {}).get("Running")
        bindings = (seen.get("HostConfig") or {}).get("PortBindings") or {}
        seen.setdefault("NetworkSettings", {})["Ports"] = copy.deepcopy(bindings) if running else {}

    # ------------------------------------------------------------------ #

    def _created(self, name: str, payload: dict) -> dict:
        cid = secrets.token_hex(32)
        image_ref = payload.get("Image")
        image = self.images.get(image_ref) or {}
        image_cfg = image.get("Config") or {}
        env = list(image_cfg.get("Env") or [])
        keys = {e.split("=", 1)[0] for e in payload.get("Env") or []}
        env = [e for e in env if e.split("=", 1)[0] not in keys] + list(payload.get("Env") or [])
        if (self.version_doc.get("Components") or [{}])[0].get("Name") == "Podman Engine":
            # Podman writes these for every container it creates.
            env += [e for e in ("container=podman", "HOME=/home/nonroot") if e.split("=", 1)[0] not in keys]
            env.append(f"HOSTNAME={cid[:12]}")
        labels = {**(image_cfg.get("Labels") or {}), **(payload.get("Labels") or {})}
        config = {k: v for k, v in payload.items() if k not in ("HostConfig", "NetworkingConfig")}
        config.update({"Image": image_ref, "Env": env, "Labels": labels})
        for key in ("Entrypoint", "Cmd", "WorkingDir", "Healthcheck", "User"):
            if key not in payload and key in image_cfg:
                config[key] = image_cfg[key]
        host = copy.deepcopy(payload.get("HostConfig") or {})
        mounts = []
        for bind in host.get("Binds") or []:
            source, dest, *rest = bind.split(":")
            kind = "bind" if source.startswith("/") else "volume"
            mounts.append({
                "Type": kind, "Name": source if kind == "volume" else None, "Source": source,
                "Destination": dest, "RW": not rest or "ro" not in rest[0].split(","),
            })  # fmt: skip
        networks = {}
        mode = str(host.get("NetworkMode") or "")
        endpoints = ((payload.get("NetworkingConfig") or {}).get("EndpointsConfig")) or {}
        for net, ep in endpoints.items():
            networks[net] = {"Aliases": list((ep or {}).get("Aliases") or []), "IPAMConfig": (ep or {}).get("IPAMConfig")}
        if not networks and mode and mode not in ("none",) and not mode.startswith("container:"):
            networks[mode] = {"Aliases": []}
        seen = {
            "Id": cid,
            "Name": f"/{name}",
            "Created": engine_time(),
            "Image": image.get("Id") or "sha256:" + hashlib.sha256(str(image_ref).encode()).hexdigest(),
            "Config": config,
            "HostConfig": host,
            "Mounts": mounts,
            "NetworkSettings": {"Networks": networks, "Ports": {}},
            "State": {"Status": "created", "Running": False, "ExitCode": 0, "StartedAt": "0001-01-01T00:00:00Z"},
        }
        return {
            "Id": cid,
            "Names": [f"/{name}"],
            "Labels": dict(labels),
            "State": "created",
            "ImageID": seen["Image"],
            "Config": payload,
            "_inspect": seen,
        }

    def _floor(self) -> tuple[int, int]:
        a, b = self.version_doc.get("MinAPIVersion", "1.12").split(".")
        return int(a), int(b)

    def handle(self, method: str, raw_path: str, body: bytes) -> tuple[int, object]:
        with self.lock:
            return self._handle(method, raw_path, body)

    def _handle(self, method: str, raw_path: str, body: bytes) -> tuple[int, object]:
        parts = urlsplit(raw_path)
        path = unquote(parts.path)
        query = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parts.query).items()}
        m = re.fullmatch(r"/v([0-9]+)\.([0-9]+)(/.*)", path)
        version = f"{m.group(1)}.{m.group(2)}" if m else None
        bare = m.group(3) if m else path
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = body.decode("utf-8", "replace")
        self.calls.append(Call(method, raw_path, version, bare, query, payload))

        if m and (int(m.group(1)), int(m.group(2))) < self._floor():
            floor = self.version_doc["MinAPIVersion"]
            return 400, {"message": f"client version {version} is too old. Minimum supported API version is {floor}"}

        route = (method, bare)
        if route == ("GET", "/_ping"):
            return 200, b"OK"
        if route == ("GET", "/version"):
            return 200, self.version_doc
        if route == ("GET", "/info"):
            return 200, self.info_doc
        if route == ("GET", "/containers/json"):
            wanted = json.loads(query.get("filters", "{}")).get("label", [])
            out = []
            for c in self.containers.values():
                labels = c["Labels"]
                if all(labels.get(w.split("=", 1)[0]) == w.split("=", 1)[1] for w in wanted):
                    out.append({k: v for k, v in c.items() if not k.startswith("_")})
            return 200, out
        if route == ("POST", "/containers/create"):
            name = query.get("name", "")
            if any(f"/{name}" in c["Names"] for c in self.containers.values()):
                return 409, {"message": f'Conflict. The container name "/{name}" is already in use.'}
            if self.refuse_create is not None:
                message = self.refuse_create(name, payload or {})
                if message:
                    return 500, {"message": message}
            made = self._created(name or secrets.token_hex(6), payload or {})
            self.containers[made["Id"]] = made
            return 201, {"Id": made["Id"], "Warnings": []}
        if route == ("POST", "/images/create"):
            ref = query.get("fromImage", "")
            if ref in self.registry:
                self.images[ref] = copy.deepcopy(self.registry[ref])
            elif ref not in self.images:
                self.images[ref] = {"Id": "sha256:" + "0" * 64, "RepoDigests": [ref], "Config": {"Labels": {}}}
            return 200, b'{"status":"Pulling"}\n{"status":"Digest: ok"}\n'

        cm = re.fullmatch(r"/containers/([^/]+)(/json|/start|/stop|/rename|/exec|/logs)?", bare)
        if cm:
            c = self.containers.get(cm.group(1))
            if c is None:
                return 404, {"message": f"No such container: {cm.group(1)}"}
            action = cm.group(2)
            if method == "GET" and action == "/json":
                return 200, self.inspect_of(c)
            if method == "POST" and action == "/start":
                self.set_state(c, "running")
                if self.on_start is not None:
                    self.on_start(self, c)
                return 204, b""
            if method == "POST" and action == "/stop":
                self.set_state(c, "exited", 143 if c["State"] == "running" else 0)
                return 204, b""
            if method == "POST" and action == "/rename":
                new = query["name"]
                if any(f"/{new}" in o["Names"] for o in self.containers.values() if o is not c):
                    return 409, {"message": f"name {new} is already in use"}
                c["Names"] = [f"/{new}"]
                return 204, b""
            if method == "POST" and action == "/exec":
                eid = secrets.token_hex(32)
                cmd = (payload or {}).get("Cmd")
                if c["State"] != "running":
                    return 409, {"message": f"container {c['Id']} is not running"}
                code, out = (0, "")
                if self.on_exec is not None:
                    code, out = self.on_exec(self, c, list(cmd or []))
                self.execs[eid] = {"container": c["Id"], "cmd": cmd, "ExitCode": code, "output": out}
                return 201, {"Id": eid}
            if method == "GET" and action == "/logs":
                return 200, c.get("_logs", b"")
            if method == "DELETE" and action is None:
                if c["State"] == "running" and query.get("force") != "1":
                    return 409, {"message": "cannot remove a running container"}
                del self.containers[c["Id"]]
                return 204, b""
        em = re.fullmatch(r"/exec/([^/]+)/(start|json)", bare)
        if em and em.group(1) in self.execs:
            run = self.execs[em.group(1)]
            if em.group(2) == "start":
                return 200, frame(run.get("output", ""))
            return 200, {"ExitCode": run["ExitCode"], "Running": False}
        im = re.fullmatch(r"/images/(.+?)(/json)?", bare)
        if im:
            ref = im.group(1)
            doc = self.images.get(ref) or (self.image_by_id(ref) if re.fullmatch(r"(sha256:)?[0-9a-f]{64}", ref) else None)
            if doc is None:
                return 404, {"message": f"No such image: {ref}"}
            if method == "GET" and im.group(2):
                return 200, doc
            if method == "DELETE" and not im.group(2):
                for key in [k for k, v in self.images.items() if v is doc]:
                    del self.images[key]
                return 200, [{"Deleted": ref}]
        return 404, {"message": f"page not found: {method} {bare}"}


def _image_id(value: object) -> str:
    text = str(value or "")
    return text.removeprefix("sha256:")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _any(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        engine = self.server.engine  # type: ignore[attr-defined]
        if engine.gone:
            # The engine went away mid-call: close without an answer.
            self.close_connection = True
            return
        status, answer = engine.handle(self.command, self.path, body)
        data = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_DELETE = do_HEAD = do_PUT = _any

    def address_string(self) -> str:  # a unix socket has no peer address
        return "unix"

    def log_message(self, *args) -> None:
        pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Running:
    """A FakeEngine served on a socket. Use as a context manager."""

    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        # AF_UNIX paths are short (104 bytes on macOS); pytest's tmp_path is not.
        self._dir = tempfile.mkdtemp(prefix="fe-", dir="/tmp")
        self.socket_path = os.path.join(self._dir, "engine.sock")
        self._server = _Server(self.socket_path, _Handler)
        self._server.engine = engine  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    def __enter__(self) -> Running:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        os.unlink(self.socket_path)
        os.rmdir(self._dir)
