"""The updater's loop: take a request, act on it, write back; resume what a crash left (5.4-5.6).

On start, `startup` lays the volume out, answers any request a crash left
half-taken (`intake.resume_intake`), and resumes every apply whose journal has
no history record yet (5.6). Then `tick` runs every couple of seconds: when
`request.json` exists, detection is taken *first* -- validation answers from
it and from the volume, so a refusal makes no engine call (U1) -- and the
request is taken and dispatched. One request at a time: while one runs, the
service is busy and another is refused, not queued.

The engine going away mid-apply (`EngineUnavailable`) leaves the journal as
it was; the next tick that reaches the engine resumes it. A fresh start of
the updater is an engine restart too (8.6): `startup` records the time since
the journal's last step as a gap, so no deadline counts it.
"""

from __future__ import annotations

import contextlib
import os
import threading

from updater import contract, detect, intake, journal, prepare, survey, volume
from updater import engine as eng
from updater.apply import Apply
from updater.site import Kit, Records
from updater.volume import REQUEST_OWNER_UID

TICK_SECONDS = 2.0


class Service:
    def __init__(self, kit: Kit, owner_uid: int = REQUEST_OWNER_UID) -> None:
        self.kit = kit
        self.owner_uid = owner_uid
        self.busy = False
        self.resume_pending = True
        self.lock = threading.Lock()

    @property
    def vol(self) -> volume.Volume:
        return self.kit.site.volume

    # ------------------------------------------------------------------ #

    def context(self) -> contract.Context:
        """Detection and the running app, read before the request is (U1)."""
        found = detect.detect(self.kit.client)
        app = contract.RunningApp(version=None, revision=None, published=False)
        if not found.refused:
            with contextlib.suppress(
                survey.NotStarted, eng.EngineError, eng.NotAllowed, eng.EngineUnavailable
            ):
                app = survey.app(self.kit.client, pod_ok=True).running_app  # type: ignore[assignment]
        return contract.Context(
            app=app,
            updater_version=self.kit.site.me.version,
            socket=found.socket,
            busy=self.busy,
            socket_sentence=found.sentence,
        )

    def startup(self) -> None:
        self.vol.init()
        if os.path.lexists(self.vol.root / "journal"):
            leftovers = list((self.vol.root / "journal").glob(f"{intake.INTAKE_PREFIX}*.taken"))
            if leftovers:
                ctx = self.context()
                for outcome in intake.resume_intake(
                    self.vol, ctx, self.kit.clock.now(), self.owner_uid, self.kit.site.me
                ):
                    self.dispatch(outcome)
        self.resume_journals()

    def unfinished(self) -> list[journal.Journal]:
        found = []
        for path in sorted((self.vol.root / "journal").glob("*.json")):
            rid = path.name[: -len(".json")]
            if not contract.is_uuid4(rid) or os.path.lexists(self.vol.history(rid)):
                continue
            j = journal.load(self.vol, rid)
            if j is not None and j.kind == "apply":
                found.append(j)
        return found

    def resume_journals(self) -> list[str]:
        outcomes = []
        try:
            for j in self.unfinished():
                started = [e.get("at") for e in j.started if isinstance(e, dict)]
                if started and isinstance(started[-1], str):
                    try:
                        away = self.kit.clock.now() - contract.parse_iso(started[-1])
                        self.kit.clock.record_gap(max(0.0, away), "updater restarted")
                    except ValueError:
                        pass
                self.busy = True
                try:
                    outcomes.append(Apply(self.kit, j).resume())
                finally:
                    self.busy = False
            self.resume_pending = False
        except eng.EngineUnavailable:
            self.resume_pending = True
        return outcomes

    def tick(self) -> intake.Outcome | None:
        self.kit.clock.tick()
        if self.resume_pending:
            self.resume_journals()
            if self.resume_pending:
                return None
        if not os.path.lexists(self.vol.request):
            return None
        ctx = self.context()
        outcome = intake.take(self.vol, ctx, self.kit.clock.now(), self.owner_uid, self.kit.site.me)
        if outcome is not None:
            self.dispatch(outcome)
        return outcome

    # ------------------------------------------------------------------ #

    def dispatch(self, outcome: intake.Outcome) -> str | None:
        request = outcome.request
        if request is None or request.kind == "ping":
            return None
        self.busy = True
        try:
            if request.kind == "prepare":
                return self.prepare(request)
            if request.kind == "apply":
                j = journal.load(self.vol, request.id)
                assert j is not None
                try:
                    return Apply(self.kit, j).run(request)
                except eng.EngineUnavailable:
                    self.resume_pending = True
                    return None
            if request.kind == "discard":
                return self.discard(request)
            if request.kind == "update_updater":
                Records(self.vol, request.id, request.kind, self.kit.clock).finish(
                    "not_started",
                    "Updating the updater on its own is not available in this updater yet.",
                    requested_by=request.requested_by,
                )
                return "not_started"
        finally:
            self.busy = False
        return None

    def prepare(self, request: contract.Request) -> str:
        records = Records(self.vol, request.id, "prepare", self.kit.clock)
        started = self.kit.clock.now()
        try:
            prepare.run(self.kit, request, records)
        except prepare.PrepareFailed as e:
            records.finish("refused", e.sentence, requested_by=request.requested_by, started_at=started)
            return "refused"
        except eng.EngineUnavailable:
            records.finish(
                "refused",
                "Preparing failed: the container engine stopped answering.",
                requested_by=request.requested_by,
                started_at=started,
            )
            return "refused"
        except (eng.EngineError, eng.NotAllowed) as e:
            records.finish(
                "refused",
                f"Preparing failed: the container engine refused ({e}).",
                requested_by=request.requested_by,
                started_at=started,
            )
            return "refused"
        records.finish(
            "succeeded",
            f"Prepared {request.to_version}.",
            requested_by=request.requested_by,
            started_at=started,
        )
        return "succeeded"

    def discard(self, request: contract.Request) -> str:
        """R22: the updater deletes the report on discard."""
        path = self.vol.prepared(str(request.prepared_id))
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        Records(self.vol, request.id, "discard", self.kit.clock).finish(
            "succeeded", "The prepared update was discarded.", requested_by=request.requested_by
        )
        return "succeeded"

    def run(self, stop: threading.Event) -> None:
        self.startup()
        while not stop.is_set():
            try:
                self.tick()
            except eng.EngineUnavailable:
                self.resume_pending = True
            stop.wait(TICK_SECONDS)
