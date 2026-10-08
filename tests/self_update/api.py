"""The scenarios' own engine client: the Docker-compatible API, unrestricted, test-only.

The updater's client (`updater.engine`) is a short list of calls scoped to its
own compose project, which is the point of it and exactly what a test driver
cannot live with: the driver kills the updater, starts a parked container by
hand, runs a helper that reads the `update` volume, restarts what it must.
So the driver speaks the same compatible API through this, over the same
unix socket, against Docker Engine, rootless Docker and Podman alike -- one
code path for every leg, and no `docker` or `podman` CLI needed wherever the
driver runs (on a runner, or inside the engine container of the canary).

Standard library only. Never imported by `updater/`.
"""

from __future__ import annotations

import http.client
import json
import socket
import struct
import time
import urllib.parse
from collections.abc import Mapping

#: Below every engine's window that the matrix and the canary run.
API = "v1.41"


class Unreachable(Exception):
    """The socket did not answer (an engine restart, in E6)."""


class Failed(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


class Engine:
    def __init__(self, socket_path: str, timeout: float = 120.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    # ------------------------------------------------------------------ #

    def call(
        self,
        method: str,
        path: str,
        query: Mapping | None = None,
        body: object = None,
        *,
        versioned: bool = True,
        raw: bool = False,
        timeout: float | None = None,
    ):
        target = (f"/{API}" if versioned else "") + path
        if query:
            target += "?" + urllib.parse.urlencode(query)
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn = _Unix(self.socket_path, timeout or self.timeout)
        try:
            try:
                conn.request(method, target, body=payload, headers=headers)
                resp = conn.getresponse()
                data = resp.read()
            except (OSError, http.client.HTTPException) as e:
                raise Unreachable(f"{method} {path}: {e}") from e
        finally:
            conn.close()
        if resp.status >= 400:
            try:
                message = json.loads(data).get("message", "")
            except ValueError:
                message = data[:300].decode("utf-8", "replace")
            raise Failed(resp.status, f"{method} {path}: {message}")
        if raw:
            return data
        if not data.strip():
            return None
        try:
            return json.loads(data)
        except ValueError:
            # A pull answers with a stream of JSON lines.
            return [json.loads(line) for line in data.splitlines() if line.strip()]

    def wait_answering(self, seconds: float = 180.0) -> None:
        deadline = time.monotonic() + seconds
        while True:
            try:
                self.call("GET", "/_ping", versioned=False, raw=True, timeout=5)
                return
            except (Unreachable, Failed):
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

    # ------------------------------------------------------------------ #

    def version(self) -> dict:
        return self.call("GET", "/version", versioned=False)

    def info(self) -> dict:
        return self.call("GET", "/info")

    def containers(self, label: str | None = None) -> list[dict]:
        query: dict = {"all": "true"}
        if label:
            query["filters"] = json.dumps({"label": [label]})
        return self.call("GET", "/containers/json", query) or []

    def inspect(self, ref: str) -> dict:
        return self.call("GET", f"/containers/{urllib.parse.quote(ref, safe='')}/json")

    def exists(self, ref: str) -> bool:
        try:
            self.inspect(ref)
            return True
        except Failed as e:
            if e.status == 404:
                return False
            raise

    def image(self, ref: str) -> dict:
        return self.call("GET", f"/images/{urllib.parse.quote(ref, safe='')}/json")

    def pull(self, ref: str) -> None:
        """Pull `ref` (a repository by digest or tag); an error inside the stream is a Failed."""
        repo, sep, digest = ref.partition("@")
        query = {"fromImage": ref} if sep else {"fromImage": ref.rsplit(":", 1)[0], "tag": ref.rsplit(":", 1)[1]}
        events = self.call("POST", "/images/create", query, timeout=600)
        for event in events if isinstance(events, list) else [events or {}]:
            if isinstance(event, dict) and (event.get("error") or event.get("errorDetail")):
                raise Failed(500, f"pull {ref}: {event.get('error') or event.get('errorDetail')}")

    def start(self, ref: str) -> None:
        self.call("POST", f"/containers/{urllib.parse.quote(ref, safe='')}/start")

    def stop(self, ref: str, seconds: int = 10) -> None:
        self.call("POST", f"/containers/{urllib.parse.quote(ref, safe='')}/stop", {"t": seconds})

    def pause(self, ref: str) -> None:
        self.call("POST", f"/containers/{urllib.parse.quote(ref, safe='')}/pause")

    def unpause(self, ref: str) -> None:
        self.call("POST", f"/containers/{urllib.parse.quote(ref, safe='')}/unpause")

    def kill(self, ref: str) -> None:
        self.call("POST", f"/containers/{urllib.parse.quote(ref, safe='')}/kill")

    def remove(self, ref: str) -> None:
        try:
            self.call(
                "DELETE", f"/containers/{urllib.parse.quote(ref, safe='')}", {"force": "true", "v": "false"}
            )
        except Failed as e:
            if e.status != 404:
                raise

    def remove_volume(self, name: str) -> None:
        try:
            self.call("DELETE", f"/volumes/{urllib.parse.quote(name, safe='')}", {"force": "true"})
        except Failed as e:
            if e.status != 404:
                raise

    def volumes(self) -> list[dict]:
        return (self.call("GET", "/volumes") or {}).get("Volumes") or []

    def run(
        self,
        name: str,
        image: str,
        cmd: list[str],
        *,
        user: str = "",
        binds: list[str] | None = None,
        entrypoint: list[str] | None = None,
        network: str = "none",
        labels: Mapping[str, str] | None = None,
    ) -> str:
        """Create and start a detached container. Its id."""
        self.remove(name)
        body: dict = {
            "Image": image,
            "Cmd": cmd,
            "User": user,
            "Labels": dict(labels or {}),
            "HostConfig": {"Binds": binds or [], "NetworkMode": network, "SecurityOpt": ["label=disable"]},
        }
        if entrypoint is not None:
            body["Entrypoint"] = entrypoint
        made = self.call("POST", "/containers/create", {"name": name}, body)
        self.start(made["Id"])
        return made["Id"]

    def exec(
        self, ref: str, cmd: list[str], user: str = "", timeout: float = 300.0, env: list[str] | None = None
    ) -> tuple[int, str]:
        """Run `cmd` in a running container; its exit code and its output (stdout then stderr)."""
        body = {"Cmd": cmd, "User": user, "AttachStdout": True, "AttachStderr": True, "Tty": False}
        if env:
            body["Env"] = env
        made = self.call("POST", f"/containers/{urllib.parse.quote(ref, safe='')}/exec", body=body)
        data = self.call(
            "POST",
            f"/exec/{made['Id']}/start",
            body={"Detach": False, "Tty": False},
            raw=True,
            timeout=timeout,
        )
        out, err = _demux(data)
        code = None
        for _ in range(50):
            code = (self.call("GET", f"/exec/{made['Id']}/json") or {}).get("ExitCode")
            if code is not None:
                break
            time.sleep(0.1)
        return int(code if code is not None else -1), out + err

    def logs(self, ref: str, tail: int = 80) -> str:
        data = self.call(
            "GET",
            f"/containers/{urllib.parse.quote(ref, safe='')}/logs",
            {"stdout": "true", "stderr": "true", "tail": str(tail)},
            raw=True,
        )
        out, err = _demux(data)
        return out + err

    def wait_exit(self, ref: str, seconds: float = 600.0) -> int:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            state = self.inspect(ref)["State"]
            if not state.get("Running"):
                return int(state.get("ExitCode") or 0)
            time.sleep(1)
        raise TimeoutError(f"{ref} still running after {seconds:.0f} s")


def _demux(data: bytes) -> tuple[str, str]:
    """The engine's multiplexed stream (no TTY): 8-byte headers, stream 1 or 2."""
    out, err = bytearray(), bytearray()
    i = 0
    if data[:1] not in (b"\x00", b"\x01", b"\x02") or len(data) < 8:
        return data.decode("utf-8", "replace"), ""
    while i + 8 <= len(data):
        kind, size = data[i], struct.unpack(">I", data[i + 4 : i + 8])[0]
        chunk = data[i + 8 : i + 8 + size]
        (err if kind == 2 else out).extend(chunk)
        i += 8 + size
    return out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
