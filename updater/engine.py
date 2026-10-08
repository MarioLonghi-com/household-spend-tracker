"""The updater's own engine client: a short list of calls, and nothing else (6.2).

**The engine socket is root on the machine** (6.1). Anything that can make the
updater call `create` can start a container with the host's files mounted. So
this client is not a general Docker client with a filter in front of it: it is
the list of calls in `ENDPOINTS`, every request is checked against that list
before a byte reaches the socket, and `tests/test_updater_engine.py` fails if
the list changes without the test changing with it.

On top of the list:

- **Container calls are scoped to the updater's own compose project.** A
  container is acted on only if a project-filtered listing returns it with
  `com.docker.compose.project` -- or podman-compose's
  `io.podman.compose.project` (S5) -- equal to the project. Anything else is
  refused before a call naming it is made.
- **Create bodies are guarded.** `Privileged`, `CapAdd`, host bind mounts
  other than the engine socket and the project directory the updater was
  itself started with, `PidMode`/`NetworkMode`/`IpcMode`/`UTSMode`/`UsernsMode`
  `host`, devices, `VolumesFrom` and unconfined security options are refused,
  whatever the caller asks. The image must be one of this repository's two,
  by digest, and the new container must carry the project label.
- **Images are only this repository's two**, and pulled by digest.
- **The sidecar is never stopped, started, renamed or removed**, and its only
  exec is `wget`.

**API versions are negotiated, never pinned (C3).** `GET /version`,
unversioned, gives the engine's window (`MinAPIVersion` to `ApiVersion`). The
client then uses `min(engine ApiVersion, TESTED_MAX)` on every path -- every
path except `/version` and `/_ping` is versioned, so no mutating call is ever
unversioned. `DOCKER_API_VERSION` and `DOCKER_HOST` are never read: the
socket is the path the client is given, and the version is the negotiated one.

If the negotiated version is below the engine's `MinAPIVersion`, the engine
has moved past everything this updater was tested with: the socket is
`outdated`. Then only the calls an `update_updater` handover needs -- inspect,
pull, create, start, rename, stop -- are allowed, at the engine's
`MinAPIVersion`, and everything else is refused. If the engine's newest
version is below `TESTED_FLOOR`, it is `too_old`, and nothing but detection
runs.

**Why the tested window is 1.41 to 1.52.**

- `TESTED_FLOOR = 1.41`: the Docker-compatible API Podman 4.x reports (4.4 is
  the oldest Podman 8.3 accepts), and Docker 20.10's. Below it the shapes this
  client sends -- create's `HostConfig.Mounts`, the `platform` query -- are
  not the ones it was written against.
- `TESTED_MAX = 1.52`: the newest version of Docker Engine 29.0, the release
  that raised the engine's floor to 1.44. Every window recorded or written
  down for the engines the design supports intersects 1.41-1.52: Podman 4.9
  (1.24-1.41), Podman 6.1 in `podman machine` (1.24-1.44), Docker 28
  (1.24-1.51), Docker 29.0 (1.44-1.52) and Docker Desktop 4.93 with Engine
  29.8 (1.40-1.56). So each of them negotiates `ok`, and a future engine
  floor can rise eight versions past 29's before this updater is `outdated`.
  It stops short of 1.56 because no create body has yet been sent to an
  engine at 1.53-1.56; the window is widened only by a release whose CI ran
  against that engine (6.2).
"""

from __future__ import annotations

import http.client
import json
import re
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlencode

#: Inclusive. See the module docstring for why these two.
TESTED_FLOOR: tuple[int, int] = (1, 41)
TESTED_MAX: tuple[int, int] = (1, 52)

#: The engine's own floor when `/version` does not say: Docker's historical one.
DEFAULT_MIN_API: tuple[int, int] = (1, 12)

PROJECT_LABELS = ("com.docker.compose.project", "io.podman.compose.project")

REPOSITORIES = (
    "ghcr.io/mariolonghi-com/household-spend-tracker",
    "ghcr.io/mariolonghi-com/household-spend-tracker-updater",
)
IMAGE_BY_DIGEST = re.compile(
    r"(ghcr\.io/mariolonghi-com/household-spend-tracker(?:-updater)?)@(sha256:[0-9a-f]{64})"
)
CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}")
EXEC_ID = re.compile(r"[0-9a-f]{12,64}")

#: The only commands an exec may start: the health probes (6.2).
APP_PROBES = ("python", "python3")
SIDECAR_PROBES = ("wget",)


@dataclass(frozen=True)
class Endpoint:
    name: str
    method: str
    #: `{id}` is one path segment; `{image}` a whole image reference.
    path: str
    mutating: bool
    versioned: bool = True


#: Every call this client can make. Design notes 6.2, plus the two exec calls
#: without which `POST /containers/{id}/exec` creates a probe that never runs.
ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("ping", "GET", "/_ping", False, versioned=False),
    Endpoint("version", "GET", "/version", False, versioned=False),
    Endpoint("info", "GET", "/info", False),
    Endpoint("containers", "GET", "/containers/json", False),
    Endpoint("inspect", "GET", "/containers/{id}/json", False),
    Endpoint("create", "POST", "/containers/create", True),
    Endpoint("start", "POST", "/containers/{id}/start", True),
    Endpoint("stop", "POST", "/containers/{id}/stop", True),
    Endpoint("rename", "POST", "/containers/{id}/rename", True),
    Endpoint("remove", "DELETE", "/containers/{id}", True),
    Endpoint("exec_create", "POST", "/containers/{id}/exec", True),
    Endpoint("exec_start", "POST", "/exec/{id}/start", True),
    Endpoint("exec_inspect", "GET", "/exec/{id}/json", False),
    Endpoint("pull", "POST", "/images/create", True),
    Endpoint("image_inspect", "GET", "/images/{image}/json", False),
    Endpoint("image_remove", "DELETE", "/images/{image}", True),
)

#: What an `outdated` updater may still do: detection, and H1-H5 (C3).
OUTDATED_ALLOWED = frozenset(
    {"ping", "version", "info", "containers", "inspect", "image_inspect", "pull", "create", "start", "rename", "stop"}
)
#: What a `too_old` engine, or one not yet negotiated with, may be asked.
DETECTION_ONLY = frozenset({"ping", "version", "info"})

_BY_NAME = {e.name: e for e in ENDPOINTS}


def _pattern(path: str) -> re.Pattern[str]:
    out = re.escape(path).replace(re.escape("{id}"), r"[^/]+").replace(re.escape("{image}"), r".+")
    return re.compile(out)


_PATTERNS = tuple((e, _pattern(e.path)) for e in ENDPOINTS)
_VERSION_PREFIX = re.compile(r"/v([0-9]+)\.([0-9]+)(/.*)")


class EngineError(Exception):
    """The engine answered with an error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class NotAllowed(Exception):
    """A call this client refuses to make. Nothing was sent."""


class EngineUnavailable(Exception):
    """The socket cannot be used. `socket_state` is the heartbeat's word for why."""

    def __init__(self, socket_state: str, message: str) -> None:
        super().__init__(message)
        self.socket_state = socket_state


def api(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"([0-9]+)\.([0-9]+)", value)
    return (int(m.group(1)), int(m.group(2))) if m else None


def api_text(v: tuple[int, int]) -> str:
    return f"{v[0]}.{v[1]}"


@dataclass(frozen=True)
class Negotiated:
    """What `GET /version` said, and what the client will speak."""

    engine_min: tuple[int, int]
    engine_max: tuple[int, int]
    #: The version every versioned path uses.
    version: tuple[int, int]
    #: `ok`, `outdated` or `too_old`.
    state: str
    #: `Engine` or `Podman Engine`: `Components[0].Name`.
    component: str | None
    #: For Podman, its own version (`Components[0].Version`), which is what
    #: 8.3's "Podman 4.4 or newer" is about. The compat window is top-level.
    engine_version: str | None
    platform: str | None

    @property
    def engine_api(self) -> str:
        """The heartbeat's `engine_api`: the engine's own window."""
        return f"{api_text(self.engine_min)}-{api_text(self.engine_max)}"

    @property
    def podman(self) -> bool:
        return self.component == "Podman Engine"


def negotiate(doc: Mapping) -> Negotiated:
    """Pure: the version to speak, from an unversioned `GET /version` answer.

    The Docker-compatible window is the top-level `ApiVersion` and
    `MinAPIVersion` on every engine. Podman's `Components[0].Details` carry
    *libpod's* versions (`APIVersion` 6.1.3, `MinAPIVersion` 4.0.0), which are
    not compat API versions and are never negotiated with.
    """
    engine_max = api(doc.get("ApiVersion"))
    if engine_max is None:
        raise EngineUnavailable("unknown_engine", "The engine did not say which API versions it speaks.")
    engine_min = api(doc.get("MinAPIVersion")) or DEFAULT_MIN_API
    components = doc.get("Components") or []
    first = components[0] if components and isinstance(components[0], dict) else {}
    component = first.get("Name") if isinstance(first.get("Name"), str) else None
    engine_version = first.get("Version") if isinstance(first.get("Version"), str) else doc.get("Version")
    platform = (doc.get("Platform") or {}).get("Name") if isinstance(doc.get("Platform"), dict) else None

    chosen = min(engine_max, TESTED_MAX)
    if engine_max < TESTED_FLOOR:
        state, version = "too_old", chosen
    elif chosen < engine_min:
        state, version = "outdated", engine_min
    else:
        state, version = "ok", chosen
    return Negotiated(engine_min, engine_max, version, state, component, engine_version, platform)


class UnixHTTPConnection(http.client.HTTPConnection):
    """`http.client` over a unix socket. The host name is only the Host header."""

    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._socket_path)
        except BaseException:
            s.close()
            raise
        self.sock = s


def allowed(method: str, path: str) -> Endpoint | None:
    """The table entry a raw request matches, or None. Versioned paths only where listed."""
    m = _VERSION_PREFIX.fullmatch(path.split("?", 1)[0])
    bare = m.group(3) if m else path.split("?", 1)[0]
    for endpoint, pattern in _PATTERNS:
        if endpoint.method == method and pattern.fullmatch(bare) and endpoint.versioned == bool(m):
            return endpoint
    return None


@dataclass(frozen=True)
class Scope:
    """Whose containers the updater may touch, and what it may mount."""

    project: str
    #: The host path of the engine socket the updater was started with, and of
    #: the project directory mounted at `/project` (S1). The only host paths a
    #: created container may bind.
    bind_sources: tuple[str, ...] = ()
    #: The sidecar's container name, if the layout has one. Never stopped,
    #: started, renamed or removed; probed only with `wget`.
    sidecar: str | None = None


class EngineClient:
    def __init__(self, socket_path: str, scope: Scope, timeout: float = 30.0) -> None:
        self.socket_path = socket_path
        self.scope = scope
        self.timeout = timeout
        self.negotiated: Negotiated | None = None

    # ------------------------------------------------------------------ #
    # The one way out
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> str:
        return self.negotiated.state if self.negotiated else "unnegotiated"

    def _permitted(self, endpoint: Endpoint) -> None:
        state = self.state
        if state == "ok":
            return
        if state == "outdated" and endpoint.name in OUTDATED_ALLOWED:
            return
        if endpoint.name in DETECTION_ONLY:
            return
        raise NotAllowed(f"{endpoint.method} {endpoint.path} is not allowed while the socket is {state}.")

    def _send(
        self,
        method: str,
        path: str,
        query: Mapping | None = None,
        body: object = None,
    ) -> tuple[int, bytes]:
        """Make one request -- if, and only if, the table lists it."""
        endpoint = allowed(method, path)
        if endpoint is None:
            raise NotAllowed(f"{method} {path} is not a call this updater makes.")
        self._permitted(endpoint)
        if endpoint.mutating and not _VERSION_PREFIX.fullmatch(path):
            raise NotAllowed(f"{method} {path} would be an unversioned mutating call.")
        target = path + ("?" + urlencode(query, doseq=True) if query else "")
        headers = {"Host": "localhost"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn = UnixHTTPConnection(self.socket_path, self.timeout)
        try:
            try:
                conn.request(method, target, body=payload, headers=headers)
            except PermissionError as e:
                raise EngineUnavailable("permission_denied", "Permission denied on the engine socket.") from e
            except (FileNotFoundError, ConnectionRefusedError) as e:
                raise EngineUnavailable("unreachable", "The engine socket does not answer.") from e
            try:
                resp = conn.getresponse()
                data = resp.read()
            except (ConnectionError, http.client.HTTPException) as e:
                # The engine went away mid-call: 8.6 counts that as an engine restart.
                raise EngineUnavailable("unreachable", "The engine stopped answering.") from e
            return resp.status, data
        finally:
            conn.close()

    def _call(
        self,
        name: str,
        query: Mapping | None = None,
        body: object = None,
        **params: str,
    ) -> object:
        endpoint = _BY_NAME[name]
        path = endpoint.path
        for key, value in params.items():
            path = path.replace("{" + key + "}", quote(value, safe="/@:"))
        if endpoint.versioned:
            if self.negotiated is None:
                raise NotAllowed("No API version negotiated yet: call negotiate() first.")
            path = f"/v{api_text(self.negotiated.version)}{path}"
        status, data = self._send(endpoint.method, path, query, body)
        if status >= 400:
            try:
                message = json.loads(data).get("message", "")
            except (ValueError, AttributeError):
                message = data.decode("utf-8", "replace")[:200]
            raise EngineError(status, message)
        if not data:
            return None
        if name == "ping":
            return data.decode()
        if name == "pull":
            return _pull_stream(data)
        try:
            return json.loads(data)
        except ValueError:
            return data.decode("utf-8", "replace")

    # ------------------------------------------------------------------ #
    # Detection
    # ------------------------------------------------------------------ #

    def negotiate(self) -> Negotiated:
        """`GET /version`, unversioned, then fix the version for every other call."""
        self.negotiated = None
        doc = self._call("version")
        if not isinstance(doc, dict):
            raise EngineUnavailable("unknown_engine", "The engine's /version answer is not a JSON object.")
        self.negotiated = negotiate(doc)
        return self.negotiated

    def ping(self) -> str:
        return str(self._call("ping"))

    def info(self) -> dict:
        return self._call("info")  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    # Containers, in the project only
    # ------------------------------------------------------------------ #

    def _in_project(self, labels: object) -> bool:
        return isinstance(labels, dict) and any(labels.get(k) == self.scope.project for k in PROJECT_LABELS)

    def containers(self) -> list[dict]:
        """The project's containers, running or not. Filtered by the engine and again here."""
        found: dict[str, dict] = {}
        for key in PROJECT_LABELS:
            listed = self._call(
                "containers",
                query={"all": "1", "filters": json.dumps({"label": [f"{key}={self.scope.project}"]})},
            )
            for c in listed or []:  # type: ignore[union-attr]
                if isinstance(c, dict) and self._in_project(c.get("Labels")) and isinstance(c.get("Id"), str):
                    found[c["Id"]] = c
        return list(found.values())

    def _resolve(self, ref: str) -> dict:
        """The project container `ref` names (id or name), or NotAllowed. Sends nothing naming it."""
        if not (CONTAINER_NAME.fullmatch(ref) or CONTAINER_ID.fullmatch(ref)):
            raise NotAllowed(f"{ref!r} is not a container name or id.")
        for c in self.containers():
            names = [n.lstrip("/") for n in c.get("Names") or []]
            if ref == c["Id"] or ref in names:
                return c
        raise NotAllowed(f"Container {ref!r} is not in project {self.scope.project!r}.")

    def _not_sidecar(self, c: dict, what: str) -> None:
        names = [n.lstrip("/") for n in c.get("Names") or []]
        if self.scope.sidecar and self.scope.sidecar in names:
            raise NotAllowed(f"The updater never {what} the sidecar.")

    def inspect(self, ref: str) -> dict:
        c = self._resolve(ref)
        return self._call("inspect", id=c["Id"])  # type: ignore[return-value]

    def start(self, ref: str) -> None:
        c = self._resolve(ref)
        self._not_sidecar(c, "starts")
        self._call("start", id=c["Id"])

    def stop(self, ref: str, grace: int = 30) -> None:
        c = self._resolve(ref)
        self._not_sidecar(c, "stops")
        self._call("stop", query={"t": str(int(grace))}, id=c["Id"])

    def rename(self, ref: str, new_name: str) -> None:
        if not CONTAINER_NAME.fullmatch(new_name):
            raise NotAllowed(f"{new_name!r} is not a container name.")
        c = self._resolve(ref)
        self._not_sidecar(c, "renames")
        self._call("rename", query={"name": new_name}, id=c["Id"])

    def remove(self, ref: str, force: bool = False) -> None:
        c = self._resolve(ref)
        self._not_sidecar(c, "removes")
        self._call("remove", query={"force": "1" if force else "0"}, id=c["Id"])

    def create(self, name: str, body: dict) -> str:
        """Create a container of one of the allowed shapes. Returns its id."""
        if not CONTAINER_NAME.fullmatch(name):
            raise NotAllowed(f"{name!r} is not a container name.")
        guard_create(body, self.scope)
        made = self._call("create", query={"name": name}, body=body)
        if not isinstance(made, dict) or not isinstance(made.get("Id"), str):
            raise EngineError(500, "create answered without an id")
        return made["Id"]

    def probe(self, ref: str, cmd: list[str]) -> int:
        """Run a health probe in a project container. Returns its exit code."""
        c = self._resolve(ref)
        names = [n.lstrip("/") for n in c.get("Names") or []]
        allowed_cmds = SIDECAR_PROBES if self.scope.sidecar and self.scope.sidecar in names else APP_PROBES
        if not cmd or not all(isinstance(a, str) for a in cmd) or cmd[0] not in allowed_cmds:
            raise NotAllowed(f"Only {', '.join(allowed_cmds)} may run there.")
        made = self._call(
            "exec_create", body={"Cmd": cmd, "AttachStdout": True, "AttachStderr": True}, id=c["Id"]
        )
        exec_id = made.get("Id") if isinstance(made, dict) else None
        if not isinstance(exec_id, str) or not EXEC_ID.fullmatch(exec_id):
            raise EngineError(500, "exec create answered without an id")
        self._call("exec_start", body={"Detach": False, "Tty": False}, id=exec_id)
        done = self._call("exec_inspect", id=exec_id)
        code = done.get("ExitCode") if isinstance(done, dict) else None
        return code if isinstance(code, int) else -1

    # ------------------------------------------------------------------ #
    # Images, this repository's two only
    # ------------------------------------------------------------------ #

    @staticmethod
    def _image(ref: str) -> str:
        if not IMAGE_BY_DIGEST.fullmatch(ref):
            raise NotAllowed(f"{ref!r} is not one of this repository's images, by digest.")
        return ref

    def pull(self, ref: str) -> list[dict]:
        return self._call("pull", query={"fromImage": self._image(ref)})  # type: ignore[return-value]

    def inspect_image(self, ref: str) -> dict:
        return self._call("image_inspect", image=self._image(ref))  # type: ignore[return-value]

    def remove_image(self, ref: str) -> None:
        self._call("image_remove", image=self._image(ref))


def _pull_stream(data: bytes) -> list[dict]:
    """`POST /images/create` answers a stream of JSON lines; an error arrives in one."""
    events = []
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            if event.get("error") or event.get("errorDetail"):
                raise EngineError(500, str(event.get("error") or event.get("errorDetail")))
            events.append(event)
    return events


# --------------------------------------------------------------------------- #
# The create guard
# --------------------------------------------------------------------------- #

_HOST_MODES = ("PidMode", "NetworkMode", "IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode")
_DEVICE_KEYS = ("Devices", "DeviceRequests", "DeviceCgroupRules")


def _bind_source(bind: str) -> str:
    return bind.split(":", 1)[0]


def guard_create(body: object, scope: Scope) -> None:
    """Refuse a create body that reaches the host, whoever asked for it (6.2, U2)."""
    if not isinstance(body, dict):
        raise NotAllowed("A create body is a JSON object.")
    image = body.get("Image")
    if not isinstance(image, str) or not IMAGE_BY_DIGEST.fullmatch(image):
        raise NotAllowed("A container is created only from this repository's images, by digest.")
    labels = body.get("Labels")
    if not (isinstance(labels, dict) and any(labels.get(k) == scope.project for k in PROJECT_LABELS)):
        raise NotAllowed(f"A created container must carry project {scope.project!r}'s label.")
    host = body.get("HostConfig") or {}
    if not isinstance(host, dict):
        raise NotAllowed("HostConfig is a JSON object.")
    if host.get("Privileged"):
        raise NotAllowed("Privileged containers are refused.")
    if host.get("CapAdd"):
        raise NotAllowed("Added capabilities are refused.")
    for key in _HOST_MODES:
        value = host.get(key)
        if isinstance(value, str) and (value == "host" or value.startswith("host:")):
            raise NotAllowed(f"{key} host is refused.")
    for key in _DEVICE_KEYS:
        if host.get(key):
            raise NotAllowed("Devices are refused.")
    if host.get("VolumesFrom"):
        raise NotAllowed("VolumesFrom is refused.")
    for opt in host.get("SecurityOpt") or []:
        if not isinstance(opt, str) or "unconfined" in opt:
            raise NotAllowed(f"Security option {opt!r} is refused.")
    allowed_sources = set(scope.bind_sources)
    for bind in host.get("Binds") or []:
        if not isinstance(bind, str):
            raise NotAllowed("A bind is a string.")
        source = _bind_source(bind)
        # A source that is not a path is a named volume.
        if source.startswith(("/", ".", "~")) and source not in allowed_sources:
            raise NotAllowed(f"A host bind mount of {source} is refused.")
    for mount in host.get("Mounts") or []:
        if not isinstance(mount, dict):
            raise NotAllowed("A mount is a JSON object.")
        kind = mount.get("Type", "volume")
        if kind == "bind":
            if mount.get("Source") not in allowed_sources:
                raise NotAllowed(f"A host bind mount of {mount.get('Source')} is refused.")
        elif kind not in ("volume", "tmpfs"):
            raise NotAllowed(f"A {kind} mount is refused.")


def endpoints_table() -> Iterable[tuple[str, str, bool]]:
    """`(method, path, versioned)` for every listed call. What U6 compares against 6.2."""
    return [(e.method, e.path, e.versioned) for e in ENDPOINTS]
