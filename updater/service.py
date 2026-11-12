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

**Only the current updater answers (6.6).** The handover (`kit.handover`)
says which side of a handover this updater is on. While it is a successor
proving itself, a standby watching its successor, or a `-previous` watching
the canonical updater, a tick is the handover's and no request is taken.
When it becomes current -- taking over at H5, taking back over, or taking
over from a broken updater -- it resumes the applies the other one left, by
their journals.

`update_updater` (C2) runs `prepare.updater_only` and then the handover; only
the pin's updater line changes.

**A status nothing will settle (#443).** Only a journal is resumed, and only
an apply or a go-back keeps one. A prepare, a discard or an `update_updater`
the updater was stopped in -- or an apply taken and stopped before its
journal began, or any request stopped between its record and its status --
would leave `status.json` saying `accepted` or `running` for good, and the
app refuses every request while it does. So once the journals are resumed,
`settle_status` looks at it: a request with a record has its status
rewritten from the record; one with an unfinished journal, or a handover of
its own, is left to those; anything else is recorded as interrupted -- a
prepare `refused`, the rest `not_started`, the code `interrupted` -- unless a
prepare got as far as its report, which makes it `succeeded`. The files and
states are protocol 1's, so an app of any release reads the result.

**A container whose storage a power cut took (#262)** makes the engine fail
every full listing it is in. Before anything else at startup, while that is
so, the updater force-removes the one-offs of each unfinished apply that are
in that state -- only those, by the names it gave them
(`oneoff.name_for`), never the `-previous` app or anything else -- and the
resume goes on as after any interruption. If the listing still fails, the
startup is not attempted: the sentence (`Service.problem`) goes into the
heartbeat and into the unfinished apply's status, and the startup is tried
again after a growing pause, rather than the process exiting into its
restart policy over and over with nobody told.
"""

from __future__ import annotations

import contextlib
import os
import threading

from updater import contract, detect, goback, intake, journal, oneoff, prepare, survey, volume
from updater import engine as eng
from updater.apply import Apply
from updater.recovery import Recovery, apply_journals
from updater.rejoin import Rejoin
from updater.site import Kit, Records
from updater.volume import REQUEST_OWNER_UID

TICK_SECONDS = 2.0
#: The longest pause between two startups that could not list the project (#262).
STUCK_MAX_SECONDS = 60.0


class Stuck(Exception):
    """The startup cannot go on, for the reason in the sentence (#262)."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


def stuck_sentence(message: str) -> str:
    return (
        "The updater cannot go on: the container engine will not list this installation's "
        f"containers ({message}). A container named there may have lost its files, for instance "
        "in a power cut; removing that container (docker rm -f, or podman rm -f, and its id) "
        "lets the updater carry on by itself."
    )


class Service:
    def __init__(self, kit: Kit, owner_uid: int = REQUEST_OWNER_UID) -> None:
        self.kit = kit
        self.owner_uid = owner_uid
        self.busy = False
        self.resume_pending = True
        self.lock = threading.Lock()
        #: Why the startup cannot go on, for the heartbeat; None while it can (#262).
        self.problem: str | None = None
        #: Part 11: the recovery page's requests, and when recovery mode starts (#163).
        self.recovery = Recovery(kit, owner_uid)
        #: #275: the app rejoins its sidecar's network after the sidecar restarts.
        self.rejoin = Rejoin(kit, self.idle)

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

    @property
    def heartbeat_problem(self) -> str | None:
        """The heartbeat's `problem`: the startup's sentence first, then the rejoin's (#262, #275)."""
        return self.problem or self.rejoin.problem

    def idle(self) -> bool:
        """Nothing in flight, by everything kept on the volume: what #275's repair waits for."""
        if self.busy or self.resume_pending or self.kit.handover.mode != "current":
            return False
        if os.path.lexists(self.vol.request) or os.path.lexists(self.vol.recovery_request):
            return False
        if any((self.vol.root / "journal").glob(f"{intake.INTAKE_PREFIX}*.taken")):
            return False
        if self.unfinished():
            return False
        return not any(self.recovery.state_of(j) for j in apply_journals(self.vol))

    @property
    def heartbeat_role(self) -> str | None:
        """What `updater.json` should say about this updater, or None for nothing (6.6)."""
        return self.kit.handover.heartbeat_role

    def startup(self) -> None:
        self.vol.init()
        self.clear_broken()
        handover = self.kit.handover
        handover.startup()
        if handover.mode != "current":
            # A successor before H5, a standby, or a `-previous` watching the
            # canonical updater: none of them takes requests or resumes an apply.
            self.resume_pending = False
            return
        if os.path.lexists(self.vol.root / "journal"):
            leftovers = list((self.vol.root / "journal").glob(f"{intake.INTAKE_PREFIX}*.taken"))
            if leftovers:
                ctx = self.context()
                for outcome in intake.resume_intake(
                    self.vol, ctx, self.kit.clock.now(), self.owner_uid, self.kit.site.me
                ):
                    self.dispatch(outcome)
        with contextlib.suppress(eng.EngineUnavailable):
            self.recovery.startup()
        self.resume_journals()

    def clear_broken(self) -> list[str]:
        """#262: this installation's containers can be listed, or `Stuck`. Returns what it removed.

        Only when the full listing fails: then each unfinished apply's own
        one-offs whose storage is gone are force-removed by name, and the
        listing is asked again.
        """
        client = self.kit.client
        try:
            client.containers()
            return []
        except eng.EngineError as e:
            if e.status < 500:
                raise
        removed = []
        for j in self.unfinished():
            app_name = j.context.get("app_name")
            if not isinstance(app_name, str):
                continue
            for role in eng.ONEOFF_ROLES:
                name = oneoff.name_for(app_name, role, j.id)
                if client.remove_broken_oneoff(name):
                    removed.append(name)
                    self.note(j, f"Removed {name}: the container engine had lost its files.")
        try:
            client.containers()
        except eng.EngineError as e:
            if e.status < 500:
                raise
            sentence = stuck_sentence(e.message)
            for j in self.unfinished():
                self.note(j, sentence)
            raise Stuck(sentence) from e
        return removed

    def note(self, j: journal.Journal, sentence: str) -> None:
        """One sentence added to an unfinished apply's status, after those it has."""
        records = Records(self.vol, j.id, "apply", self.kit.clock)
        seen = volume.read_own_json(self.vol.status) or {}
        if seen.get("id") == j.id and isinstance(seen.get("sentences"), list):
            if seen["sentences"] and seen["sentences"][-1] == sentence:
                return
            records.sentences = [str(x) for x in seen["sentences"]]
        records.say(sentence, step=j.step)

    def unfinished(self) -> list[journal.Journal]:
        found = []
        for path in sorted((self.vol.root / "journal").glob("*.json")):
            rid = path.name[: -len(".json")]
            if not contract.is_uuid4(rid) or os.path.lexists(self.vol.history(rid)):
                continue
            j = journal.load(self.vol, rid)
            if j is not None and j.kind in contract.JOURNAL_KINDS:
                found.append(j)
        return found

    def resume_journals(self, restarted: bool = True) -> list[str]:
        """Resume every unfinished apply. `restarted`: this updater has just started, so
        the time since each journal's last step is a gap (8.6) -- not so for one that
        has just taken over or taken back an apply another updater was running."""
        outcomes = []
        handover = self.kit.handover
        try:
            # A handover this updater started settles first: it decides who
            # owns an apply taken at 2a (5.6).
            handover.recover()
            if handover.mode != "current":
                self.resume_pending = False
                return outcomes
            for j in self.unfinished():
                started = [e.get("at") for e in j.started if isinstance(e, dict)]
                if restarted and started and isinstance(started[-1], str):
                    try:
                        away = self.kit.clock.now() - contract.parse_iso(started[-1])
                        self.kit.clock.record_gap(max(0.0, away), "updater restarted")
                    except ValueError:
                        pass
                self.busy = True
                try:
                    outcomes.append(goback.for_journal(self.kit, j).resume())
                finally:
                    self.busy = False
            self.resume_pending = False
        except eng.EngineUnavailable:
            self.resume_pending = True
        handover.flush()
        if not self.resume_pending:
            with contextlib.suppress(OSError, ValueError, volume.UnsafeFile):
                self.settle_status()
        return outcomes

    def settle_status(self) -> str | None:
        """#443: `status.json` names a request nothing is running any more; settle it.

        Called with every journal resumed, by the current updater, before it
        takes a request: nothing of this updater's can still be running then.
        Returns the state the status was settled to, or None when it was left.
        """
        doc = volume.read_own_json(self.vol.status)
        view = contract.read_status(doc)
        if view is None or view.state in contract.SETTLED_STATES or not contract.is_uuid4(view.id):
            return None
        rid = str(view.id)
        record = volume.read_own_json(self.vol.history(rid))
        if record is not None:
            # Stopped between the record and the status, or a record the
            # handover wrote for the updater it replaced: the record decides.
            state = record.get("state")
            if state not in contract.STATES or state == view.state:
                return None
            sentence = record.get("sentence")
            status = contract.Status(
                id=rid,
                kind=view.kind or str(record.get("kind") or ""),
                state=state,
                updated_at=contract.iso(self.kit.clock.now()),
                sentences=tuple([*view.sentences, *([sentence] if isinstance(sentence, str) else [])][-20:]),
            )
            volume.write_json(self.vol.status, status.to_dict())
            return state
        if os.path.lexists(self.vol.journal(rid)) or os.path.lexists(self.vol.handover(rid, "request")):
            # An apply or go-back the journal still has (recovery's, when the
            # resume could not finish it), or a handover's own record to write.
            return None
        state, sentence = self._interrupted(view.kind, rid)
        records = Records(self.vol, rid, view.kind or "request", self.kit.clock)
        records.sentences = list(view.sentences)
        records.finish(state, sentence, requested_by=self._requested_by(rid), code=contract.INTERRUPTED)
        return state

    def _interrupted(self, kind: str | None, rid: str) -> tuple[str, str]:
        """The record of a request the updater was stopped in, with no journal of it."""
        if kind == "prepare":
            report = volume.read_own_json(self.vol.prepared(rid))
            if report is not None and isinstance(report.get("to_version"), str):
                # P7 wrote the report; only the record was missing.
                return "succeeded", f"Prepared {report['to_version']}."
            return "refused", "Preparing failed: the updater restarted before it finished."
        if kind == "apply":
            return "not_started", "Not started: the updater restarted before the update began."
        if kind == "go_back":
            return "not_started", "Not started: the updater restarted before going back began."
        if kind == "update_updater":
            return (
                "not_started",
                f"The updater stayed on {self.kit.site.me.version}: it restarted before the handover began.",
            )
        if kind == "discard":
            return (
                "not_started",
                "The updater restarted before the prepared update was discarded. "
                "Discard it again if it is still offered.",
            )
        return "not_started", "The updater restarted before this request finished."

    def _requested_by(self, rid: str) -> str | None:
        """Who asked, from the request intake kept, when it says so plainly."""
        try:
            raw = volume.read_own_json(self.vol.taken(rid))
        except (OSError, ValueError, volume.UnsafeFile):
            return None
        by = (raw or {}).get("requested_by")
        return by if isinstance(by, str) and 0 < len(by) <= contract.REQUESTED_BY_MAX else None

    def tick(self) -> intake.Outcome | None:
        self.kit.clock.tick()
        handover = self.kit.handover
        if handover.mode != "current":
            handover.tick()
            if handover.mode != "current":
                return None
            # Taken back over, or taken over: what the other one left -- a
            # recovery action half done, an apply -- is this one's now.
            with contextlib.suppress(eng.EngineUnavailable):
                self.recovery.startup()
            self.resume_journals(restarted=False)
            return None
        # Before resuming: a journal that cannot proceed is exactly when
        # recovery has to be reachable (11.1).
        self.busy = True
        try:
            self.recovery.tick(self.unfinished, busy=False)
        except eng.EngineUnavailable:
            pass
        finally:
            self.busy = False
        if self.resume_pending:
            self.resume_journals()
            if self.resume_pending:
                return None
        if not os.path.lexists(self.vol.request):
            self.rejoin.tick()
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
            if request.kind == "go_back":
                j = journal.load(self.vol, request.id)
                assert j is not None
                try:
                    return goback.GoBack(self.kit, j).run(request)
                except eng.EngineUnavailable:
                    self.resume_pending = True
                    return None
            if request.kind == "discard":
                return self.discard(request)
            if request.kind == "update_updater":
                return self.update_updater(request)
        finally:
            self.busy = False
        return None

    def update_updater(self, request: contract.Request) -> str:
        """C2: the short prepare (resolve, verify, pull the updater only), then the handover.

        No step-up and no journal (R12): it cannot touch the ledger, and the
        handover keeps its own. Only the pin's updater line changes.
        """
        records = Records(self.vol, request.id, "update_updater", self.kit.clock)
        started = self.kit.clock.now()
        me = self.kit.site.me
        try:
            successor = prepare.updater_only(self.kit, request, records)
        except prepare.PrepareFailed as e:
            records.finish("refused", e.sentence, requested_by=request.requested_by, started_at=started)
            return "refused"
        except eng.EngineUnavailable:
            records.finish(
                "refused",
                "Updating the updater failed: the container engine stopped answering.",
                requested_by=request.requested_by,
                started_at=started,
            )
            return "refused"
        except (eng.EngineError, eng.NotAllowed) as e:
            records.finish(
                "refused",
                f"Updating the updater failed: the container engine refused ({e}).",
                requested_by=request.requested_by,
                started_at=started,
            )
            return "refused"
        records.say(f"Handing over to the updater of {successor.version}.", step="H1")
        outcome = self.kit.handover.update_updater(request_id=request.id, successor=successor)
        if outcome.done:
            records.finish(
                "succeeded",
                f"The updater now runs {successor.version}.",
                requested_by=request.requested_by,
                started_at=started,
                extra={"handover": "done", "from_version": me.version, "to_version": successor.version},
            )
            return "succeeded"
        recorded = volume.read_own_json(self.vol.history(request.id))
        if recorded is None:
            # The handover wrote nothing (none available): the record is this one.
            sentence = outcome.sentence
            if not sentence.startswith("The updater stayed on"):
                sentence = f"The updater stayed on {me.version}: {sentence}"
            records.finish("not_started", sentence, requested_by=request.requested_by, started_at=started)
            return "not_started"
        return str(recorded.get("state"))

    def prepare(self, request: contract.Request) -> str:
        records = Records(self.vol, request.id, "prepare", self.kit.clock)
        started = self.kit.clock.now()
        try:
            prepare.run(self.kit, request, records)
        except prepare.PrepareFailed as e:
            records.finish(
                "refused", e.sentence, requested_by=request.requested_by, started_at=started, code=e.code
            )
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
        pause = TICK_SECONDS
        while True:
            try:
                self.startup()
                self.problem = None
                break
            except Stuck as e:
                # Said where it can be read, and tried again -- not exited (#262).
                self.problem = e.sentence
                if stop.wait(pause):
                    return
                pause = min(pause * 2, STUCK_MAX_SECONDS)
        while not stop.is_set():
            try:
                self.tick()
            except eng.EngineUnavailable:
                self.resume_pending = True
            stop.wait(TICK_SECONDS)
