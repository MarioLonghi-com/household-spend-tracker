"""Health, asked from where requests arrive (design notes 8.4, 4.2 step 8, 4.3 R4).

A 200 alone is not health. The answer must name the expected version, and a
commit the expected revision label begins with (`/api/health` gives the
seven-character short SHA, the label the full one).

**Sidecar layout:** `wget` in the sidecar, as the sidecar's own healthcheck
asks -- the app is reachable there exactly when Tailscale can reach it.

**Loopback layout:** `python` in the app container, as the image's
`HEALTHCHECK` asks. Then the port binding, which is what the browser at
`localhost:<port>` depends on:

- where the engine runs in a VM and has a gateway name (Docker Desktop,
  `podman machine`), a short-lived probe on the default bridge asks the
  gateway at the published port, **sending `Host: localhost:<port>`**, because
  the app's allowed-hosts check answers 400 to the gateway's name (S6);
- elsewhere (Docker Engine and Podman on Linux) a port published on
  127.0.0.1 cannot be reached from a bridge container at all (S13, and the
  same on rootful Podman in spike 3), so the binding is **checked by
  inspection**: the new container publishes 8848 where the previous one did.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from updater import engine as eng
from updater import oneoff, shapes
from updater.clock import Deadline

#: 4.2 step 8 and 4.3 R4: within 120 s, gaps excluded (8.6).
HEALTH_SECONDS = 120
HEALTH_POLL_SECONDS = 2.0
PROBE_SECONDS = 30

LOCAL_URL = f"http://127.0.0.1:{shapes.APP_PORT}/api/health"
SIDECAR_PROBE = ["wget", "-q", "-O", "-", "-T", "5", LOCAL_URL]
APP_PROBE = ["python", "-c", shapes.HEALTH_SCRIPT, LOCAL_URL]


@dataclass(frozen=True)
class Expect:
    version: str
    #: The full revision label; None when the image carries none (never for a release).
    revision: str | None


def judge(body: str, expect: Expect) -> str | None:
    """None when `body` is the expected app answering; else why not, as a phrase."""
    try:
        doc = json.loads(body)
    except ValueError:
        return "it did not answer with JSON"
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        return "it did not say ok"
    if doc.get("version") != expect.version:
        return f"it answered as version {doc.get('version')}, not {expect.version}"
    if expect.revision:
        commit = doc.get("commit")
        if not (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{7,40}", commit)):
            return "it did not say which commit it runs"
        if not expect.revision.startswith(commit):
            return f"it runs commit {commit}, not {expect.revision[:7]}"
    return None


def binding_problem(previous: Mapping, current: Mapping) -> str | None:
    """Inspection's check of 8.4: the new container publishes what the previous one did."""
    wanted = (previous.get("HostConfig") or {}).get("PortBindings") or {}
    actual = (current.get("NetworkSettings") or {}).get("Ports") or {}
    for port, bindings in wanted.items():
        for b in bindings or []:
            want_port = str((b or {}).get("HostPort") or "")
            want_ip = str((b or {}).get("HostIp") or "")
            got = actual.get(port) or []
            if not any(
                str((g or {}).get("HostPort") or "") == want_port
                and str((g or {}).get("HostIp") or "") in (want_ip, "")
                for g in got
            ):
                return f"port {port} is not published on {want_ip or '*'}:{want_port}"
    return None


class Checker:
    """One health check, polled until it passes or its deadline runs out."""

    def __init__(
        self,
        client: eng.EngineClient,
        runner: oneoff.Runner,
        *,
        engine: str,
        sleep: Callable[[float], None],
    ) -> None:
        self.client = client
        self.runner = runner
        self.engine = engine
        self.sleep = sleep

    def once(
        self,
        *,
        app_name: str,
        sidecar_name: str | None,
        expect: Expect,
        previous: Mapping,
        image: str,
        request_id: str,
    ) -> str | None:
        """One round of 8.4's probes. None when healthy; else why not."""
        try:
            if sidecar_name:
                code, out = self.client.probe_output(sidecar_name, SIDECAR_PROBE)
                if code != 0:
                    return "it did not answer inside the sidecar"
                return judge(out, expect)
            code, out = self.client.probe_output(app_name, APP_PROBE)
            if code != 0:
                return "it did not answer inside its container"
            problem = judge(out, expect)
            if problem:
                return problem
            return self._binding(app_name, expect, previous, image, request_id)
        except eng.EngineError as e:
            return f"the engine said {e.message or e.status}"

    def _binding(
        self, app_name: str, expect: Expect, previous: Mapping, image: str, request_id: str
    ) -> str | None:
        port = shapes.published_port(previous)
        if port is None:
            return None
        gateway = shapes.GATEWAYS.get(self.engine)
        if gateway is None:
            return binding_problem(previous, self.client.inspect(app_name))
        body = shapes.port_probe(previous, image, request_id, gateway, port)
        name = oneoff.name_for(app_name, "probe", request_id)
        try:
            result = self.runner.run(name, body, PROBE_SECONDS, stderr=False)
        except oneoff.TimedOut:
            return f"nothing answered at {gateway}:{port}"
        if result.exit_code != 0:
            return f"nothing answered at {gateway}:{port}"
        return judge(result.output, expect)

    def wait(self, deadline: Deadline, **kw) -> str | None:
        """Probe until healthy (None) or the deadline passes (the last reason)."""
        while True:
            problem = self.once(**kw)
            if problem is None:
                return None
            if deadline.expired():
                return problem
            self.sleep(HEALTH_POLL_SECONDS)
