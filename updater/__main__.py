"""`python -m updater`: the updater's container runs this (6.3).

    python -m updater [--socket /run/engine.sock] [--update /update]
                      [--project-dir /project] [--project spend-tracker]

It finds its own container, learns its own digest from it (R27), detects the
engine, and then runs the heartbeat (`updater.heartbeat`) and the request loop
(`updater.service`) until it is stopped.

There is no option, variable or argument that changes how images are
verified: `trust.Sigstore` is the only trust this entry point builds.
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path

from updater import detect, heartbeat, shapes, survey
from updater import engine as eng
from updater.handover import NotAvailable
from updater.journal import Owner
from updater.service import Service
from updater.site import Kit, Site
from updater.trust import Sigstore, Trust
from updater.volume import Volume

DEFAULT_PROJECT = "spend-tracker"
UPDATE_MOUNT = "/update"


def identify(
    client: eng.EngineClient, mountinfo: str | None = None, hostname: str | None = None
) -> tuple[Owner, str | None]:
    """This updater as the journal names it, and the engine name of its `update` volume.

    Its digest comes from its own image's `RepoDigests`, read by image id (R27).
    Outside a container -- the CI job runs it as a process -- it is unnamed.
    """
    own = detect.find_own_container(client.containers(), mountinfo, hostname)
    if own is None:
        return Owner(image_digest="", version="0.0.0", container=""), None
    name = detect.container_name(own)
    seen = client.inspect(name)
    image = survey.image_of(client, seen) or {}
    digest = ""
    for ref in image.get("RepoDigests") or []:
        m = eng.IMAGE_BY_DIGEST.fullmatch(ref) if isinstance(ref, str) else None
        if m and m.group(1) == eng.REPOSITORIES[1]:
            digest = m.group(2)
    labels = (image.get("Config") or {}).get("Labels") or {}
    version = detect.label_version(labels.get(detect.VERSION_LABEL)) or "0.0.0"
    return Owner(image_digest=digest, version=version, container=name), shapes.volume_at(seen, UPDATE_MOUNT)


def build(args: argparse.Namespace, trust: Trust) -> tuple[Kit, heartbeat.Identity]:
    client = eng.EngineClient(args.socket, eng.Scope(project=args.project))
    client.negotiate()
    me, update_volume = identify(client)
    found = detect.detect(client)
    site = Site(
        project=args.project,
        volume=Volume(Path(args.update)),
        update_volume=args.update_volume or update_volume or f"{args.project}_update",
        project_dir=Path(args.project_dir)
        if args.project_dir and os.path.isdir(args.project_dir)
        else None,
        me=me,
        engine=found.engine or "docker-engine",
        hook_dir=Path(args.hook) if args.hook and os.path.isdir(args.hook) else None,
    )
    kit = Kit(client=client, site=site, trust=trust, handover=NotAvailable())
    return kit, heartbeat.Identity(updater_version=me.version, image_digest=me.image_digest)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m updater", description=__doc__.splitlines()[0])
    p.add_argument("--socket", default="/run/engine.sock")
    p.add_argument("--update", default=UPDATE_MOUNT)
    p.add_argument("--update-volume", default=None, help="the engine's name for the update volume")
    p.add_argument("--project-dir", default="/project")
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--hook", default="/hook")
    return p


def serve(args: argparse.Namespace, trust: Trust) -> None:
    kit, identity = build(args, trust)
    service = Service(kit)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    # Its own client: detection renegotiates, and must not do so under a step.
    beat_client = eng.EngineClient(args.socket, eng.Scope(project=args.project))
    beat = heartbeat.Beat(
        beat_client,
        kit.site.volume,
        identity,
        hook=kit.site.hook_dir is not None,
        busy=lambda: service.busy,
    )
    threading.Thread(target=beat.run, args=(stop,), daemon=True).start()
    service.run(stop)


def main(argv: list[str] | None = None) -> int:
    serve(parser().parse_args(argv), Sigstore())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
