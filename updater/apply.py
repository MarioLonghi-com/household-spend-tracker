"""Apply, its automatic rollback, and resuming either after a crash (design notes 4.2, 4.3, 5.6).

## Apply (4.2)

Each step is written to the journal **before** it starts.

| # | Step | If it fails |
|---|---|---|
| 0 | The request was validated and the journal begun, holding the recovery code's hash (`intake`). | refused |
| 1 | **Preflight**: the app runs the prepared `from` version; both prepared images are present and still verify; the sidecar runs; disk and memory (8.5); the copy of the app is a shape the engine client accepts. | not started |
| 2 | The pre-update hook, if one is configured (6.5). | not started |
| 2a | **Updater first** (C1), through the handover interface (`updater.handover`). | the old updater carries on from 3 |
| 3 | Stop the app (30 s) and rename it `<name>-previous` at once. | started again under its name; not started |
| 4 | The maintenance page, from the **old** image (6.4). | logged |
| 5 | **The drill** from the new image: `scripts.upgrade --yes --report`. | no verified backup: R3; else R1 |
| 6 | Stop the maintenance page. | removed by force |
| 7 | The new app: an allowlist copy of the previous one (C10). | R1 |
| 8 | Health from where requests arrive (8.4); one extra recreate if the sidecar restarted. | R1 |
| 9 | The pin, the previous container's configuration, pruning, the recovery code invalidated. | logged |
| 10 | The handover, unless 2a did it or this updater is newer (never downgrade). | the updater stays behind |

## Rollback (4.3)

R1 removes the new app and the maintenance page; R2 restores the drill's
backup **with the old image**; R3 renames `-previous` back and starts it; R4
checks its health against the old version and commit; R5 records *rolled
back*. A failure inside the rollback is retried from the step that failed;
**three attempts in all** (entering the rollback is one, each resumption
another), then `needs_recovery`: the recovery code stays valid, nothing
serves the ledger, and the maintenance page starts in recovery mode (Part 11,
#163).

**Never a half-migrated ledger, by construction.** No container is ever
created with `SPENDTRACKER_AUTO_MIGRATE=1`, and a new app is created only
after the drill reported success. An app that meets a stamp its code does not
match is refused by `schema_check`.

## Resuming (5.6)

`resume()` re-inspects (8.6) and asks `journal.resume` which branch of 5.6
applies; this module carries it out. The drill is never started twice: a
drill container still running is waited for, a finished one is judged by its
report, and a vanished one by the backup folder it left (one backup folder
per request).

**The engine going away** (`EngineUnavailable`) is not a failure of a step:
it propagates, and the service resumes the journal when the engine answers
again. Every other engine error inside a step is that step failing.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import replace
from pathlib import Path

from updater import contract, health, journal, oneoff, pin, prepare, shapes, survey, verify, volume
from updater import engine as eng
from updater.clock import Deadline
from updater.handover import goes_first, protocol_window, runs, stays_newer
from updater.journal import Action, Journal, Observed, Owner
from updater.site import APP_REPOSITORY, UPDATER_REPOSITORY, Kit, Records

DRILL_SECONDS = 30 * 60
RESTORE_SECONDS = 15 * 60
FIND_BACKUP_SECONDS = 5 * 60
PRUNE_SECONDS = 5 * 60
STOP_GRACE_SECONDS = 30
HOOK_DEFAULT_SECONDS = 300
#: How many update backups are protected (A12, B9).
KEEP_BACKUPS = 5
#: How often the drill's remaining time is saved to the journal while it runs.
SAVE_DEADLINE_EVERY = 30.0

DRILL_REPORT = "drill.json"

#: The owner's sentences for each way an apply ends (Part 12).
NOT_MIGRATED = "nothing was migrated"
SENTENCE_BY_EXIT = {
    1: "the backup could not be taken or verified; nothing was migrated",
    3: "it failed while migrating",
    4: "the result did not verify: migrations were left pending",
    5: "the result did not verify: the key no longer opens the ledger's secrets",
    6: "the result did not verify: a table lost rows",
}
DID_NOT_START = "the new version did not start"
DID_NOT_ANSWER = "the new version did not answer"
INTERRUPTED = "the update was interrupted"


class StepFailed(Exception):
    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


_STEP_ERRORS = (eng.EngineError, eng.NotAllowed, oneoff.TimedOut, StepFailed, ValueError)


def previous_name(app_name: str) -> str:
    return app_name + shapes.PREVIOUS_SUFFIX


class Apply:
    """One apply request, from its journal. `run()` from step 0, or `resume()`."""

    def __init__(self, kit: Kit, j: Journal) -> None:
        self.kit = kit
        self.client = kit.client
        self.vol = kit.site.volume
        self.j = j
        self.ctx = j.context
        self.records = Records(self.vol, j.id, "apply", kit.clock)
        self.runner = kit.runner
        self.notes: list[str] = []

    # ------------------------------------------------------------------ #
    # Bookkeeping
    # ------------------------------------------------------------------ #

    @property
    def id(self) -> str:
        return self.j.id

    @property
    def to(self) -> str:
        return str(self.ctx["to_version"])

    @property
    def app_name(self) -> str:
        return str(self.ctx["app_name"])

    @property
    def new_ref(self) -> str:
        return f"{APP_REPOSITORY}@{self.ctx['digest']}"

    @property
    def updater_ref(self) -> str:
        return f"{UPDATER_REPOSITORY}@{self.ctx['updater_digest']}"

    def remember(self, **facts: object) -> None:
        journal.remember(self.vol, self.j, **facts)

    def start(self, step: str, sentence: str) -> None:
        journal.start_step(self.vol, self.j, step, self.kit.clock.now())
        self.records.say(sentence, step=step)

    def name(self, role: str) -> str:
        return oneoff.name_for(self.app_name, role, self.id)

    def drill_report_path(self) -> Path:
        return self.vol.work(self.id) / DRILL_REPORT

    def previous(self) -> dict:
        """The previous app container, inspected now, by the id recorded at preflight."""
        return self.client.inspect(str(self.ctx["previous_id"]))

    def by_id(self, cid: str | None) -> dict | None:
        if not cid:
            return None
        return next((c for c in self.client.containers() if c.get("Id") == cid), None)

    def sidecar_now(self, app_inspect: dict) -> survey.Sidecar | None:
        recorded = self.ctx.get("sidecar") or {}
        found = survey.sidecar_of(self.client, app_inspect, service=recorded.get("service"))
        if found is not None:
            self.client.scope = replace(self.client.scope, sidecar=found.name)
        return found

    def _finish(
        self, state: str, sentence: str, failed_step: str | None = None, settle: bool = True
    ) -> str:
        report = volume.read_own_json(self.drill_report_path()) or {}
        log = report.get("log") if isinstance(report.get("log"), list) else []
        self.records.finish(
            state,
            sentence,
            requested_by=self.ctx.get("requested_by"),
            failed_step=failed_step,
            backup=self.ctx.get("backup"),
            started_at=self.ctx.get("started_at"),
            log_tail=tuple(str(x) for x in log[-40:]),
            extra={
                "from_version": self.ctx.get("from_version"),
                "to_version": self.ctx.get("to_version"),
                "notes": list(self.notes),
            },
        )
        if settle:
            journal.settle(self.vol, self.j)
        return state

    def not_started(self, sentence: str) -> str:
        return self._finish("not_started", f"Not started: {sentence}", failed_step=self.j.step)

    # ------------------------------------------------------------------ #
    # Steps 0-2a
    # ------------------------------------------------------------------ #

    def run(self, request: contract.Request) -> str:
        """An accepted apply request, its journal at step 0."""
        self.remember(
            from_version=request.from_version,
            to_version=request.to_version,
            digest=request.digest,
            updater_digest=request.updater_digest,
            prepared_id=request.prepared_id,
            requested_by=request.requested_by,
            started_at=self.kit.clock.now(),
        )
        self.records.say(f"Updating to {request.to_version}.", step="0")
        try:
            self.step1()
            self.step2()
            if self.step2a():
                return "handed_over"
        except survey.NotStarted as e:
            return self.not_started(e.sentence)
        except (eng.EngineError, eng.NotAllowed) as e:
            return self.not_started(f"the container engine refused a check ({e}).")
        fresh = journal.load(self.vol, self.id)
        if fresh is not None and (fresh.step != self.j.step or len(fresh.owners) != len(self.j.owners)):
            # The successor took the request at H5 and failed after it: the
            # handover gave it back, or left it past step 3 (6.6, H6). The
            # journal says which; 5.6 says what to do.
            self.j, self.ctx = fresh, fresh.context
            return self.resume()
        return self.from_step3()

    def step1(self) -> None:
        self.start("1", "Checking everything is ready.")
        client = self.client
        app = survey.app(client)
        running = app.running_app
        if running.local_build:
            raise survey.NotStarted(
                "This instance runs a locally built image. Self-update starts only from a published release."
            )
        if running.version != self.ctx["from_version"]:
            raise survey.NotStarted(
                f"This instance runs {running.version}, not {self.ctx['from_version']}."
            )
        found = {}
        for repo, ref in ((APP_REPOSITORY, self.new_ref), (UPDATER_REPOSITORY, self.updater_ref)):
            image = prepare.present(client, ref)
            if image is None:
                raise survey.NotStarted(
                    "The prepared images are no longer on this machine. Prepare the update again."
                )
            try:
                verified = self.kit.trust.verify(repo, ref.split("@", 1)[1], self.to)
                verify.labels_agree(verified, (image.get("Config") or {}).get("Labels"))
            except verify.Refused as e:
                raise survey.NotStarted(
                    f"The prepared image no longer verifies: {e.rule}: {e.detail}."
                ) from None
            found[repo] = image
        sidecar = None
        if app.layout == "sidecar":
            sidecar = survey.sidecar_of(client, app.inspect)
            if sidecar is None or not sidecar.running:
                raise survey.NotStarted("The Tailscale sidecar is not running.")
            client.scope = replace(client.scope, sidecar=sidecar.name)
        try:
            measured = prepare.measure(self.kit, app, self.id)
        except prepare.PrepareFailed as e:
            raise survey.NotStarted(e.sentence.removeprefix("Preparing failed: ")) from None
        except oneoff.TimedOut:
            raise survey.NotStarted("the free disk and memory could not be measured.") from None
        problem = prepare.floor_problem(
            measured,
            engine=self.kit.site.engine,
            new_image_bytes=0,
            memory_needed=prepare.app_memory(app.inspect) + prepare.MEMORY_SPARE,
        )
        if problem:
            raise survey.NotStarted(problem)
        # The copy is built now and put to the engine client's guard, so a
        # setting the updater will not copy refuses here, not at step 7.
        try:
            body = shapes.copy_app(
                app.inspect,
                self.new_ref,
                image_config=app.image,
                sidecar_id=sidecar.id if sidecar else None,
            )
            eng.guard_create(body, client.scope)
        except (eng.NotAllowed, ValueError) as e:
            raise survey.NotStarted(
                f"The app container has a setting the updater will not copy: {e}"
            ) from None
        new_labels = (found[APP_REPOSITORY].get("Config") or {}).get("Labels") or {}
        old_labels = (app.inspect.get("Config") or {}).get("Labels") or {}
        self.remember(
            app_name=app.name,
            previous_id=app.inspect["Id"],
            layout=app.layout,
            ledger_volume=app.ledger_volume,
            old_ref=app.image_ref,
            old_version=running.version,
            old_revision=old_labels.get(verify.REVISION_LABEL),
            new_revision=new_labels.get(verify.REVISION_LABEL),
            app_protocol=running.protocol,
            sidecar=(
                {
                    "id": sidecar.id,
                    "name": sidecar.name,
                    "service": sidecar.service,
                    "started_at": sidecar.started_at,
                }
                if sidecar
                else None
            ),
            updater_protocols=list(
                protocol_window((found[UPDATER_REPOSITORY].get("Config") or {}).get("Labels")) or ()
            ),
        )

    def step2(self) -> None:
        hook_dir = self.kit.site.hook_dir
        config = volume.read_own_json(hook_dir / "hook.json") if hook_dir else None
        if config is None:
            self.records.say("No pre-update hook is configured; skipped.")
            return
        self.start("2", "Running the pre-update hook.")
        timeout = config.get("timeout_seconds", HOOK_DEFAULT_SECONDS)
        timeout = timeout if isinstance(timeout, int) and 0 < timeout <= 3600 else HOOK_DEFAULT_SECONDS
        assert hook_dir is not None
        volume.write_json(
            hook_dir / f"{self.id}.request",
            {"id": self.id, "from": self.ctx["from_version"], "to": self.to},
        )
        deadline = Deadline(self.kit.clock, timeout)
        while True:
            result = volume.read_own_json(hook_dir / f"{self.id}.result")
            if result is not None:
                if result.get("exit") == 0:
                    self.records.say("The pre-update hook succeeded.")
                    return
                raise survey.NotStarted("the pre-update hook failed.")
            if deadline.expired():
                raise survey.NotStarted("the pre-update hook did not answer in time.")
            self.kit.sleep(self.kit.poll)

    def step2a(self) -> bool:
        """C1. True when the successor took the request over."""
        me = self.kit.site.me
        successor = Owner(image_digest=str(self.ctx["updater_digest"]), version=self.to, container="")
        window = tuple(self.ctx.get("updater_protocols") or ()) or None
        if not goes_first(me, successor, window, int(self.ctx.get("app_protocol") or 1)):  # type: ignore[arg-type]
            self.remember(first_handover="skipped")
            return False
        self.start("2a", f"Handing over to the updater of {self.to} before the app stops.")
        outcome = self.kit.handover.first(request_id=self.id, me=me, successor=successor)
        if outcome.done:
            # The successor wrote itself in as the owner at H5, with
            # `first_handover`, and owns the journal from there: this updater
            # writes nothing more to it, nor to the status.
            return True
        self.remember(first_handover="failed")
        self.notes.append(
            f"The updater of {self.to} did not take over first; this one ran the update. {outcome.sentence}"
        )
        self.records.say("The handover did not happen; this updater carries on.")
        return False

    # ------------------------------------------------------------------ #
    # Steps 3-10
    # ------------------------------------------------------------------ #

    def from_step3(self) -> str:
        try:
            self.step3()
        except _STEP_ERRORS as e:
            sentence = e.sentence if isinstance(e, StepFailed) else f"the app could not be stopped ({e})."
            with contextlib.suppress(_STEP_ERRORS):
                self.previous_home()
            return self.not_started(sentence)
        self.step4()
        verdict = self.step5()
        if verdict is not None:
            return self.rollback(*verdict)
        return self.after_drill()

    def step3(self) -> None:
        self.start("3", "Stopping the app.")
        parked = previous_name(self.app_name)
        stale = survey.find(self.client, parked)
        if stale is not None and stale.get("Id") != self.ctx["previous_id"]:
            if survey.running(stale):
                raise StepFailed(
                    f"a container called {parked} is running, and the updater will not stop it."
                )
            image = survey.image_of(self.client, self.client.inspect(stale["Id"]))
            refs = [r for r in (image or {}).get("RepoDigests") or [] if eng.IMAGE_BY_DIGEST.fullmatch(r)]
            self.remember(stale_previous={"id": stale["Id"], "images": refs})
            # Kept until the next successful update (8.7); this is that update,
            # and its configuration is in an earlier history/<id>.previous.json.
            self.client.remove(stale["Id"], force=True)
        self.client.stop(self.ctx["previous_id"], grace=STOP_GRACE_SECONDS)
        self.client.rename(self.ctx["previous_id"], parked)

    def step4(self) -> None:
        self.start("4", "Starting the maintenance page.")
        try:
            prev = self.previous()
            sidecar = self.sidecar_now(prev) if self.ctx.get("layout") == "sidecar" else None
            body = shapes.placard(
                prev,
                str(self.ctx["old_ref"]),
                self.id,
                ledger_volume=str(self.ctx["ledger_volume"]),
                update_volume=self.kit.site.update_volume,
                sidecar_id=sidecar.id if sidecar else None,
                image_config=(survey.image_of(self.client, prev) or {}).get("Config"),
            )
            self.runner.launch(self.name("placard"), body)
        except _STEP_ERRORS as e:
            # 4.2: logged. People see a refused connection or a 502 instead.
            self.notes.append(f"The maintenance page did not start ({e}).")
            self.records.say("The maintenance page did not start; carrying on without it.")

    def step5(self) -> tuple[str, str] | None:
        """The drill. None when it succeeded; else where the rollback starts, and why."""
        self.start("5", "Backing up and migrating the ledger.")
        self.remember(drill_started=self.kit.clock.now())
        volume.make_shared_dir(self.vol.work(self.id))
        prev = self.previous()
        report = f"{shapes.UPDATE_PATH}/work/{self.id}/{DRILL_REPORT}"
        body = shapes.oneoff(
            prev,
            self.new_ref,
            "drill",
            self.id,
            ["python", "-m", "scripts.upgrade", "--yes", "--report", report],
            ledger_volume=str(self.ctx["ledger_volume"]),
            update_volume=self.kit.site.update_volume,
            image_config=(survey.image_of(self.client, prev) or {}).get("Config"),
        )
        try:
            self.runner.launch(self.name("drill"), body)
        except _STEP_ERRORS as e:
            # Nothing ran: no backup, nothing migrated.
            return "R3", f"the drill could not start ({e})"
        return self.await_drill()

    def await_drill(self) -> tuple[str, str] | None:
        deadline = Deadline.from_dict(
            self.kit.clock, self.ctx.get("drill_deadline") or {"seconds": DRILL_SECONDS}
        )
        name = self.name("drill")
        code: int | None
        try:
            code = self._wait_saving(name, deadline)
        except oneoff.TimedOut:
            code = None
        with contextlib.suppress(_STEP_ERRORS):
            self.runner.discard(name)
        return self.judge_drill(code)

    def _wait_saving(self, name: str, deadline: Deadline) -> int:
        """`Runner.wait`, saving what the deadline has spent so a restart carries on from it."""
        saved = self.kit.clock.now()
        while True:
            state = self.runner.state(name)
            if state is None:
                raise oneoff.TimedOut(f"{name} is gone")
            if not state.get("Running") and state.get("Status") not in ("created", "restarting"):
                code = state.get("ExitCode")
                return code if isinstance(code, int) else -1
            if deadline.expired():
                self.runner.discard(name)
                raise oneoff.TimedOut(f"{name} ran past {int(deadline.seconds)} s")
            if self.kit.clock.now() - saved >= SAVE_DEADLINE_EVERY:
                self.remember(drill_deadline=deadline.to_dict())
                saved = self.kit.clock.now()
            self.kit.sleep(self.kit.poll)

    def judge_drill(self, code: int | None) -> tuple[str, str] | None:
        report = volume.read_own_json(self.drill_report_path())
        backup = (report or {}).get("backup") if isinstance((report or {}).get("backup"), dict) else {}
        folder = backup.get("folder") if backup.get("verified") else None
        if report is None:
            folder = self.find_backup()
        if folder:
            self.remember(backup=folder)
        exit_code = (report or {}).get("exit")
        if report is not None and code == 0 and exit_code == 0 and folder:
            return None
        if not folder:
            return "R3", SENTENCE_BY_EXIT[1]
        if report is None:
            return "R1", INTERRUPTED
        return "R1", SENTENCE_BY_EXIT.get(exit_code, "the drill did not finish")  # type: ignore[arg-type]

    def find_backup(self) -> str | None:
        """5.6, *5, no drill report*: a backup folder made since the drill started, if it verifies."""
        since = float(self.ctx.get("drill_started") or 0)
        body = shapes.oneoff(
            self.previous(),
            self.new_ref,
            "find-backup",
            self.id,
            ["python", "-c", shapes.FIND_BACKUP_SCRIPT, f"{since:.0f}"],
            ledger_volume=str(self.ctx["ledger_volume"]),
            ledger_mode="ro",
        )
        try:
            result = self.runner.run(self.name("find-backup"), body, FIND_BACKUP_SECONDS)
            folder = (
                json.loads(result.output.strip().splitlines()[-1]).get("folder")
                if result.exit_code == 0
                else None
            )
        except (*_STEP_ERRORS, IndexError, AttributeError):
            folder = None
        return folder if isinstance(folder, str) else None

    def after_drill(self) -> str:
        self.step6()
        try:
            self.step7()
        except _STEP_ERRORS as e:
            return self.rollback("R1", DID_NOT_START, detail=str(e))
        problem = self.step8()
        if problem:
            return self.rollback("R1", DID_NOT_ANSWER, detail=problem)
        return self.finish_success()

    def step6(self) -> None:
        self.start("6", "Stopping the maintenance page.")
        try:
            self.runner.discard(self.name("placard"))
        except _STEP_ERRORS as e:
            self.notes.append(f"The maintenance page could not be removed ({e}).")

    def create_new_app(self) -> str:
        prev = self.previous()
        sidecar = self.sidecar_now(prev) if self.ctx.get("layout") == "sidecar" else None
        if self.ctx.get("layout") == "sidecar" and (sidecar is None or not sidecar.running):
            raise StepFailed("the Tailscale sidecar is not running")
        image = survey.image_of(self.client, prev)
        body = shapes.copy_app(
            prev,
            self.new_ref,
            image_config=(image or {}).get("Config") if image else None,
            sidecar_id=sidecar.id if sidecar else None,
        )
        new_id = self.client.create(self.app_name, body)
        self.remember(new_id=new_id)
        self.client.start(new_id)
        return new_id

    def step7(self) -> None:
        self.start("7", f"Starting {self.to}.")
        self.create_new_app()

    def step8(self) -> str | None:
        self.start("8", f"Checking {self.to} answers.")
        expect = health.Expect(self.to, self.ctx.get("new_revision"))
        problem = self.check_health(expect, self.new_ref)
        if problem and self.ctx.get("layout") == "sidecar" and not self.ctx.get("sidecar_recreated"):
            recorded = self.ctx.get("sidecar") or {}
            now = self.sidecar_now(self.previous())
            if now is not None and (
                now.id != recorded.get("id") or now.started_at != recorded.get("started_at")
            ):
                # 8.4: the sidecar restarted mid-update, and the app joined the
                # namespace it had. Once more, into the one it has now.
                self.remember(sidecar_recreated=True)
                self.records.say("The sidecar restarted; starting the new version again inside it.")
                try:
                    self.client.remove(str(self.ctx["new_id"]), force=True)
                    self.create_new_app()
                except _STEP_ERRORS as e:
                    return f"it could not be started again ({e})"
                problem = self.check_health(expect, self.new_ref)
        return problem

    def check_health(self, expect: health.Expect, image: str) -> str | None:
        checker = health.Checker(
            self.client, self.runner, engine=self.kit.site.engine, sleep=self.kit.sleep
        )
        prev = self.previous()
        sidecar = self.sidecar_now(prev) if self.ctx.get("layout") == "sidecar" else None
        return checker.wait(
            Deadline(self.kit.clock, health.HEALTH_SECONDS),
            app_name=self.app_name,
            sidecar_name=sidecar.name if sidecar else None,
            expect=expect,
            previous=prev,
            image=image,
            request_id=self.id,
        )

    def step9(self) -> None:
        if self.j.step != "9":
            self.start("9", "Recording the update.")
        site = self.kit.site
        if site.project_dir is not None:
            me = site.me
            mine = (
                pin.image_ref(UPDATER_REPOSITORY, me.version, me.image_digest)
                if contract.DIGEST.fullmatch(me.image_digest) and contract.VERSION.fullmatch(me.version)
                else None
            )
            try:
                pin.write(
                    site.project_dir, pin.image_ref(APP_REPOSITORY, self.to, str(self.ctx["digest"])), mine
                )
            except OSError as e:
                self.notes.append(
                    f"The pin could not be written ({e.strerror}); a later compose up may start the old version."
                )
        try:
            volume.write_json(self.vol.root / "history" / f"{self.id}.previous.json", self.previous())
        except (*_STEP_ERRORS, OSError) as e:
            self.notes.append(f"The previous container's configuration could not be saved ({e}).")
        self.prune()
        for ref in (self.ctx.get("stale_previous") or {}).get("images") or []:
            if ref not in (self.ctx.get("old_ref"), self.new_ref):
                with contextlib.suppress(_STEP_ERRORS):
                    self.client.remove_image(ref)

    def prune(self) -> None:
        """B9: update backups beyond the newest five go, after a successful update only."""
        names = set()
        for path in (self.vol.root / "history").glob("*.json"):
            stem = path.name[: -len(".json")]
            if not contract.is_uuid4(stem):
                continue
            doc = volume.read_own_json(path) or {}
            if isinstance(doc.get("backup"), str):
                names.add(os.path.basename(doc["backup"]))
        if isinstance(self.ctx.get("backup"), str):
            names.add(os.path.basename(self.ctx["backup"]))
        stamps = sorted((n for n in names if shapes.BACKUP_STAMP.fullmatch(n)), reverse=True)
        older = stamps[KEEP_BACKUPS:]
        if not older:
            return
        body = shapes.oneoff(
            self.previous(),
            self.new_ref,
            "prune",
            self.id,
            ["python", "-c", shapes.PRUNE_SCRIPT, *older],
            ledger_volume=str(self.ctx["ledger_volume"]),
        )
        try:
            result = self.runner.run(self.name("prune"), body, PRUNE_SECONDS)
            if result.exit_code != 0:
                raise StepFailed(f"exit {result.exit_code}")
        except _STEP_ERRORS as e:
            self.notes.append(f"Older update backups could not be pruned ({e}).")

    def step10(self) -> str:
        """The handover after a successful apply, then the record. Returns the state."""
        me = self.kit.site.me

        def record() -> str:
            return self._finish("succeeded", f"Updated to {self.to}.")

        if self.ctx.get("first_handover") == "done":
            return record()
        if stays_newer(me, self.to):
            self.notes.append(f"The updater stays on {me.version}, which is newer.")
            return record()
        if self.j.step != "10":
            self.start("10", "Handing over to the new updater.")
        successor = Owner(image_digest=str(self.ctx["updater_digest"]), version=self.to, container="")
        written: list[str] = []

        def before_go() -> None:
            # The app update is recorded before the successor is told to go:
            # from `go` on it is the successor's volume to write in, and a
            # take-back adds its sentence to this record (6.6).
            self.notes.append(
                f"The updater of {self.to} takes over; this one stays on standby for ten minutes."
            )
            written.append(record())

        outcome = self.kit.handover.after(
            request_id=self.id, me=me, successor=successor, before_go=before_go
        )
        if written:
            return written[0]
        if not outcome.done:
            self.notes.append(f"The updater stayed on {me.version}. {outcome.sentence}")
        return record()

    def finish_success(self) -> str:
        self.step9()
        return self.step10()

    # ------------------------------------------------------------------ #
    # Rollback, R1-R5
    # ------------------------------------------------------------------ #

    def previous_home(self) -> None:
        """The previous container back under its own name, and running (R3; 5.6 rows 3-4)."""
        self.runner.discard(self.name("placard"))
        pid = str(self.ctx["previous_id"])
        prev = self.by_id(pid)
        if prev is None:
            raise StepFailed("the previous container is gone")
        if self.app_name not in survey.names(prev):
            squatter = survey.find(self.client, self.app_name)
            if squatter is not None and squatter.get("Id") != pid:
                self.client.remove(squatter["Id"], force=True)
            self.client.rename(pid, self.app_name)
        if not survey.running(self.by_id(pid)):
            self.client.start(pid)

    def r1(self) -> None:
        self.start("R1", "Removing the new version.")
        found = survey.find(self.client, self.app_name)
        if found is not None and found.get("Id") != self.ctx["previous_id"]:
            self.client.remove(found["Id"], force=True)
        self.runner.discard(self.name("placard"))

    def r2(self) -> None:
        self.start("R2", "Restoring the backup with the previous version.")
        folder = self.ctx.get("backup")
        if not isinstance(folder, str):
            raise StepFailed("there is no verified backup to restore")
        prev = self.previous()
        body = shapes.oneoff(
            prev,
            str(self.ctx["old_ref"]),
            "restore",
            self.id,
            ["python", "-m", "scripts.restore", folder, "--yes"],
            ledger_volume=str(self.ctx["ledger_volume"]),
            image_config=(survey.image_of(self.client, prev) or {}).get("Config"),
        )
        result = self.runner.run(self.name("restore"), body, RESTORE_SECONDS)
        if result.exit_code != 0:
            raise StepFailed(f"the backup could not be restored (exit {result.exit_code})")
        self.remember(restored=True)

    def r3(self) -> None:
        self.start("R3", "Starting the previous version.")
        self.previous_home()

    def r4(self) -> None:
        self.start("R4", "Checking the previous version answers.")
        expect = health.Expect(str(self.ctx["old_version"]), self.ctx.get("old_revision"))
        problem = self.check_health(expect, str(self.ctx["old_ref"]))
        if problem:
            raise StepFailed(f"the previous version did not answer: {problem}")

    def rollback(self, start: str, reason: str, detail: str | None = None) -> str:
        if "rollback_reason" not in self.ctx:
            self.remember(rollback_reason=reason, failed_step=self.j.step, rollback_detail=detail)
        steps = ("R1", "R2", "R3", "R4")
        step = start
        while True:
            try:
                for one in steps[steps.index(step) :]:
                    step = one
                    getattr(self, one.lower())()
                break
            except _STEP_ERRORS as e:
                if self.j.rollback_attempts >= journal.MAX_ROLLBACK_ATTEMPTS:
                    return self.needs_recovery(str(e.sentence if isinstance(e, StepFailed) else e))
                journal.count_rollback_attempt(self.vol, self.j)
                step = self.j.step if self.j.step in steps else step
        self.start("R5", "Recording the rollback.")
        return self.finish_rolled_back()

    def finish_rolled_back(self) -> str:
        reason = self.ctx.get("rollback_reason") or INTERRUPTED
        if self.ctx.get("restored"):
            tail = "; the ledger was restored"
        elif NOT_MIGRATED in reason:
            tail = ""
        else:
            tail = f"; {NOT_MIGRATED}"
        return self._finish(
            "rolled_back",
            f"Rolled back to {self.ctx.get('old_version')}: {reason}{tail}.",
            failed_step=self.ctx.get("failed_step"),
        )

    def needs_recovery(self, why: str) -> str:
        """4.3: the rollback failed three times. Nothing serves; the code stays valid."""
        try:
            current = survey.find(self.client, self.app_name)
            if current is not None and survey.running(current):
                self.client.stop(current["Id"], grace=STOP_GRACE_SECONDS)
            prev = self.previous()
            sidecar = self.sidecar_now(prev) if self.ctx.get("layout") == "sidecar" else None
            body = shapes.placard(
                prev,
                str(self.ctx["old_ref"]),
                self.id,
                ledger_volume=str(self.ctx["ledger_volume"]),
                update_volume=self.kit.site.update_volume,
                sidecar_id=sidecar.id if sidecar else None,
                recovery=True,
            )
            self.runner.launch(self.name("placard"), body)
        except _STEP_ERRORS as e:
            self.notes.append(f"The recovery page did not start ({e}).")
        return self._finish(
            "needs_recovery",
            f"The update failed and putting {self.ctx.get('old_version')} back failed too: {why}. "
            "Open the recovery page with the recovery code.",
            failed_step=self.j.step,
            settle=False,
        )

    # ------------------------------------------------------------------ #
    # Resuming (5.6)
    # ------------------------------------------------------------------ #

    def observe(self) -> Observed:
        """What the resuming updater finds, re-inspecting before it decides (8.6)."""
        drill_running = False
        if self.j.step == "5" and "app_name" in self.ctx:
            state = self.runner.state(self.name("drill"))
            drill_running = bool(state and state.get("Running"))
        report = volume.read_own_json(self.drill_report_path())
        backup = (report or {}).get("backup") if isinstance((report or {}).get("backup"), dict) else {}
        verified = bool(backup.get("verified") and backup.get("folder"))
        if verified:
            self.remember(backup=backup["folder"])
        newer = False
        if self.j.step == "5" and report is None and not drill_running and "app_name" in self.ctx:
            folder = self.find_backup()
            if folder:
                self.remember(backup=folder)
                newer = True
        owner = self.j.owner
        alive = False
        me = self.kit.site.me
        if owner is not None and not owner.is_(me):
            # By image, not by name: a handover renames both updaters (H5).
            alive = runs(self.client, owner.image_digest)
        return Observed(
            drill_running=drill_running,
            drill_report=report is not None,
            report_backup_verified=verified,
            newer_backup_verifies=newer,
            owner_alive=alive,
        )

    def resume(self) -> str:
        """Carry out 5.6's branch for this journal."""
        decision = journal.resume(self.j, self.observe(), self.kit.site.me)
        action = decision.action
        self.records.say("The updater restarted; resuming the update.")
        if "app_name" not in self.ctx and action not in (Action.DEFER,):
            # Preflight never finished: nothing was stopped.
            return self.not_started("the updater restarted before anything was changed.")
        if action == Action.DEFER:
            return "deferred"
        if action == Action.NOT_STARTED:
            with contextlib.suppress(_STEP_ERRORS):
                self.previous_home()
            return self.not_started("the updater restarted before the app was stopped.")
        if action == Action.CONTINUE:
            return self.from_step3()
        if action == Action.RESTORE_PREVIOUS:
            try:
                self.previous_home()
            except _STEP_ERRORS as e:
                return self.rollback("R3", INTERRUPTED, detail=str(e))
            return self.not_started("the updater restarted while the app was being stopped.")
        if action == Action.WAIT_FOR_DRILL:
            verdict = self.await_drill()
            if verdict is None and decision.carry_forward:
                return self.after_drill()
            return self.rollback(*(verdict or ("R1", INTERRUPTED)))
        if action == Action.ROLLBACK:
            return self.rollback(decision.from_step or "R1", self.ctx.get("rollback_reason") or INTERRUPTED)
        if action == Action.RESUME_ROLLBACK:
            if self.j.rollback_attempts >= journal.MAX_ROLLBACK_ATTEMPTS:
                return self.needs_recovery(INTERRUPTED)
            journal.count_rollback_attempt(self.vol, self.j)
            step = decision.from_step or "R1"
            return self.rollback(step, self.ctx.get("rollback_reason") or INTERRUPTED)
        if action == Action.NEEDS_RECOVERY:
            return self.needs_recovery(INTERRUPTED)
        if action == Action.FINISH_RECORD:
            if self.j.step == "R5":
                return self.finish_rolled_back()
            return self.finish_success()
        if action == Action.RESUME_HANDOVER:
            return self.step10()
        return self.not_started("the journal could not be resumed.")  # pragma: no cover
