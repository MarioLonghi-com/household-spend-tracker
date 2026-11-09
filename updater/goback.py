"""Going back: one step, to the release the last successful update came from (#173).

The design notes' sketch (Part 17): a `go_back` request, behind the owner's
step-up and the version typed; only to the version immediately before, and
only while its image and the update's backup still exist; the backup
restored with the previous image *exactly as rollback R1-R4 does*. Since
revision 4 (C9) the previous container is **recreated** from the
configuration step 9 saved, `history/<update>.previous.json`, rather than
depended on: a later `compose up` may have removed the parked one.

| # | Step | If it fails |
|---|---|---|
| 0 | The request was validated and the journal begun, holding the recovery code's hash (`intake`). | refused |
| G1 | **Preflight**: the app runs the version the update went to; the previous image is present and still verifies as the release it says it is; the saved configuration is there and is a shape the engine client accepts; the update's backup is on the disk and verifies; the sidecar runs; disk and memory. | not started |
| G2 | The previous container recreated from the saved configuration, stopped, as `<name>-previous`, parked with restart policy `no` (a stopped leftover of that name is removed first). | not started |
| G3 | The app stopped (30 s), and the maintenance page started where it was, from the previous image. | the app is started again; not started |
| R1 | The app removed. The maintenance page stays. | 4.3's rule |
| R2 | The update's backup restored **with the previous image**. The ledger it replaces is kept beside it as `.before-restore-<stamp>`. | 4.3's rule |
| R3 | `-previous` renamed to the app's name, its own restart policy back, started. | 4.3's rule |
| R4 | Health against the previous version and commit. | 4.3's rule |
| G4 | The pin names the previous release; the record. | logged |

4.3's rule is the rollback's: retried from the step that failed, three
attempts in all, then `needs_recovery` -- nothing serves the ledger, the
recovery page opens with this request's code (Part 11).

**Everything written since the update is lost**, as from the terminal (Case
C, `deploy/UPGRADING.md`): the confirmation counts it. Passkeys registered
since then are gone from the ledger but not from the authenticators.

**What it never does**: go further back than the release the newest settled
update came from; go back from an update a later go-back already undid; run
an image that does not verify now as that release; run without the update's
backup on the disk, verified; hand over to another updater.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import replace

from updater import contract, journal, oneoff, pin, prepare, shapes, survey, verify, volume
from updater import engine as eng
from updater.apply import (
    _STEP_ERRORS,
    PARKED_POLICY,
    Apply,
    StepFailed,
    previous_name,
    restart_policy,
)
from updater.site import APP_REPOSITORY, UPDATER_REPOSITORY, Kit, Records
from updater.volume import UnsafeFile, Volume

KIND = "go_back"
VERIFY_SECONDS = 5 * 60
GOING_BACK = "going back was asked for"
INTERRUPTED = "going back was interrupted"


def _record(vol: Volume, rid: str) -> dict | None:
    with contextlib.suppress(UnsafeFile, ValueError, OSError):
        return volume.read_own_json(vol.history(rid))
    return None


def previous_config(vol: Volume, update_id: str) -> dict | None:
    """Step 9's `history/<id>.previous.json` (C9): the previous container as it was inspected."""
    with contextlib.suppress(UnsafeFile, ValueError, OSError):
        doc = volume.read_own_json(vol.root / "history" / f"{update_id}.previous.json")
        if isinstance(doc, dict) and isinstance(doc.get("Config"), dict) and isinstance(doc.get("HostConfig"), dict):
            return doc
    return None


def last_update(vol: Volume) -> contract.LastUpdate | None:
    """The newest settled apply or go-back, and whether going back from it is possible (files only).

    Only a successful apply can be gone back from, and only while nothing
    later has settled: one step back, never two. The rest -- the image, the
    backup on the disk -- is the engine's to say, at G1.
    """
    newest: tuple[str, str, dict] | None = None
    for path in (vol.root / "history").glob("*.json"):
        rid = path.name[: -len(".json")]
        if not contract.is_uuid4(rid):
            continue
        doc = _record(vol, rid)
        if not doc or doc.get("kind") not in contract.JOURNAL_KINDS:
            continue
        if doc.get("state") not in ("succeeded", "rolled_back", "recovered", "left_for_operator", "needs_recovery"):
            continue
        at = doc.get("finished_at")
        if isinstance(at, str) and (newest is None or at > newest[0]):
            newest = (at, rid, doc)
    if newest is None:
        return None
    _, rid, doc = newest
    frm, to = doc.get("from_version"), doc.get("to_version")
    found = contract.LastUpdate(
        id=rid,
        from_version=frm if isinstance(frm, str) else None,
        to_version=to if isinstance(to, str) else None,
    )

    def no(problem: str) -> contract.LastUpdate:
        return contract.LastUpdate(found.id, found.from_version, found.to_version, problem)

    if doc.get("kind") != "apply" or doc.get("state") != "succeeded":
        return no("The last update did not finish as a successful update, so there is nothing to go back from.")
    ctx = source_context(vol, rid)
    if ctx is None:
        return no("The last update's journal does not say what it replaced, so the updater cannot go back.")
    if previous_config(vol, rid) is None:
        return no("The last update did not keep the previous version's configuration, so the updater cannot go back.")
    stamp = os.path.basename(str(ctx["backup"]))
    newest_five = sorted(prepare.update_backup_names(vol), reverse=True)[: prepare.KEEP_BACKUPS]
    if stamp not in newest_five:
        return no("The last update's backup is no longer one of the newest five, so the updater cannot go back.")
    return found


def source_context(vol: Volume, update_id: str) -> dict | None:
    """What the apply `update_id` recorded that going back needs, or None when anything is missing."""
    j = None
    with contextlib.suppress(UnsafeFile, ValueError, KeyError, OSError):
        j = journal.load(vol, update_id)
    if j is None or j.kind != "apply":
        return None
    ctx = j.context
    ref = ctx.get("old_ref")
    m = eng.IMAGE_BY_DIGEST.fullmatch(ref) if isinstance(ref, str) else None
    backup = ctx.get("backup")
    if (
        m is None
        or m.group(1) != APP_REPOSITORY
        or not isinstance(ctx.get("old_version"), str)
        or not contract.VERSION.fullmatch(ctx["old_version"])
        or not isinstance(backup, str)
        or backup != f"{shapes.LEDGER_PATH}/backups/{os.path.basename(backup)}"
        or not contract.BACKUP_STAMP.fullmatch(os.path.basename(backup))
        or not isinstance(ctx.get("app_name"), str)
    ):
        return None
    return dict(ctx)


def for_journal(kit: Kit, j: journal.Journal) -> Apply:
    """The orchestration a journal belongs to: an apply's, or a go-back's."""
    return GoBack(kit, j) if j.kind == KIND else Apply(kit, j)


class GoBack(Apply):
    """One `go_back` request, from its journal. `run()` from step 0, or `resume()`.

    It is an apply's rollback, entered on purpose long after the apply
    succeeded, so it reuses the rollback's steps. The journal's context uses
    the apply's names: `old_*` is the release gone back to, `digest` and
    `new_revision` the one left, `previous_id` the recreated container.
    """

    def __init__(self, kit: Kit, j: journal.Journal) -> None:
        super().__init__(kit, j)
        self.records = Records(self.vol, j.id, KIND, kit.clock)

    @property
    def to(self) -> str:
        """The release being left (the apply's `to`): what the rollback steps call the new version."""
        return str(self.ctx.get("left_version") or self.ctx.get("from_version"))

    @property
    def back_to(self) -> str:
        return str(self.ctx["to_version"])

    def record_extra(self) -> dict:
        return {**super().record_extra(), "update_id": self.ctx.get("update_id")}

    # ------------------------------------------------------------------ #

    def run(self, request: contract.Request) -> str:  # type: ignore[override]
        self.remember(
            from_version=request.from_version,
            to_version=request.to_version,
            update_id=request.update_id,
            requested_by=request.requested_by,
            started_at=self.kit.clock.now(),
        )
        self.records.say(f"Going back to {request.to_version}.", step="0")
        try:
            self.g1()
        except survey.NotStarted as e:
            return self.not_started(e.sentence, e.code)
        except (eng.EngineError, eng.NotAllowed) as e:
            return self.not_started(f"the container engine refused a check ({e}).")
        return self.from_g2()

    def from_g2(self) -> str:
        try:
            self.g2()
            self.g3()
        except _STEP_ERRORS as e:
            sentence = e.sentence if isinstance(e, StepFailed) else f"the app could not be stopped ({e})."
            with contextlib.suppress(_STEP_ERRORS):
                self.current_home()
            return self.not_started(sentence)
        return self.rollback("R1", GOING_BACK)

    def g1(self) -> None:
        self.start("G1", "Checking everything is ready.")
        client = self.client
        update_id = str(self.ctx["update_id"])
        last = last_update(self.vol)
        if last is None or last.id != update_id or last.problem:
            raise survey.NotStarted(
                (last.problem if last and last.id == update_id else None)
                or "That is no longer the last update, so it cannot be gone back from."
            )
        source = source_context(self.vol, update_id)
        saved = previous_config(self.vol, update_id)
        assert source is not None and saved is not None  # last_update checked both
        app = survey.app(client)
        running = app.running_app
        if running.local_build:
            raise survey.NotStarted(
                "This instance runs a locally built image. Self-update starts only from a published release."
            )
        if running.version != self.ctx["from_version"]:
            raise survey.NotStarted(f"This instance runs {running.version}, not {self.ctx['from_version']}.")
        old_ref = str(source["old_ref"])
        image = prepare.present(client, old_ref)
        if image is None:
            raise survey.NotStarted(
                f"The image of {self.back_to} is no longer on this machine, so the updater cannot go back to it."
            )
        try:
            verified = self.kit.trust.verify(APP_REPOSITORY, old_ref.split("@", 1)[1], self.back_to)
            verify.labels_agree(verified, (image.get("Config") or {}).get("Labels"))
        except verify.Refused as e:
            raise survey.NotStarted(f"The image of {self.back_to} no longer verifies: {e.rule}: {e.detail}.") from None
        sidecar = None
        if app.layout == "sidecar":
            sidecar = survey.sidecar_of(client, app.inspect)
            if sidecar is None or not sidecar.running:
                raise survey.NotStarted("The Tailscale sidecar is not running.")
            client.scope = replace(client.scope, sidecar=sidecar.name)
        try:
            body = shapes.copy_app(
                saved, old_ref, image_config=image.get("Config"), sidecar_id=sidecar.id if sidecar else None
            )
            eng.guard_create(body, client.scope)
        except (eng.NotAllowed, ValueError) as e:
            raise survey.NotStarted(
                f"The saved configuration of {self.back_to} has a setting the updater will not copy: {e}"
            ) from None
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
            memory_needed=prepare.app_memory(saved) + prepare.MEMORY_SPARE,
            update_backups=prepare.update_backup_names(self.vol),
        )
        if problem:
            raise survey.NotStarted(problem.sentence, problem.code)
        folder = str(source["backup"])
        if not self.backup_verifies(app, folder):
            raise survey.NotStarted(
                f"The backup the update took, {os.path.basename(folder)}, is not on the disk or does not "
                "verify, so going back would have nothing to restore."
            )
        labels = (app.inspect.get("Config") or {}).get("Labels") or {}
        own = source.get("previous_restart_policy")
        self.remember(
            app_name=app.name,
            current_id=app.inspect["Id"],
            layout=app.layout,
            ledger_volume=app.ledger_volume,
            left_version=running.version,
            digest=(app.image_ref or "").split("@", 1)[-1] or None,
            new_revision=labels.get(verify.REVISION_LABEL),
            old_ref=old_ref,
            old_version=self.back_to,
            old_revision=((image.get("Config") or {}).get("Labels") or {}).get(verify.REVISION_LABEL),
            backup=folder,
            previous_restart_policy=dict(own) if isinstance(own, dict) and own.get("Name") else restart_policy(app.inspect),
            sidecar=(
                {"id": sidecar.id, "name": sidecar.name, "service": sidecar.service, "started_at": sidecar.started_at}
                if sidecar
                else None
            ),
        )

    def backup_verifies(self, app: survey.App, folder: str) -> bool:
        """The update's backup on the disk, verified by the running image's `scripts.backup`."""
        body = shapes.oneoff(
            app.inspect,
            str(app.image_ref),
            "find-backup",
            self.id,
            ["python", "-c", shapes.VERIFY_BACKUP_SCRIPT, folder],
            ledger_volume=app.ledger_volume,
            ledger_mode="ro",
            image_config=app.image,
        )
        try:
            result = self.runner.run(oneoff.name_for(app.name, "find-backup", self.id), body, VERIFY_SECONDS)
            found = json.loads(result.output.strip().splitlines()[-1]).get("folder") if result.exit_code == 0 else None
        except (*_STEP_ERRORS, IndexError, AttributeError):
            found = None
        return found == folder

    def g2(self) -> None:
        """C9: the previous container from its saved configuration, stopped and parked."""
        self.start("G2", f"Making the container of {self.back_to} again from its saved configuration.")
        parked = previous_name(self.app_name)
        stale = survey.find(self.client, parked)
        if stale is not None and stale.get("Id") != self.ctx.get("previous_id"):
            if survey.running(stale):
                raise StepFailed(f"a container called {parked} is running, and the updater will not stop it.")
            self.client.remove(stale["Id"], force=True)
        if self.ctx.get("previous_id") and self.by_id(str(self.ctx["previous_id"])) is not None:
            return
        saved = previous_config(self.vol, str(self.ctx["update_id"]))
        if saved is None:
            raise StepFailed("the previous version's saved configuration is gone")
        old_ref = str(self.ctx["old_ref"])
        image = prepare.present(self.client, old_ref)
        sidecar = self.sidecar_now(self.client.inspect(str(self.ctx["current_id"]))) if self.ctx.get("layout") == "sidecar" else None
        body = shapes.copy_app(
            saved,
            old_ref,
            image_config=(image or {}).get("Config"),
            sidecar_id=sidecar.id if sidecar else None,
        )
        body["HostConfig"]["RestartPolicy"] = dict(PARKED_POLICY)
        new_id = self.client.create(parked, body)
        self.remember(previous_id=new_id, restart_policy_parked=True)

    def g3(self) -> None:
        self.start("G3", f"Stopping {self.to}.")
        self.stop_app(str(self.ctx["current_id"]))
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
            self.notes.append(f"The maintenance page did not start ({e}).")

    def current_home(self) -> None:
        """Not started after all: the version that was running, running again under its name."""
        if "app_name" not in self.ctx:
            return  # preflight never finished: nothing was touched
        self.runner.discard(self.name("placard"))
        cid = self.ctx.get("current_id")
        current = self.by_id(str(cid)) if cid else None
        if current is not None and not survey.running(current):
            self.client.start(str(cid))

    def r1(self) -> None:
        """The app removed, the maintenance page left where it is until R3."""
        self.start("R1", f"Removing {self.to}.")
        found = survey.find(self.client, self.app_name)
        if found is not None and found.get("Id") != self.ctx["previous_id"]:
            self.client.remove(found["Id"], force=True)

    # ------------------------------------------------------------------ #

    def finish_rolled_back(self) -> str:
        """G4 rather than R5: the pin names the release gone back to, and the record says so."""
        if self.j.step != "G4":
            self.start("G4", f"Recording going back to {self.back_to}.")
        site = self.kit.site
        if site.project_dir is not None:
            me = site.me
            mine = (
                pin.image_ref(UPDATER_REPOSITORY, me.version, me.image_digest)
                if contract.DIGEST.fullmatch(me.image_digest) and contract.VERSION.fullmatch(me.version)
                else None
            )
            ref = str(self.ctx["old_ref"])
            try:
                pin.write(site.project_dir, pin.image_ref(APP_REPOSITORY, self.back_to, ref.split("@", 1)[1]), mine)
            except OSError as e:
                self.notes.append(
                    f"The pin could not be written ({e.strerror}); a later compose up may start {self.to} again."
                )
        return self._finish("succeeded", f"Went back to {self.back_to}.")

    def needs_recovery(self, why: str) -> str:
        try:
            self.recovery_page()
        except _STEP_ERRORS as e:
            self.notes.append(f"The recovery page did not start ({e}).")
        return self._finish(
            "needs_recovery",
            f"Going back to {self.back_to} failed: {why}. Open the recovery page with the recovery code.",
            failed_step=self.j.step,
            settle=False,
        )

    def resume(self) -> str:
        """5.6 for a go-back: before R1 nothing is lost, so not started; from R1 on, carry on."""
        step = self.j.step
        self.records.say("The updater restarted; resuming going back.")
        if step in ("0", "G1", "G2", "G3") or "current_id" not in self.ctx:
            with contextlib.suppress(_STEP_ERRORS):
                self.current_home()
            return self.not_started("the updater restarted before the app was removed.")
        if step in ("R1", "R2", "R3", "R4"):
            if self.j.rollback_attempts >= journal.MAX_ROLLBACK_ATTEMPTS:
                return self.needs_recovery(INTERRUPTED)
            journal.count_rollback_attempt(self.vol, self.j)
            return self.rollback(step, GOING_BACK)
        return self.finish_rolled_back()

