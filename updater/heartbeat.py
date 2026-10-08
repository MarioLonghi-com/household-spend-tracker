"""`updater.json`, assembled and rewritten every 30 seconds (design notes 5.3, 8.3).

`Beat` owns the two clocks: the heartbeat is written every
`HEARTBEAT_EVERY_SECONDS`, and the engine is detected again every
`detect.DETECT_EVERY_SECONDS` -- or at once while the last detection was a
refusal, so a socket that comes back is seen at the next beat rather than five
minutes later. Between detections a beat makes no engine call at all.

What a beat carries beyond detection:

- `container`, the updater's real name, read from the engine
  (`detect.find_own_container`): `spend-tracker-updater-1` under Docker
  Compose, `spend-tracker_updater_1` under podman-compose, or whatever a
  handover renamed it to. Never built from the project name.
- `layout`, from the app container's network mode (`detect.layout_of`).
- `podman_restart`, inferred (S2), and `socket_sentence`, the refusal's one
  sentence, so the screen can quote it before any request is sent.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from updater import detect as det
from updater import engine as eng
from updater.contract import UNKNOWN_ENGINE, Heartbeat, iso
from updater.volume import Volume, write_json

HEARTBEAT_EVERY_SECONDS = 30

APP_SERVICE = "app"
SERVICE_LABELS = ("com.docker.compose.service", "io.podman.compose.service")


@dataclass(frozen=True)
class Identity:
    """What the updater knows about itself without the engine."""

    updater_version: str
    image_digest: str
    role: str = "current"


def assemble(
    me: Identity,
    found: det.Detection,
    *,
    container: str,
    layout: str,
    hook: bool,
    busy: bool,
    podman_restart: str,
    now: float,
) -> Heartbeat:
    """Pure: one heartbeat from a detection and what the beat learned around it."""
    return Heartbeat(
        updater_version=me.updater_version,
        image_digest=me.image_digest,
        seen_at=iso(now),
        engine=found.engine or UNKNOWN_ENGINE,
        engine_version=found.engine_version,
        rootless=found.rootless,
        layout=layout,
        socket=found.socket,
        hook=hook,
        busy=busy,
        role=me.role,
        api_version=found.api_version,
        engine_api=found.engine_api,
        container=container,
        socket_sentence=found.sentence,
        podman_restart=podman_restart,
    )


def _service(c: dict) -> str | None:
    labels = c.get("Labels") if isinstance(c.get("Labels"), dict) else {}
    return next((labels[k] for k in SERVICE_LABELS if isinstance(labels.get(k), str)), None)


class Beat:
    """Detection plus the heartbeat file. `tick(now)` is one beat; `run` loops it."""

    def __init__(
        self,
        client: eng.EngineClient,
        volume: Volume,
        me: Identity,
        *,
        hook: bool = False,
        busy: Callable[[], bool] = lambda: False,
        mountinfo: str | None = None,
        hostname: str | None = None,
        role: Callable[[], str | None] | None = None,
    ) -> None:
        self.client = client
        self.volume = volume
        self.me = me
        self.hook = hook
        self.busy = busy
        #: The role to write, asked at every beat (6.6): `current`, `standby`
        #: while a successor proves itself, or None while another updater owns
        #: `updater.json` -- the successor before it takes over, the standby
        #: after `go`, a `-previous` watching the canonical one.
        self.role = role
        self._mountinfo = mountinfo
        self._hostname = hostname
        self.detection: det.Detection | None = None
        self.detected_at: float | None = None
        #: Kept from the last detection that could see them; a refused socket
        #: does not make the updater forget its own name.
        self.container = ""
        self.working_dir: str | None = None
        self.layout = "loopback"
        self.podman_restart = "not_applicable"

    def due(self, now: float) -> bool:
        if self.detection is None or self.detected_at is None or self.detection.refused:
            return True
        return now - self.detected_at >= det.DETECT_EVERY_SECONDS

    def redetect(self, now: float) -> det.Detection:
        known = self.working_dir
        found = det.detect(self.client, known)
        if not found.refused:
            # Not finding itself is not a refusal: the name stays the last one seen.
            with contextlib.suppress(eng.EngineError, eng.EngineUnavailable, eng.NotAllowed, OSError):
                self._look_around(found, now)
            if self.working_dir != known:
                # The project's directory is a machine's second signal, and
                # the first detection ran before the updater knew it. Once.
                found = det.detect(self.client, self.working_dir)
        self.detection, self.detected_at = found, now
        return found

    def _look_around(self, found: det.Detection, now: float) -> None:
        listing = self.client.containers()
        own = det.find_own_container(listing, self._mountinfo, self._hostname)
        if own is not None:
            self.container = det.container_name(own)
            self.working_dir = det.working_dir_of(own) or self.working_dir
            inspected = self.client.inspect(self.container)
            self.podman_restart = det.podman_restart(found, inspected, now)
        else:
            self.podman_restart = "unknown" if (found.engine or "").startswith("podman") else "not_applicable"
        app = next((c for c in listing if _service(c) == APP_SERVICE), None)
        if app is not None:
            self.layout = det.layout_of(self.client.inspect(det.container_name(app)))

    def heartbeat(self, now: float, role: str | None = None) -> Heartbeat:
        assert self.detection is not None
        return assemble(
            replace(self.me, role=role) if role else self.me,
            self.detection,
            container=self.container,
            layout=self.layout,
            hook=self.hook,
            busy=self.busy(),
            podman_restart=self.podman_restart,
            now=now,
        )

    def tick(self, now: float) -> Heartbeat | None:
        role = self.role() if self.role is not None else self.me.role
        if role is None:
            # Another updater writes `updater.json` now (6.6): not a word.
            return None
        if self.due(now):
            self.redetect(now)
        beat = self.heartbeat(now, role)
        write_json(self.volume.heartbeat, beat.to_dict())
        return beat

    def run(self, stop: threading.Event, clock: Callable[[], float] = time.time) -> None:
        while not stop.is_set():
            self.tick(clock())
            stop.wait(HEARTBEAT_EVERY_SECONDS)
