"""Where the orchestration runs, what it acts through, and how it writes back (5.3).

`Site` is what the updater knows about its own installation: its compose
project, the `update` volume (mounted at `/update`, and the volume's engine
name, which the one-offs mount), the project directory where the pin is
written (`/project`, S1), its own identity, and the engine detection found.
`Kit` bundles the tools every step uses. `Records` writes `status.json` and
`history/<id>.json` the same way for prepare and apply.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from updater import contract, volume
from updater import engine as eng
from updater.clock import GapClock
from updater.handover import Handover, NotAvailable
from updater.journal import Owner
from updater.oneoff import Runner
from updater.trust import Trust
from updater.volume import Volume

APP_REPOSITORY = eng.REPOSITORIES[0]
UPDATER_REPOSITORY = eng.REPOSITORIES[1]


@dataclass(frozen=True)
class Site:
    project: str
    volume: Volume
    #: The engine's name for the `update` volume, which one-offs mount.
    update_volume: str
    #: Where `.env` and `pin/` live (`/project` in the updater's container).
    project_dir: Path | None
    me: Owner
    #: Detection's engine: `docker-engine`, `docker-desktop`, `podman`, `podman-machine`.
    engine: str
    #: `/hook`, when the server-only pre-update hook is mounted (6.5).
    hook_dir: Path | None = None


@dataclass
class Kit:
    client: eng.EngineClient
    site: Site
    trust: Trust
    clock: GapClock = field(default_factory=GapClock)
    sleep: Callable[[float], None] = time.sleep
    #: `handover.Successions` in the updater's container (`__main__`); none in a bare kit.
    handover: Handover = field(default_factory=NotAvailable)
    #: How often a one-off or a health check is polled.
    poll: float = 1.0

    @property
    def runner(self) -> Runner:
        return Runner(self.client, self.clock, self.sleep, self.poll)


class Records:
    """`status.json` while a request runs, `history/<id>.json` when it ends."""

    def __init__(self, vol: Volume, request_id: str, kind: str, clock: GapClock) -> None:
        self.vol = vol
        self.id = request_id
        self.kind = kind
        self.clock = clock
        self.sentences: list[str] = []

    def say(self, sentence: str, step: str | None = None, state: str = "running") -> None:
        self.sentences.append(sentence)
        status = contract.Status(
            id=self.id,
            kind=self.kind,
            state=state,
            updated_at=contract.iso(self.clock.now()),
            step=step,
            sentences=tuple(self.sentences[-20:]),
        )
        volume.write_json(self.vol.status, status.to_dict())

    def finish(
        self,
        state: str,
        sentence: str,
        *,
        requested_by: str | None = None,
        failed_step: str | None = None,
        backup: str | None = None,
        started_at: float | None = None,
        log_tail: tuple[str, ...] = (),
        extra: dict | None = None,
    ) -> dict:
        now = self.clock.now()
        record = contract.History(
            id=self.id,
            kind=self.kind,
            state=state,
            sentence=sentence,
            finished_at=contract.iso(now),
            requested_by=requested_by,
            failed_step=failed_step,
            backup=backup,
            started_at=contract.iso(started_at) if started_at else None,
            duration_s=round(now - started_at - self.clock.asleep(), 1) if started_at else None,
            gap_s=round(self.clock.asleep(), 1),
            log_tail=log_tail,
        ).to_dict()
        if extra:
            # Files only ever gain keys within protocol 1 (C4).
            for key, value in extra.items():
                record.setdefault(key, value)
        volume.write_json(self.vol.history(self.id), record)
        self.say(sentence, state=state)
        return record
