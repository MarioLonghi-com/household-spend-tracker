"""The app rejoins its sidecar's network after the sidecar restarts (#275).

In the tailnet layout the app runs `network_mode: service:tailscale`, which
the engine resolves to `container:<sidecar id>` when the app is created and
to the sidecar's network namespace *as it is* when the app starts. A sidecar
that restarts -- a crash, an OOM kill, its restart policy, `docker restart` --
keeps its id and gets a new namespace; the app stays in the old one, which has
no interfaces any more, and nothing restarts it: `depends_on` does not cover
restarts, and a restart policy acts only when the app itself exits.

**A narrow duty, decided on #275.** Every `CHECK_EVERY_SECONDS`
the idle updater inspects the app and the sidecar -- a listing and two
inspects, nothing else -- and acts only when all of these hold:

- the layout is `sidecar` (the app's network mode is another container's);
- the container is this project's `app` service: not a one-off, not a
  parked `-previous`, and running;
- nothing is in flight: no request waiting or taken, no apply journal without
  its history record, no recovery open or asked for, no updater one-off
  running, no handover, no `-previous` running;
- the sidecar is running, and either its `State.StartedAt` is after the app's
  (it restarted since the app joined it: the real signal, since the id stays)
  or the app's `container:<id>` names a container that is no longer the
  sidecar (it was recreated outside compose; compose itself recreates the app
  when it recreates the sidecar).

**What it does.** For a restarted sidecar, it stops and starts the app: both
engines resolve `container:<id>` at every start -- Docker joins
`/proc/<sidecar pid>/ns/net` of the sidecar as it runs then, and libpod the
namespace of the dependency container's current process -- so a plain
restart rejoins, with no new engine call. For a sidecar with a new id, a
start would fail (the id is gone), so the app is recreated from the same
allowlist copy the apply uses (`shapes.copy_app`), with the image it runs,
its own environment unchanged, and the sidecar's current id.

**No loops.** At most `MAX_PER_HOUR` attempts in any hour, counted from the
attempts this process made and the repairs the history records. After that,
or when a repair fails, the sentence goes into the heartbeat's `problem` key
(#262) with the command that does it by hand, and it is cleared once the app
and the sidecar agree again.

Each repair is recorded as `history/<id>.json` with kind `rejoin`, so the
Updates screen shows its sentence like any outcome the updater writes.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from updater import contract, shapes, survey, volume
from updater import detect as det
from updater import engine as eng
from updater.site import Kit

CHECK_EVERY_SECONDS = 45.0
MAX_PER_HOUR = 3
WINDOW_SECONDS = 3600.0
KIND = "rejoin"
STOP_GRACE = 10
#: The command that rejoins the app by hand, in every sentence that gives up.
BY_HAND = "docker compose up -d --force-recreate app"
SIDECAR_SERVICE = "tailscale"

_ERRORS = (eng.EngineError, eng.NotAllowed, survey.NotStarted)


@dataclass(frozen=True)
class Finding:
    """What one check saw. `how` is None when the app and the sidecar agree."""

    app: dict
    sidecar: survey.Sidecar
    how: str | None  # "restart" | "recreate" | None


def started(inspect: Mapping) -> float | None:
    state = inspect.get("State") if isinstance(inspect.get("State"), dict) else {}
    return det.parse_engine_time(state.get("StartedAt"))


def _depends_on(app_inspect: Mapping) -> list[str]:
    """The services compose's `depends_on` label names, in order (`svc:condition:restart,…`)."""
    labels = (app_inspect.get("Config") or {}).get("Labels") or {}
    raw = labels.get("com.docker.compose.depends_on") if isinstance(labels, dict) else None
    if not isinstance(raw, str):
        return []
    return [part.split(":", 1)[0] for part in raw.split(",") if part.strip()]


def sidecar_now(client: eng.EngineClient, app_inspect: Mapping) -> survey.Sidecar | None:
    """The sidecar as it runs now: the container the app's mode names, or -- when that one is
    gone -- the project's container of the service the app depends on for its network."""
    mode = str((app_inspect.get("HostConfig") or {}).get("NetworkMode") or "")
    if not mode.startswith(("container:", "service:")):
        return None
    candidates = [s for s in _depends_on(app_inspect) if s not in (survey.APP_SERVICE, "updater")]
    service = candidates[0] if candidates else SIDECAR_SERVICE
    return survey.sidecar_of(client, app_inspect, service=service)


def names_sidecar(app_inspect: Mapping, sidecar: survey.Sidecar) -> bool:
    """Whether the app's `container:<ref>` is the sidecar's current id or name."""
    mode = str((app_inspect.get("HostConfig") or {}).get("NetworkMode") or "")
    ref = mode.split(":", 1)[1] if ":" in mode else ""
    return bool(ref) and (sidecar.id.startswith(ref) or ref == sidecar.name)


def judge(
    app_inspect: Mapping, sidecar: survey.Sidecar, sidecar_inspect_started: float | None
) -> str | None:
    """Pure: `restart`, `recreate`, or None when there is nothing to do (or nothing sure)."""
    if not sidecar.running:
        return None  # it cannot be joined yet; the next check sees it running
    if not names_sidecar(app_inspect, sidecar):
        return "recreate"
    app_at = started(app_inspect)
    if app_at is None or sidecar_inspect_started is None:
        return None
    return "restart" if sidecar_inspect_started > app_at else None


class Rejoin:
    """`tick(idle)` from the service's loop; `problem` for the heartbeat."""

    def __init__(self, kit: Kit, idle: Callable[[], bool]) -> None:
        self.kit = kit
        self.idle = idle
        self.checked_at: float | None = None
        #: When this process tried a repair, successful or not.
        self.attempts: list[float] = []
        self._seeded = False
        #: The heartbeat's sentence while the updater has stopped repairing, or one failed.
        self.problem: str | None = None

    @property
    def vol(self) -> volume.Volume:
        return self.kit.site.volume

    def due(self, now: float) -> bool:
        return self.checked_at is None or now - self.checked_at >= CHECK_EVERY_SECONDS

    def tick(self) -> str | None:
        """One check when due. Returns `restarted`, `recreated`, `limited`, `failed`, or None."""
        now = self.kit.clock.now()
        if not self.due(now):
            return None
        self.checked_at = now
        if not self.idle():
            return None
        try:
            found = self.look()
        except (*_ERRORS, eng.EngineUnavailable):
            return None
        if found is None:
            return None
        if found.how is None:
            # They agree: whatever was reported has been put right, here or by hand.
            self.problem = None
            return None
        if not self.idle():
            return None
        return self.repair(found, now)

    # ------------------------------------------------------------------ #

    def look(self) -> Finding | None:
        client = self.kit.client
        listing = client.containers()
        listed = survey.app_listing(listing)
        if listed is None or not survey.running(listed) or self.others_busy(listing):
            return None
        seen = client.inspect(str(listed["Id"]))
        if det.layout_of(seen) != "sidecar" or shapes.in_pod(seen):
            return None
        state = seen.get("State") if isinstance(seen.get("State"), dict) else {}
        if not state.get("Running") or state.get("Restarting"):
            return None
        sidecar = sidecar_now(client, seen)
        if sidecar is None:
            return None
        sidecar_started = det.parse_engine_time(sidecar.started_at)
        return Finding(app=seen, sidecar=sidecar, how=judge(seen, sidecar, sidecar_started))

    @staticmethod
    def others_busy(listing: list[dict]) -> bool:
        """An updater one-off or a parked `-previous` running: an update or a recovery is about."""
        for c in listing:
            if not survey.running(c):
                continue
            labels = c.get("Labels") if isinstance(c.get("Labels"), dict) else {}
            if eng.ROLE_LABEL in labels:
                return True
            if survey.service_of(labels) == survey.APP_SERVICE and any(
                n.endswith(shapes.PREVIOUS_SUFFIX) for n in survey.names(c)
            ):
                return True
        return False

    def recent(self, now: float) -> int:
        if not self._seeded:
            self._seeded = True
            self.attempts.extend(self._recorded(now))
        self.attempts = [t for t in self.attempts if now - t < WINDOW_SECONDS]
        return len(self.attempts)

    def _recorded(self, now: float) -> list[float]:
        """The repairs the history records within the hour: a restarted updater keeps counting."""
        out = []
        for path in (self.vol.root / "history").glob("*.json"):
            if not contract.is_uuid4(path.name[: -len(".json")]):
                continue
            with contextlib.suppress(OSError, ValueError, volume.UnsafeFile):
                doc = volume.read_own_json(path) or {}
                if doc.get("kind") == KIND and isinstance(doc.get("finished_at"), str):
                    at = contract.parse_iso(doc["finished_at"])
                    if now - at < WINDOW_SECONDS:
                        out.append(at)
        return out

    def repair(self, found: Finding, now: float) -> str:
        if self.recent(now) >= MAX_PER_HOUR:
            self.problem = (
                f"The app has lost its network to a Tailscale sidecar restart {MAX_PER_HOUR} times "
                "within an hour, so the updater has stopped rejoining it for now. The sidecar's log "
                f"(docker compose logs tailscale) says why it restarts; {BY_HAND} rejoins the app by hand."
            )
            return "limited"
        self.attempts.append(now)
        client = self.kit.client
        client.scope = replace(client.scope, sidecar=found.sidecar.name)
        app_id = str(found.app.get("Id"))
        try:
            if found.how == "restart":
                client.stop(app_id, grace=STOP_GRACE)
                client.start(app_id)
                sentence = (
                    "The Tailscale sidecar restarted and left the app without a network, "
                    "so the updater restarted the app inside it."
                )
            else:
                self.recreate(found)
                sentence = (
                    "The Tailscale sidecar was replaced and left the app without a network, "
                    "so the updater recreated the app inside the new one."
                )
        except (*_ERRORS, eng.EngineUnavailable) as e:
            self.problem = (
                f"The app lost its network when the Tailscale sidecar restarted, and the updater "
                f"could not rejoin it ({e}). {BY_HAND} rejoins it by hand."
            )
            return "failed"
        self.record(sentence, found, now)
        self.problem = None
        return "restarted" if found.how == "restart" else "recreated"

    def recreate(self, found: Finding) -> None:
        """The app again, from the allowlist copy (C10), joined to the sidecar's current id.

        Everything that can refuse is asked before the old container goes: the
        image it runs must be this repository's, by digest, present, and the
        body one the engine client's create guard accepts.
        """
        client = self.kit.client
        seen = found.app
        image = survey.image_of(client, seen)
        config_image = (seen.get("Config") or {}).get("Image")
        ref = survey.published_ref(image, config_image if isinstance(config_image, str) else None)
        if ref is None:
            raise survey.NotStarted("the app does not run a published image, so it cannot be recreated")
        client.inspect_image(ref)
        image_config = (image or {}).get("Config") if image else None
        body = shapes.copy_app(seen, ref, image_config=image_config, sidecar_id=found.sidecar.id)
        # The same image, so the same environment: no migration switch changes here.
        body["Env"] = shapes.own_env(seen, image_config)
        eng.guard_create(body, client.scope)
        name = shapes.name_of(seen)
        client.stop(str(seen["Id"]), grace=STOP_GRACE)
        client.remove(str(seen["Id"]), force=True)
        new_id = client.create(name, body)
        client.start(new_id)

    def record(self, sentence: str, found: Finding, now: float) -> None:
        rid = str(uuid.uuid4())
        record = contract.History(
            id=rid, kind=KIND, state="succeeded", sentence=sentence, finished_at=contract.iso(now)
        ).to_dict()
        record["sidecar"] = found.sidecar.name
        record["how"] = found.how
        volume.write_json(self.vol.history(rid), record)
