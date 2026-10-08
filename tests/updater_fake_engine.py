"""A recording fake container engine, on a real unix socket, for the updater's tests.

It serves canned answers to the Docker-compatible API in a thread, so the
updater's client is exercised end to end -- `http.client`, the socket, the
paths, the bodies -- and it records every request it receives, including
the ones it has no route for. A test that says "the client made no call"
asserts on `calls`, which is what the engine would have seen.

It answers like a real engine where the tests depend on it: a versioned path
below the engine's `MinAPIVersion` is a 400 naming the floor, as Docker 29
does, and a container listing honours the `label` filter.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socketserver
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ENGINES = Path(__file__).parent / "fixtures" / "updater" / "engines"


def engine_fixture(engine: str, what: str = "version") -> dict:
    return json.loads((ENGINES / engine / f"{what}.json").read_text())


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


@dataclass
class FakeEngine:
    version_doc: dict
    info_doc: dict = field(default_factory=lambda: {"OSType": "linux"})
    containers: dict[str, dict] = field(default_factory=dict)
    images: dict[str, dict] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    execs: dict[str, dict] = field(default_factory=dict)

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

    def by_name(self, name: str) -> dict:
        return next(c for c in self.containers.values() if f"/{name}" in c["Names"])

    # ------------------------------------------------------------------ #

    def _floor(self) -> tuple[int, int]:
        a, b = self.version_doc.get("MinAPIVersion", "1.12").split(".")
        return int(a), int(b)

    def handle(self, method: str, raw_path: str, body: bytes) -> tuple[int, object]:
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
                    out.append(c)
            return 200, out
        if route == ("POST", "/containers/create"):
            cid = secrets.token_hex(32)
            self.containers[cid] = {
                "Id": cid,
                "Names": [f"/{query.get('name', cid[:12])}"],
                "Labels": dict((payload or {}).get("Labels") or {}),
                "State": "created",
                "Config": payload,
            }
            return 201, {"Id": cid, "Warnings": []}
        if route == ("POST", "/images/create"):
            ref = query.get("fromImage", "")
            self.images[ref] = {"Id": "sha256:" + "0" * 64, "RepoDigests": [ref]}
            return 200, b'{"status":"Pulling"}\n{"status":"Digest: ok"}\n'

        cm = re.fullmatch(r"/containers/([^/]+)(/json|/start|/stop|/rename|/exec)?", bare)
        if cm:
            c = self.containers.get(cm.group(1))
            if c is None:
                return 404, {"message": f"No such container: {cm.group(1)}"}
            action = cm.group(2)
            if method == "GET" and action == "/json":
                return 200, {**c, "Name": c["Names"][0]}
            if method == "POST" and action == "/start":
                c["State"] = "running"
                return 204, b""
            if method == "POST" and action == "/stop":
                c["State"] = "exited"
                return 204, b""
            if method == "POST" and action == "/rename":
                c["Names"] = [f"/{query['name']}"]
                return 204, b""
            if method == "POST" and action == "/exec":
                eid = secrets.token_hex(32)
                self.execs[eid] = {"container": c["Id"], "cmd": (payload or {}).get("Cmd"), "ExitCode": 0}
                return 201, {"Id": eid}
            if method == "DELETE" and action is None:
                del self.containers[c["Id"]]
                return 204, b""
        em = re.fullmatch(r"/exec/([^/]+)/(start|json)", bare)
        if em and em.group(1) in self.execs:
            if em.group(2) == "start":
                return 200, b""
            return 200, {"ExitCode": self.execs[em.group(1)]["ExitCode"], "Running": False}
        im = re.fullmatch(r"/images/(.+?)(/json)?", bare)
        if im:
            ref = im.group(1)
            if ref not in self.images:
                return 404, {"message": f"No such image: {ref}"}
            if method == "GET" and im.group(2):
                return 200, self.images[ref]
            if method == "DELETE" and not im.group(2):
                del self.images[ref]
                return 200, [{"Deleted": ref}]
        return 404, {"message": f"page not found: {method} {bare}"}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _any(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, answer = self.server.engine.handle(self.command, self.path, body)  # type: ignore[attr-defined]
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
