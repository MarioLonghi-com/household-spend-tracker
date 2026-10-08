"""What the project looks like right now: the app, its parked predecessor, the sidecar.

Re-read before every decision (8.6): after a gap, an engine restart or a crash
the updater trusts nothing it remembered about containers, only their names
and ids in the journal, and looks again.

**Two naming schemes (S5).** Docker Compose names the app `<project>-app-1`
and labels it `com.docker.compose.*`; podman-compose names it
`<project>_app_1`, labels it `io.podman.compose.*` as well and sets no
`oneoff` label. The app is therefore found by its *service* label, under
either scheme, never by a name built here: a container of service `app` that
is not a one-off and is not a parked `-previous`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from updater import detect as det
from updater import engine as eng
from updater import shapes

APP_SERVICE = "app"
SERVICE_LABELS = ("com.docker.compose.service", "io.podman.compose.service")


class NotStarted(Exception):
    """A reason not to start, found before anything changed. `sentence` is the owner's."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


def service_of(labels: object) -> str | None:
    if not isinstance(labels, dict):
        return None
    return next((labels[k] for k in SERVICE_LABELS if isinstance(labels.get(k), str)), None)


def names(c: Mapping) -> list[str]:
    return [str(n).lstrip("/") for n in c.get("Names") or []]


def find(client: eng.EngineClient, name: str) -> dict | None:
    """The project's container called `name` (or with that id), or None."""
    for c in client.containers():
        if name in names(c) or c.get("Id") == name:
            return c
    return None


def is_oneoff(c: Mapping) -> bool:
    labels = c.get("Labels") if isinstance(c.get("Labels"), dict) else {}
    return labels.get(eng.ONEOFF_LABEL) == "True" or eng.ROLE_LABEL in labels


def running(c: Mapping | None) -> bool:
    if c is None:
        return False
    state = c.get("State")
    if isinstance(state, dict):
        return bool(state.get("Running"))
    return state == "running"


def app_listing(listing: list[dict]) -> dict | None:
    """The app container in a project listing: service `app`, not a one-off, not parked."""
    found = [
        c
        for c in listing
        if service_of(c.get("Labels")) == APP_SERVICE
        and not is_oneoff(c)
        and not any(n.endswith(shapes.PREVIOUS_SUFFIX) for n in names(c))
    ]
    found.sort(key=lambda c: not running(c))
    return found[0] if found else None


@dataclass(frozen=True)
class Sidecar:
    """The container whose network namespace the app joins (8.4)."""

    id: str
    name: str
    service: str | None
    started_at: str | None
    running: bool


def sidecar_of(
    client: eng.EngineClient, app_inspect: Mapping, service: str | None = None
) -> Sidecar | None:
    """The sidecar as it is **now**.

    From the app's `container:<id>` network mode, or -- when that container is
    gone because the sidecar was recreated -- the project's container of the
    sidecar's service.
    """
    mode = str((app_inspect.get("HostConfig") or {}).get("NetworkMode") or "")
    if not mode.startswith(("container:", "service:")):
        return None
    ref = mode.split(":", 1)[1]
    listing = client.containers()
    chosen = None
    for c in listing:
        if str(c.get("Id", "")).startswith(ref) or ref in names(c):
            chosen = c
    if service is not None:
        current = [c for c in listing if service_of(c.get("Labels")) == service and not is_oneoff(c)]
        current.sort(key=lambda c: not running(c))
        if current and (chosen is None or not running(chosen)):
            chosen = current[0]
    if chosen is None:
        return None
    seen = client.inspect(chosen["Id"])
    state = seen.get("State") if isinstance(seen.get("State"), dict) else {}
    return Sidecar(
        id=str(seen.get("Id")),
        name=shapes.name_of(seen) or names(chosen)[0],
        service=service_of((seen.get("Config") or {}).get("Labels")),
        started_at=state.get("StartedAt"),
        running=bool(state.get("Running")),
    )


@dataclass(frozen=True)
class App:
    """The running app, inspected, and what follows from it."""

    inspect: dict
    name: str
    layout: str
    ledger_volume: str
    #: The old image's configuration, read by image id (R27); None if unreadable.
    image: dict | None
    #: `repo@sha256:…` of the old image, when it is a published one.
    image_ref: str | None
    running_app: object


def image_of(client: eng.EngineClient, inspect: Mapping) -> dict | None:
    """The image a project container runs, by id (R27). None when it cannot be read."""
    image_id = inspect.get("Image")
    if not isinstance(image_id, str):
        return None
    try:
        return client.inspect_image_id(image_id)
    except (eng.EngineError, eng.NotAllowed):
        return None


def published_ref(image: Mapping | None, config_image: str | None) -> str | None:
    """This repository's app image by digest, from `RepoDigests` or the container's own reference."""
    refs = [config_image, *((image or {}).get("RepoDigests") or [])]
    for ref in refs:
        m = eng.IMAGE_BY_DIGEST.fullmatch(ref) if isinstance(ref, str) else None
        if m and m.group(1) == eng.REPOSITORIES[0]:
            return ref
    return None


def app(client: eng.EngineClient, name: str | None = None, *, pod_ok: bool = False) -> App:
    """Find and inspect the app. Raises `NotStarted` with the sentence when it cannot go on.

    `pod_ok` is for detection, which needs only what the app runs: a request
    is then refused for the pod itself, not mistaken for a local build.
    """
    if name is None:
        listed = app_listing(client.containers())
        if listed is None:
            raise NotStarted("The updater cannot find the app container in its project.")
        name = names(listed)[0]
    elif find(client, name) is None:
        raise NotStarted(f"The updater cannot find the app container {name}.")
    seen = client.inspect(name)
    if shapes.in_pod(seen) and not pod_ok:
        raise NotStarted(shapes.POD_SENTENCE)
    ledger = shapes.volume_at(seen, shapes.LEDGER_PATH)
    if not ledger:
        raise NotStarted(
            f"The app keeps its ledger somewhere other than a named volume at {shapes.LEDGER_PATH}, "
            "so the updater cannot back it up."
        )
    image = image_of(client, seen)
    config_image = (seen.get("Config") or {}).get("Image")
    ref = published_ref(image, config_image if isinstance(config_image, str) else None)
    return App(
        inspect=seen,
        name=shapes.name_of(seen) or name,
        layout=det.layout_of(seen),
        ledger_volume=ledger,
        image=(image or {}).get("Config") if image else None,
        image_ref=ref,
        running_app=det.running_app(seen, [ref] if ref else ()),
    )
