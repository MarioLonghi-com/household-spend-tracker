"""Browser recovery: the updater's side (design notes Part 11, 9.2; #163).

When an update `needs_recovery`, the maintenance page (`scripts/placard.py`,
from the **old** app image) runs in recovery mode. It has no socket and no
say in whether a code is right: it forwards the code the owner typed inside
one of R9's fixed requests, and **this module decides**.

## The handshake, four files in `recovery/`

| File | Written by | What |
|---|---|---|
| `mode.json` | updater | what the page is for: an update (`recovery`) or 9.2 (`ledger_ahead`) |
| `request.json` | page | one request of `contract.RECOVERY_FIELDS`' shapes, carrying the code |
| `answer.json` | updater | the verdict, echoing `id`, `kind` and `created_at`; never the code |
| `attempts.json` | updater | wrong codes so far and when the refusal ends; the page shows it |

The page writes `created_at` to the microsecond, so the echo tells its own
answer from an older one without a field R9 does not have.

## How a request is taken

Like `intake`: renamed out of the way first, then read from the name it now
holds, never through a symlink, only a regular file owned by the page's uid,
at most 4 KiB. **Then it is unlinked at once**, before anything else: the
request is the one file that carries the code, and the volume keeps nothing
that does (U10).

## How the code is checked

1. The shape (`contract.validate_recovery`), then the refusal window: while
   `attempts.json` says refused, every request is refused unread -- no hash
   is computed, and the refusal is not counted, so a flood cannot lengthen it.
2. The update must be unsettled: its journal still holds `recovery_hash`
   (`journal.settle` deletes it). A code for a settled update opens nothing.
3. **Canonical form first** (R20): upper case, hyphens and spaces removed, O
   read as zero, I and L as one -- exactly `app.services.updates.canonical_code`.
4. scrypt with the hash's own stored parameters, bounded as `contract`
   bounds them (R10), and `secrets.compare_digest`.
5. Five wrong codes: 15 minutes refused; each further five doubles it. A
   right code resets the count.

Every refusal on this path is decided from the volume alone: **no engine
call** (U10).

## What each action does (11.3)

- `open`: nothing but the check. The page then shows the history.
- `retry_rollback`: R1-R4 again (`Apply.rollback`), one attempt per press.
- `restore_backup`: an update backup is restored with the app image that was
  running when the drill took it -- the `old_ref` of the update whose journal
  names that backup -- if that image is still present and still verifies;
  then that version is started and must answer health.
- `start_matching`: a version this update knows the head of (the prepare
  report's stamp and code head) is started, after its own `scripts.upgrade
  --check` confirms the ledger is at exactly that revision. Never a migration.
- `download_backup`, `download_diagnostics`: accepted here; the page streams.
- `leave_for_operator`: the record is marked `left_for_operator`. Only the
  downloads stay open after it: whatever someone does from a terminal next
  is not undone by a button.

Each is recorded, with its result, in the update's `history/<id>.json`
under `recovery`.

## When recovery mode starts

On `needs_recovery` (`Apply.recovery_page`); when an apply's journal has been
unfinished for an hour with the app stopped and nothing answering (11.1);
and, without a code, when compose started the app on an image older than the
pin and it exited (9.2).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from updater import contract, health, journal, oneoff, pin, prepare, shapes, survey, verify, volume
from updater import engine as eng
from updater.apply import _STEP_ERRORS, Apply, StepFailed
from updater.contract import RecoveryRequest, Refusal
from updater.detect import VERSION_LABEL, label_version
from updater.site import APP_REPOSITORY, Kit, Records
from updater.volume import REQUEST_OWNER_UID, UnsafeFile

#: Wrong codes allowed before the first refusal, and each further round.
WRONG_PER_ROUND = 5
#: The first refusal; it doubles after each further round (11.2).
REFUSE_SECONDS = 15 * 60
#: An apply journal unfinished this long, with nothing serving, opens recovery (11.1).
STUCK_SECONDS = 60 * 60
#: How often the 9.2 check looks at the app.
AHEAD_EVERY_SECONDS = 30.0
#: A pause between answering `accepted` and the first engine call, so the
#: page can tell the owner before R1 takes it down.
ACCEPT_GRACE_SECONDS = 3.0
#: The newest update backups offered (11.3), as many as are kept (B9).
OFFERED_BACKUPS = 5
CHECK_SECONDS = 5 * 60
RESTORE_SECONDS = 15 * 60
#: What `oneoff.name_for` makes of the 9.2 page's "request".
AHEAD_ID = "ahead"
TAKEN_PREFIX = ".taken-"
#: The actions that act through the engine, refused once the owner left it to a terminal.
ENGINE_KINDS = ("retry_rollback", "restore_backup", "start_matching")
#: The steps after which the app has been stopped (4.2 step 3 onwards).
_APP_STOPPED_STEPS = ("4", "5", "6", "7", "8", "R1", "R2", "R3", "R4")
_PIN_VERSION = re.compile(r":([0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})@sha256:[0-9a-f]{64}$")


# --------------------------------------------------------------------------- #
# The code
# --------------------------------------------------------------------------- #


def canonical(code: str) -> str:
    """The text that was hashed (R20). Identical to the app's `canonical_code`."""
    text = code.strip().upper().replace("-", "").replace(" ", "")
    return text.replace("O", "0").replace("I", "1").replace("L", "1")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text + "=" * (-len(text) % 4), validate=True)


def code_matches(code: str, hashed: str | None) -> bool:
    """Whether `code` is the one `hashed` was made from. Constant-time on the hash."""
    if not isinstance(hashed, str) or not contract.recovery_hash_ok(hashed):
        return False
    m = contract.RECOVERY_HASH.fullmatch(hashed)
    assert m is not None
    ln, r, p = (int(m.group(i)) for i in (1, 2, 3))
    try:
        salt, want = _unb64(m.group(4)), _unb64(m.group(5))
    except ValueError:
        return False
    got = hashlib.scrypt(
        canonical(code).encode("ascii", "replace"),
        salt=salt,
        n=1 << ln,
        r=r,
        p=p,
        maxmem=contract.SCRYPT_MAX_BYTES + 1024 * 1024,
        dklen=len(want),
    )
    return secrets.compare_digest(got, want)


# --------------------------------------------------------------------------- #
# The attempt limit (11.2)
# --------------------------------------------------------------------------- #


@dataclass
class Attempts:
    wrong: int = 0
    refused_until: float | None = None

    def refused(self, now: float) -> bool:
        return self.refused_until is not None and now < self.refused_until

    def to_dict(self) -> dict:
        return {
            "protocol": contract.FROZEN_PROTOCOL,
            "wrong": self.wrong,
            "refused_until": contract.iso(self.refused_until) if self.refused_until else None,
            "per_round": WRONG_PER_ROUND,
        }


def refusal_seconds(wrong: int) -> float | None:
    """How long the `wrong`-th wrong code refuses for: at 5, 15 min; at 10, 30; at 15, 60."""
    if wrong < WRONG_PER_ROUND or wrong % WRONG_PER_ROUND:
        return None
    return REFUSE_SECONDS * 2 ** (wrong // WRONG_PER_ROUND - 1)


def load_attempts(vol: volume.Volume) -> Attempts:
    try:
        doc = volume.read_own_json(vol.recovery_attempts) or {}
    except (UnsafeFile, ValueError, OSError):
        doc = {}
    wrong = doc.get("wrong")
    until = doc.get("refused_until")
    try:
        until_ts = contract.parse_iso(until) if isinstance(until, str) else None
    except ValueError:
        until_ts = None
    return Attempts(wrong=wrong if isinstance(wrong, int) and wrong >= 0 else 0, refused_until=until_ts)


def save_attempts(vol: volume.Volume, attempts: Attempts) -> None:
    volume.write_json(vol.recovery_attempts, attempts.to_dict())


# --------------------------------------------------------------------------- #
# Which backups are update backups, and which image took each
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Source:
    """The app image a backup's ledger was written by: the drill's `old_ref`."""

    stamp: str
    ref: str
    version: str
    revision: str | None
    update_id: str


def _journals(vol: volume.Volume) -> list[journal.Journal]:
    found = []
    for path in sorted((vol.root / "journal").glob("*.json")):
        rid = path.name[: -len(".json")]
        if not contract.is_uuid4(rid):
            continue
        with contextlib.suppress(UnsafeFile, ValueError, KeyError, OSError):
            j = journal.load(vol, rid)
            if j is not None and j.kind == "apply":
                found.append(j)
    return found


def backup_sources(vol: volume.Volume) -> dict[str, Source]:
    """Every update backup a journal names, newest first, with the image that took it."""
    out: dict[str, Source] = {}
    for j in _journals(vol):
        folder = j.context.get("backup")
        ref = j.context.get("old_ref")
        version = j.context.get("old_version")
        if not (isinstance(folder, str) and isinstance(ref, str) and isinstance(version, str)):
            continue
        stamp = os.path.basename(folder)
        if not contract.BACKUP_STAMP.fullmatch(stamp) or not eng.IMAGE_BY_DIGEST.fullmatch(ref):
            continue
        out[stamp] = Source(stamp, ref, version, j.context.get("old_revision"), j.id)
    return dict(sorted(out.items(), reverse=True)[:OFFERED_BACKUPS])


# --------------------------------------------------------------------------- #
# The recovery loop
# --------------------------------------------------------------------------- #


class Recovery:
    def __init__(self, kit: Kit, owner_uid: int = REQUEST_OWNER_UID) -> None:
        self.kit = kit
        self.owner_uid = owner_uid
        self.busy = False
        self._ahead_checked = float("-inf")

    @property
    def vol(self) -> volume.Volume:
        return self.kit.site.volume

    def now(self) -> float:
        return self.kit.clock.now()

    # ------------------------------------------------------------------ #

    def startup(self) -> None:
        """Forget what a crash left: a half-taken request (it carries a code), an action cut short."""
        for leftover in (self.vol.root / "recovery").glob(f"{TAKEN_PREFIX}*"):
            with contextlib.suppress(FileNotFoundError):
                leftover.unlink()
        for j in _journals(self.vol):
            interrupted = j.context.get("recovery_action")
            if not interrupted or j.recovery_hash is None:
                continue
            a = Apply(self.kit, j)
            with contextlib.suppress(*_STEP_ERRORS, eng.EngineUnavailable):
                a.recovery_page()
            a.remember(recovery_action=None)
            self.record(
                j.id,
                {"kind": interrupted.get("kind"), "result": "failed", "sentence": "Interrupted by a restart of the updater."},
            )

    def tick(self, unfinished: Callable[[], list[journal.Journal]], busy: bool = False) -> str | None:
        """One pass: a request if there is one, then the two watches. Returns the answer's state."""
        answered = None
        if os.path.lexists(self.vol.recovery_request):
            answered = self.handle()
        if not busy:
            try:
                stuck = unfinished()
                self.watch_stuck(stuck)
                if not stuck:
                    self.watch_ahead()
            except (*_STEP_ERRORS, eng.EngineUnavailable):
                pass
        return answered

    # ------------------------------------------------------------------ #
    # Taking and answering
    # ------------------------------------------------------------------ #

    def take(self) -> object:
        """The request's JSON, read once and unlinked at once. Raises `Refusal`."""
        holding = self.vol.root / "recovery" / f"{TAKEN_PREFIX}{secrets.token_hex(8)}"
        try:
            os.rename(self.vol.recovery_request, holding)
        except FileNotFoundError:
            return None
        try:
            data = volume.read_untrusted(holding, self.owner_uid)
        except UnsafeFile as e:
            raise Refusal("unsafe_file", str(e)) from None
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(holding)
        try:

            def no_constants(name: str) -> object:
                raise ValueError(name)

            return json.loads(data.decode("utf-8"), parse_constant=no_constants)
        except (ValueError, UnicodeDecodeError):
            raise Refusal("not_json", "The recovery request is not valid JSON.") from None

    def answer(self, request: RecoveryRequest | dict | None, state: str, sentence: str, code: str | None = None) -> None:
        volume.write_json(
            self.vol.recovery_answer, contract.recovery_answer(request, state, sentence, self.now(), code)
        )

    def handle(self) -> str:
        """Take the page's request, check the code, carry out the action. Returns the answer's state."""
        raw: object = None
        try:
            raw = self.take()
            request = contract.validate_recovery(raw, self.now())
        except Refusal as e:
            self.answer(raw if isinstance(raw, dict) else None, "refused", e.sentence, e.code)
            return "refused"

        try:
            j = self.check(request)
        except Refusal as e:
            self.answer(request, "refused", e.sentence, e.code)
            if e.code not in ("refused_for_now", "unknown"):
                self.record(request.id, {"kind": request.kind, "result": "refused", "sentence": e.sentence})
            return "refused"
        return self.act(request, j)

    def check(self, request: RecoveryRequest) -> journal.Journal:
        """Everything that decides whether the code opens anything. No engine call."""
        attempts = load_attempts(self.vol)
        now = self.now()
        if attempts.refused(now):
            minutes = max(1, int((attempts.refused_until - now + 59) // 60))  # type: ignore[operator]
            raise Refusal(
                "refused_for_now",
                f"Too many wrong codes. Recovery refuses every code for {minutes} more minute"
                f"{'s' if minutes != 1 else ''}.",
            )
        j = None
        with contextlib.suppress(UnsafeFile, ValueError, KeyError, OSError):
            j = journal.load(self.vol, request.id)
        if j is None or j.kind != "apply":
            raise Refusal("unknown", "There is no update with that id.")
        if j.recovery_hash is None:
            raise Refusal("settled", "That update has finished, so its recovery code opens nothing any more.")
        if not code_matches(request.code, j.recovery_hash):
            attempts.wrong += 1
            pause = refusal_seconds(attempts.wrong)
            if pause:
                attempts.refused_until = now + pause
            save_attempts(self.vol, attempts)
            raise Refusal("wrong_code", "That is not the recovery code for this update.")
        if attempts.wrong:
            save_attempts(self.vol, Attempts())
        state = self.state_of(j)
        if state is None:
            raise Refusal("not_open", "That update does not need recovery.")
        if state == "left_for_operator" and request.kind in ENGINE_KINDS:
            raise Refusal(
                "left",
                "This update was left to be finished from a terminal; the page will not act on it now.",
            )
        return j

    def state_of(self, j: journal.Journal) -> str | None:
        """`needs_recovery`, `left_for_operator`, `stuck` (11.1's hour), or None: not open."""
        record = None
        with contextlib.suppress(UnsafeFile, ValueError, OSError):
            record = volume.read_own_json(self.vol.history(j.id))
        if record is not None:
            state = record.get("state")
            return state if state in ("needs_recovery", "left_for_operator") else None
        return "stuck" if j.context.get("recovery_mode") else None

    # ------------------------------------------------------------------ #
    # The update's history
    # ------------------------------------------------------------------ #

    def record(self, update_id: str, entry: dict, replace_last: bool = False) -> None:
        """Add one action to `history/<id>.json`'s `recovery` list (files only gain keys, C4)."""
        path = self.vol.history(update_id)
        try:
            doc = volume.read_own_json(path)
        except (UnsafeFile, ValueError, OSError):
            return
        if doc is None:
            # 11.1's stuck journal has no record yet; the actions wait in the journal.
            j = journal.load(self.vol, update_id)
            if j is None:
                return
            done = list(j.context.get("recovery_log") or [])
            if replace_last and done:
                done.pop()
            done.append({**entry, "at": contract.iso(self.now())})
            journal.remember(self.vol, j, recovery_log=done)
            return
        done = list(doc.get("recovery") or [])
        if replace_last and done:
            done.pop()
        done.append({**entry, "at": contract.iso(self.now())})
        doc["recovery"] = done
        volume.write_json(path, doc)

    def _carry_log(self, update_id: str, before: list) -> None:
        """Apply rewrote the record; put the recovery actions back on it."""
        doc = volume.read_own_json(self.vol.history(update_id))
        if doc is None:
            return
        j = journal.load(self.vol, update_id)
        earlier = list((j.context.get("recovery_log") if j else None) or [])
        doc["recovery"] = [*earlier, *before, *(doc.get("recovery") or [])]
        volume.write_json(self.vol.history(update_id), doc)
        if earlier and j is not None:
            journal.remember(self.vol, j, recovery_log=[])

    def _log_so_far(self, update_id: str) -> list:
        doc = volume.read_own_json(self.vol.history(update_id)) or {}
        return list(doc.get("recovery") or [])

    # ------------------------------------------------------------------ #
    # The actions
    # ------------------------------------------------------------------ #

    def act(self, request: RecoveryRequest, j: journal.Journal) -> str:
        kind = request.kind
        if kind == "open":
            self.answer(request, "done", "The recovery code is right.")
            self.record(j.id, {"kind": "open", "result": "done", "sentence": "Recovery was opened."})
            return "done"
        if kind == "download_backup":
            if request.backup not in backup_sources(self.vol):
                return self._refuse_after_code(request, "That is not one of the newest update backups.")
            with_key = " with secret.key" if request.include_key else " without secret.key"
            sentence = f"The backup {request.backup} was downloaded{with_key}."
            self.answer(request, "done", sentence)
            self.record(j.id, {"kind": kind, "result": "done", "sentence": sentence})
            return "done"
        if kind == "download_diagnostics":
            sentence = "The diagnostics were downloaded."
            self.answer(request, "done", sentence)
            self.record(j.id, {"kind": kind, "result": "done", "sentence": sentence})
            return "done"
        if kind == "leave_for_operator":
            return self.leave(request, j)

        # The engine actions.
        if kind == "restore_backup" and request.backup not in backup_sources(self.vol):
            return self._refuse_after_code(request, "That is not one of the newest update backups.")
        self.answer(request, "accepted", "Accepted. The updater is working on it; this page stops meanwhile.")
        self.record(j.id, {"kind": kind, "result": "running", "sentence": "Started."})
        a = Apply(self.kit, j)
        a.remember(recovery_action={"kind": kind, "at": contract.iso(self.now())})
        self.kit.sleep(ACCEPT_GRACE_SECONDS)
        self.busy = True
        try:
            if kind == "retry_rollback":
                state, sentence = self.retry(a)
            elif kind == "restore_backup":
                state, sentence = self.restore(a, str(request.backup))
            else:
                state, sentence = self.start_matching(a, str(request.revision))
        except eng.EngineUnavailable:
            state, sentence = "failed", "The container engine stopped answering."
            with contextlib.suppress(*_STEP_ERRORS, eng.EngineUnavailable):
                a.recovery_page()
        finally:
            self.busy = False
            a.remember(recovery_action=None)
        result = "done" if state in ("rolled_back", "recovered") else "failed"
        self.answer(request, result, sentence)
        self.record(j.id, {"kind": kind, "result": result, "sentence": sentence}, replace_last=True)
        return result

    def _refuse_after_code(self, request: RecoveryRequest, sentence: str) -> str:
        self.answer(request, "refused", sentence, "backup")
        self.record(request.id, {"kind": request.kind, "result": "refused", "sentence": sentence})
        return "refused"

    def leave(self, request: RecoveryRequest, j: journal.Journal) -> str:
        path = self.vol.history(j.id)
        doc = volume.read_own_json(path)
        sentence = "Left to be finished from a terminal. Nothing serves the ledger until then."
        if doc is None:
            # 11.1's stuck journal: its record is written now, and the journal stops being resumed.
            a = Apply(self.kit, j)
            a._finish("left_for_operator", sentence, failed_step=j.step, settle=False)
            self._carry_log(j.id, [])
        else:
            doc["state"] = "left_for_operator"
            doc["sentence"] = sentence
            volume.write_json(path, doc)
        Records(self.vol, j.id, "apply", self.kit.clock).say(sentence, state="left_for_operator")
        self.answer(request, "done", sentence)
        self.record(j.id, {"kind": "leave_for_operator", "result": "done", "sentence": sentence})
        return "done"

    def _fail_back(self, a: Apply, why: str) -> tuple[str, str]:
        """An action that did not bring the app back: recovery stays open, the page comes back."""
        before = self._log_so_far(a.id)
        a.needs_recovery(why)
        self._carry_log(a.id, before)
        return "needs_recovery", f"It did not work: {why}. Recovery is still open."

    def retry(self, a: Apply) -> tuple[str, str]:
        """R1-R4 again: one attempt."""
        before = self._log_so_far(a.id)
        reason = a.ctx.get("rollback_reason") or "the update was interrupted"
        state = a.rollback("R1", str(reason))
        self._carry_log(a.id, before)
        record = volume.read_own_json(self.vol.history(a.id)) or {}
        if state == "rolled_back":
            return state, str(record.get("sentence") or "Rolled back.")
        return state, "Retrying the rollback did not work. Recovery is still open."

    def _clear(self, a: Apply) -> None:
        """The failed new app and the page out of the way (R1's work, without counting an attempt)."""
        found = survey.find(a.client, a.app_name)
        if found is not None and found.get("Id") != a.ctx["previous_id"]:
            a.client.remove(found["Id"], force=True)
        a.runner.discard(a.name("placard"))

    def _usable(self, a: Apply, ref: str, version: str) -> dict:
        """The image, still present and still verifying (11.3's confinement). Raises `StepFailed`."""
        image = prepare.present(a.client, ref)
        if image is None:
            raise StepFailed(f"{version} is no longer on this machine")
        try:
            verified = self.kit.trust.verify(APP_REPOSITORY, ref.split("@", 1)[1], version)
            verify.labels_agree(verified, (image.get("Config") or {}).get("Labels"))
        except verify.Refused as e:
            raise StepFailed(f"{version} no longer verifies ({e.rule})") from None
        return image

    def _start(self, a: Apply, ref: str, version: str, revision: str | None) -> None:
        """`version` serving under the app's name, and answering. Raises `StepFailed`."""
        if ref == a.ctx.get("old_ref"):
            a.previous_home()
        else:
            prev = a.previous()
            parked = a.app_name + shapes.PREVIOUS_SUFFIX
            if a.app_name in survey.names(survey.find(a.client, a.ctx["previous_id"]) or {}):
                if survey.running(a.by_id(a.ctx["previous_id"])):
                    a.client.stop(a.ctx["previous_id"], grace=30)
                a.client.rename(a.ctx["previous_id"], parked)
            sidecar = a.sidecar_now(prev) if a.ctx.get("layout") == "sidecar" else None
            body = shapes.copy_app(
                prev,
                ref,
                image_config=(survey.image_of(a.client, prev) or {}).get("Config"),
                sidecar_id=sidecar.id if sidecar else None,
            )
            new_id = a.client.create(a.app_name, body)
            a.remember(recovered_id=new_id)
            a.client.start(new_id)
        problem = a.check_health(health.Expect(version, revision), ref)
        if problem:
            raise StepFailed(f"{version} did not answer: {problem}")

    def _recovered(self, a: Apply, ref: str, version: str, sentence: str) -> tuple[str, str]:
        site = self.kit.site
        if site.project_dir is not None:
            try:
                pin.write(site.project_dir, pin.image_ref(APP_REPOSITORY, version, ref.split("@", 1)[1]), None)
            except OSError as e:
                a.notes.append(f"The pin could not be written ({e.strerror}); a later compose up may start another version.")
        before = self._log_so_far(a.id)
        a._finish("recovered", sentence, failed_step=a.ctx.get("failed_step"))
        self._carry_log(a.id, before)
        return "recovered", sentence

    def restore(self, a: Apply, stamp: str) -> tuple[str, str]:
        source = backup_sources(self.vol).get(stamp)
        if source is None:  # pragma: no cover - checked before accepting
            return self._fail_back(a, "that backup is not an update backup")
        try:
            image = self._usable(a, source.ref, source.version)
            self._clear(a)
            prev = a.previous()
            body = shapes.oneoff(
                prev,
                source.ref,
                "restore",
                a.id,
                ["python", "-m", "scripts.restore", f"{shapes.LEDGER_PATH}/backups/{stamp}", "--yes"],
                ledger_volume=str(a.ctx["ledger_volume"]),
                image_config=image.get("Config"),
            )
            result = a.runner.run(a.name("restore"), body, RESTORE_SECONDS, stderr=True)
            a.remember(restore_log=result.output.splitlines()[-40:])
            if result.exit_code != 0:
                raise StepFailed(f"the backup {stamp} could not be restored (exit {result.exit_code})")
            revision = source.revision or ((image.get("Config") or {}).get("Labels") or {}).get(verify.REVISION_LABEL)
            self._start(a, source.ref, source.version, revision)
        except (*_STEP_ERRORS, oneoff.TimedOut) as e:
            return self._fail_back(a, str(e.sentence if isinstance(e, StepFailed) else e))
        return self._recovered(
            a, source.ref, source.version, f"Recovered: the backup {stamp} was restored and {source.version} started."
        )

    def known_heads(self, a: Apply) -> dict[str, tuple[str, str, str | None]]:
        """Revision -> (image, version, revision label), for the two versions this update knows."""
        report = None
        with contextlib.suppress(UnsafeFile, ValueError, OSError):
            if contract.is_uuid4(a.ctx.get("prepared_id")):
                report = volume.read_own_json(self.vol.prepared(str(a.ctx["prepared_id"])))
        drill = volume.read_own_json(a.drill_report_path()) or {}
        stamps = drill.get("stamp") if isinstance(drill.get("stamp"), dict) else {}
        old_head = (report or {}).get("database_stamp") or stamps.get("before")
        new_head = (report or {}).get("code_head") or stamps.get("after")
        heads: dict[str, tuple[str, str, str | None]] = {}
        if isinstance(old_head, str) and isinstance(a.ctx.get("old_ref"), str):
            heads[old_head] = (str(a.ctx["old_ref"]), str(a.ctx["old_version"]), a.ctx.get("old_revision"))
        if isinstance(new_head, str) and a.ctx.get("digest"):
            heads[new_head] = (a.new_ref, a.to, a.ctx.get("new_revision"))
        return heads

    def start_matching(self, a: Apply, revision: str) -> tuple[str, str]:
        known = self.known_heads(a).get(revision)
        if known is None:
            return self._fail_back(a, f"no version on this machine is known to be at {revision}")
        ref, version, label = known
        try:
            image = self._usable(a, ref, version)
            self._clear(a)
            body = shapes.oneoff(
                a.previous(),
                ref,
                "check",
                a.id,
                ["python", "-m", "scripts.upgrade", "--check", "--json"],
                ledger_volume=str(a.ctx["ledger_volume"]),
                image_config=image.get("Config"),
            )
            result = a.runner.run(a.name("check"), body, CHECK_SECONDS)
            try:
                facts = json.loads(result.output) if result.exit_code == 0 else {}
            except ValueError:
                facts = {}
            at, head = facts.get("database_stamped"), facts.get("code_head")
            if at != revision or head != revision:
                raise StepFailed(f"the ledger is at {at or 'an unknown revision'}, which {version} does not run as it is")
            self._start(a, ref, version, label)
        except (*_STEP_ERRORS, oneoff.TimedOut) as e:
            return self._fail_back(a, str(e.sentence if isinstance(e, StepFailed) else e))
        return self._recovered(a, ref, version, f"Recovered: {version} matches the ledger and was started.")

    # ------------------------------------------------------------------ #
    # When recovery mode starts on its own
    # ------------------------------------------------------------------ #

    def watch_stuck(self, unfinished: list[journal.Journal]) -> list[str]:
        """11.1: an apply unfinished for an hour, the app stopped, nothing answering."""
        opened = []
        now = self.now()
        for j in unfinished:
            if j.context.get("recovery_mode") or j.recovery_hash is None or j.step not in _APP_STOPPED_STEPS:
                continue
            if "app_name" not in j.context:
                continue
            started = [e.get("at") for e in j.started if isinstance(e, dict) and isinstance(e.get("at"), str)]
            try:
                last = contract.parse_iso(started[-1]) if started else None
            except ValueError:
                last = None
            if last is None or now - last < STUCK_SECONDS:
                continue
            a = Apply(self.kit, j)
            if survey.running(survey.find(a.client, a.app_name)):
                continue
            a.recovery_page(stop_app=False)
            a.remember(recovery_mode=contract.iso(now))
            opened.append(j.id)
        return opened

    def watch_ahead(self) -> str | None:
        """9.2: an app compose started on an image older than the pin, and which exited.

        Not acted on beyond showing the page (decision 3): the refusing
        container is stopped so its restart policy stops fighting the page
        for the port, and the page says to run the launcher again. When an
        app of the pinned version is there again, the page goes.
        """
        now = self.kit.clock.now()
        if now - self._ahead_checked < AHEAD_EVERY_SECONDS:
            return None
        self._ahead_checked = now
        site = self.kit.site
        if site.project_dir is None:
            return None
        m = _PIN_VERSION.search(pin.read(site.project_dir).get(pin.APP_KEY, ""))
        if not m:
            return None
        pinned = m.group(1)
        client = self.kit.client
        listed = survey.app_listing(client.containers())
        if listed is None:
            return None
        app_name = survey.names(listed)[0]
        page = oneoff.name_for(app_name, "placard", AHEAD_ID)
        runner = self.kit.runner
        seen = client.inspect(app_name)
        image = survey.image_of(client, seen) or {}
        version = label_version(((image.get("Config") or {}).get("Labels") or {}).get(VERSION_LABEL))
        older = version is not None and contract.parse_version(version) < contract.parse_version(pinned)
        state = seen.get("State") if isinstance(seen.get("State"), dict) else {}
        if not older:
            if runner.find(page) is not None:
                runner.discard(page)
                if not survey.running(listed) and version == pinned:
                    client.start(seen["Id"])
                return "cleared"
            return None
        exited = (not state.get("Running") or state.get("Restarting")) and state.get("ExitCode") not in (0, None)
        if not exited or runner.find(page) is not None:
            return None
        ref = survey.published_ref(image, (seen.get("Config") or {}).get("Image"))
        ledger = shapes.volume_at(seen, shapes.LEDGER_PATH)
        if ref is None or ledger is None:
            return None
        if state.get("Running") or state.get("Restarting"):
            client.stop(seen["Id"], grace=10)
        sidecar = None
        if shapes.network_of(seen)[0].startswith(("container:", "service:")):
            sidecar = survey.sidecar_of(client, seen)
        volume.write_json(self.vol.recovery_mode, contract.recovery_mode("ledger_ahead", None, now))
        body = shapes.placard(
            seen,
            ref,
            AHEAD_ID,
            ledger_volume=ledger,
            update_volume=site.update_volume,
            sidecar_id=sidecar.id if sidecar else None,
            recovery=True,
            image_config=image.get("Config"),
        )
        runner.launch(page, body)
        return "opened"

