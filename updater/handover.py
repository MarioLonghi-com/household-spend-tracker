"""The updater hands over to its successor (design notes 6.6, C1-C5).

An updater cannot fix its own code, so it never replaces itself in place: it
starts a verified successor beside itself, lets it prove it can work, steps
aside, and stays on standby for ten minutes in case it has to take back over.

## When

- **step 2a, updater first (C1):** after preflight and before the app is
  stopped, when the prepared updater is newer than this one and its protocol
  window includes the running app's protocol (`goes_first`). If the handover
  completes, the successor owns the apply's journal and carries on from step
  3; if it does not, nothing has been stopped, this updater carries on from
  step 3 itself, and the handover is tried again at step 10;
- **step 10:** after a successful apply, unless 2a already handed over, this
  updater is already newer than the target release's (never downgrade,
  `stays_newer`), or it already runs the target's updater image (`is_me`);
- **`update_updater` (C2):** the updater of any newer release whose protocol
  window includes the app's, after its own short prepare
  (`prepare.updater_only`). From `socket: outdated` (C3) it uses only the
  calls H1-H5 need, which the engine client still allows.

## The handover, H1-H7

U1 is the running updater, U2 its successor. U1's journal is
`handover/<request id>.request`; U2 answers in `.ready`, U1 says `.go`.

| # | U1 | U2 |
|---|---|---|
| H1 | U2's image is on the engine at the verified digest, and verifies (the updater subject). | |
| H2 | Create U2 as a copy of its own container (`shapes.successor`): image changed, named `<name>-next`, `--successor <id>`. Start it. | |
| H3 | Stop taking requests; the heartbeat says `standby`. | Prove it can work: the socket, `_ping`, the project listing, a write read back in `/update`, and its own digest -- from inspecting itself -- equal to the request's. Write `ready`. |
| H4 | `ready` within 60 s, its digest the verified one by U2's word and the engine's: release the lock, write `go` carrying one `ping`. | Wait for `go`. |
| H5 | | Rename U1 to `<name>-previous` (removing an older `-previous`), itself to `<name>`, take the lock, pin its image, take the apply's journal over (2a), answer U1's ping, write the heartbeat as `current`. |
| H6 | Standby for 10 minutes: U2's heartbeat fresh and U2 running, else take back over -- or stand down, if another container now holds the name. | |
| H7 | After 10 healthy minutes, stop itself. It stays, stopped, as `-previous` until the next handover's H5 removes it. | |

**Who is current is U1's journal's to say** (`current_side`): U1, until `go`
was written *and* U2's first heartbeat as `current` exists; then U2. A
recorded outcome (`failed`, `taken_back`, `done`, `stood_down`) settles it for good. Each
side re-reads this on every start, so after an engine restart mid-handover --
or either updater dying after any of its writes -- exactly one of them
carries on, holding the lock, and requests are answered (U9). Every action
of a settling side is idempotent: renames by container id, removals of what
is already gone, a lock rewritten to the same holder.

**Taking back (H6).** U1 writes the outcome first, then stops U2, renames
both back (U2 to `-next`, itself to the canonical name), takes the lock,
pins its own image again, and resumes as `current`. An apply U2 took at 2a is
then resumed from its journal by U1 (`Apply.resume`): still at 2a, U1 owns it
again and carries on from step 3; at step 3 or later, older code never carries
a newer updater's apply forward (`carry_forward=False`), so it follows 5.6's
branch for the step reached -- the previous container back, or the rollback.

**Standing down (#259).** A `compose up` during the standby can replace U2:
compose removes it and starts a new container under the canonical name.
Started from U2's image, that is U2 still and the standby goes on (#169).
Started from another image -- `.env` pinned back to an older updater, say
-- it is the owner's choice of updater, not a failed successor: U1 records
`stood_down`, leaves the name and the lock to it, and stops itself, as at
H7. A take-back that finds its name held all the same, or that the engine
refuses, ends the same way rather than in a crash. And a container started
from U1's image under the canonical name is not U1 while U1's own container
still exists: it does not settle U1's handovers, and starts as `current`.

**The `-previous` lifecycle.** An updater started under the `-previous` name
with no handover of its own to finish watches the canonical one's heartbeat.
Stale for more than two minutes, it stops the broken one, renames it
`-next`, takes the canonical name and the lock, and takes over. A healthy
canonical updater is left alone, and the `-previous` one stops itself again.

**The lock** is `updater.lock` in the volume: the updater that takes
requests, by digest. U1 releases it at H4; U2 takes it at H5; a take-back or
a `-previous` takeover takes it back. Only an updater whose mode is `current`
answers requests.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from updater import contract, journal, pin, shapes, survey, verify, volume
from updater import engine as eng
from updater.clock import Deadline
from updater.detect import PROTOCOL_LABEL
from updater.journal import Owner

if TYPE_CHECKING:
    from updater.site import Kit

#: The label on an updater image naming the protocols it accepts (C4): `1-N`.
#: Built from the app's label, which is built from the ghcr owner (R28).
PROTOCOLS_LABEL = PROTOCOL_LABEL + "s"

UPDATER_REPOSITORY = eng.REPOSITORIES[1]

#: H4: how long U1 waits for `ready`.
READY_SECONDS = 60
#: After `go`: how long U1 waits for U2's heartbeat as `current` and its pong.
TAKEOVER_SECONDS = 60
#: H6: the standby window.
STANDBY_SECONDS = 10 * 60
#: A heartbeat older than this is stale (H6, and the `-previous` lifecycle).
STALE_SECONDS = 2 * 60
#: H6: how long U2's container may be gone, its heartbeat still fresh, before
#: U1 takes back over -- the time compose takes to replace it. podman-compose's
#: `up` removes and recreates every container, the successor included (#169,
#: E12 on rootless Podman); U1 took back in that gap, and two updaters ran.
GONE_GRACE_SECONDS = 20
#: A `-previous` updater started by hand beside a healthy canonical one stops
#: itself again after this long.
PREVIOUS_PATIENCE_SECONDS = 5 * 60
#: How often the standby saves what its window has spent.
SAVE_EVERY_SECONDS = 30.0
STOP_GRACE_SECONDS = 10
#: How long stopping itself waits for the engine's answer before the loop goes
#: on: an answer within it is a refusal, or an engine that has already stopped
#: it; none means the stop is under way and waits for this process to exit.
STOP_ANSWER_SECONDS = 2.0

NEXT_SUFFIX = "-next"
PREVIOUS_SUFFIX = shapes.PREVIOUS_SUFFIX

#: Where the handover runs from: step 2a, step 10, or an `update_updater` request.
KINDS = ("first", "after", "update_updater")
FAILED, TAKEN_BACK, DONE = "failed", "taken_back", "done"
#: H6: U2 was replaced by another updater under its name, and U1 left it be (#259).
STOOD_DOWN = "stood_down"


@dataclass(frozen=True)
class Outcome:
    done: bool
    sentence: str
    #: The updater that owns the request afterwards; None when unchanged.
    owner: Owner | None = None


class Mode(StrEnum):
    #: Takes requests, holds the lock, writes the heartbeat as `current`.
    CURRENT = "current"
    #: H3: waiting for the successor's `ready`; the heartbeat says `standby`.
    HANDING = "handing_over"
    #: After `go`: watching the successor (H6). Silent.
    STANDBY = "standby"
    #: Started with `--successor`, before H5. Silent.
    SUCCESSOR = "successor"
    #: Started under the `-previous` name: watching the canonical one. Silent.
    PREVIOUS = "previous"
    #: Stepped aside for good (H7), or a successor that was not taken. Silent.
    RETIRED = "retired"


class Handover(Protocol):
    """What the service and the orchestration ask of a handover."""

    mode: str

    @property
    def heartbeat_role(self) -> str | None: ...

    def first(self, *, request_id: str, me: Owner, successor: Owner) -> Outcome:
        """Step 2a: hand over before the app stops. `done` means the successor owns the request."""
        ...

    def after(
        self,
        *,
        request_id: str,
        me: Owner,
        successor: Owner,
        before_go: Callable[[], object] | None = None,
        handover_id: str | None = None,
    ) -> Outcome:
        """Step 10: hand over after the app update settled. `before_go` records the apply;
        `handover_id` is this attempt's own, apart from 2a's under the request's id."""
        ...

    def update_updater(self, *, request_id: str, successor: Owner) -> Outcome:
        """An `update_updater` request, its short prepare done."""
        ...

    def startup(self) -> None: ...

    def recover(self) -> None: ...

    def tick(self) -> None: ...

    def flush(self) -> None: ...


class NotAvailable:
    """No handover: every attempt reports that, and nothing is touched.

    What a kit built without one has -- the unit tests of the orchestration,
    and an updater that is not in a container it can find.
    """

    SENTENCE = "Handing over to a newer updater is not available in this updater."
    mode = Mode.CURRENT
    heartbeat_role = "current"

    def first(self, *, request_id: str, me: Owner, successor: Owner) -> Outcome:
        return Outcome(False, self.SENTENCE)

    def after(self, *, request_id: str, me: Owner, successor: Owner, before_go=None, handover_id=None) -> Outcome:
        return Outcome(False, self.SENTENCE)

    def update_updater(self, *, request_id: str, successor: Owner) -> Outcome:
        return Outcome(False, self.SENTENCE)

    def startup(self) -> None:
        pass

    def recover(self) -> None:
        pass

    def tick(self) -> None:
        pass

    def flush(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Small decisions, pure
# --------------------------------------------------------------------------- #


def protocol_window(labels: object) -> tuple[int, int] | None:
    """An updater image's `…updater-protocols` window, or None when it carries none."""
    value = labels.get(PROTOCOLS_LABEL) if isinstance(labels, dict) else None
    m = re.fullmatch(r"([0-9]{1,4})-([0-9]{1,4})", value) if isinstance(value, str) else None
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    return (lo, hi) if lo <= hi else None


def goes_first(me: Owner, successor: Owner, window: tuple[int, int] | None, app_protocol: int) -> bool:
    """C1: the successor is newer than this updater and speaks the running app's protocol."""
    if window is None or is_me(me, successor):
        return False
    return successor.newer_than(me) and window[0] <= app_protocol <= window[1]


def is_me(me: Owner, successor: Owner) -> bool:
    """The successor is this updater: the same image, by digest (#258).

    The digest is an updater's identity, not its version: an image rebuilt at
    the same version is a different updater and is still handed over to, and
    one already on the target's image never hands over to a copy of itself --
    which would evict the `-previous` a take-back needs.
    """
    return bool(me.image_digest) and me.image_digest == successor.image_digest


def stays_newer(me: Owner, target_version: str) -> bool:
    """Never downgrade an updater (6.6): this one is already newer than the target release's."""
    try:
        return contract.parse_version(me.version) > contract.parse_version(target_version)
    except ValueError:
        return False


def canonical(name: str) -> str:
    """The updater's own name, without a handover's `-next` or `-previous`."""
    for suffix in (NEXT_SUFFIX, PREVIOUS_SUFFIX):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def successor_is_current(doc: Mapping, beat: Mapping | None) -> bool:
    """Whether the heartbeat is the successor's, as `current` (H5's last write)."""
    succ = Owner.from_dict(doc.get("successor"))
    return (
        succ is not None
        and isinstance(beat, Mapping)
        and beat.get("role") == "current"
        and beat.get("image_digest") == succ.image_digest
    )


def current_side(doc: Mapping, go_written: bool, beat: Mapping | None) -> str:
    """`predecessor` or `successor`: who is current, by U1's journal (6.6).

    A recorded outcome settles it. Otherwise U1, until `go` was written and
    U2's first heartbeat as `current` exists (`journal.handover_owner`).
    """
    outcome = doc.get("outcome")
    if outcome in (FAILED, TAKEN_BACK):
        return "predecessor"
    if outcome in (DONE, STOOD_DOWN):
        return "successor"
    pred = Owner.from_dict(doc.get("predecessor"))
    succ = Owner.from_dict(doc.get("successor"))
    if pred is None or succ is None:
        return "predecessor"
    owner = journal.handover_owner(pred, succ, go_written, successor_is_current(doc, beat))
    return "successor" if owner.is_(succ) else "predecessor"


def request_of(doc: Mapping) -> str:
    """The request a handover serves: its own id, except a step-10 attempt's."""
    value = doc.get("request")
    return value if isinstance(value, str) and contract.is_uuid4(value) else str(doc.get("id"))


def own_bind_sources(inspect: Mapping, destinations: tuple[str, ...]) -> tuple[str, ...]:
    """The host paths the updater itself binds at `destinations`: the socket, `/project`
    and, where a server configured one, the pre-update hook's `/hook` (6.5).

    The only host paths its successor may bind (the create guard's
    `Scope.bind_sources`), read from the engine, never configured.
    """
    found: list[str] = []
    for m in inspect.get("Mounts") or []:
        if isinstance(m, dict) and m.get("Type") == "bind" and m.get("Destination") in destinations:
            source = m.get("Source")
            if isinstance(source, str) and source not in found:
                found.append(source)
    for bind in (inspect.get("HostConfig") or {}).get("Binds") or []:
        if not isinstance(bind, str):
            continue
        source, _, rest = bind.partition(":")
        if source.startswith("/") and rest.split(":", 1)[0] in destinations and source not in found:
            found.append(source)
    return tuple(found)


# --------------------------------------------------------------------------- #
# Files: the lock and the journal
# --------------------------------------------------------------------------- #


def lock_path(vol: volume.Volume) -> os.PathLike:
    return vol.root / "updater.lock"


def lock_holder(vol: volume.Volume) -> Owner | None:
    doc = volume.read_own_json(lock_path(vol))  # type: ignore[arg-type]
    return Owner.from_dict((doc or {}).get("holder"))


def write_lock(
    vol: volume.Volume, holder: Owner | None, now: float, released_to: Owner | None = None
) -> None:
    doc: dict = {
        "protocol": contract.FROZEN_PROTOCOL,
        "holder": holder.to_dict() if holder else None,
        "at": contract.iso(now),
    }
    if released_to is not None:
        doc["released_to"] = released_to.to_dict()
    volume.write_json(lock_path(vol), doc)  # type: ignore[arg-type]


def load(vol: volume.Volume, request_id: str) -> dict | None:
    """U1's journal of one handover, or None."""
    return journal.read_handover(vol, request_id, "request")


def _image_id(value: object) -> str | None:
    m = eng.IMAGE_ID.fullmatch(str(value or ""))
    return m.group(1) if m else None


def _runs_image(client: eng.EngineClient, container: Mapping, digest: str) -> bool:
    """Whether `container` was started from the updater image `digest`."""
    if not contract.DIGEST.fullmatch(digest or ""):
        return False
    try:
        image = client.inspect_image(f"{UPDATER_REPOSITORY}@{digest}")
    except (eng.EngineError, eng.NotAllowed):
        return False
    wanted = _image_id(image.get("Id"))
    return wanted is not None and _image_id(container.get("ImageID")) == wanted


def runs(client: eng.EngineClient, digest: str) -> bool:
    """Whether a project container runs the updater image `digest` now.

    By image, not by name: a handover renames containers (H5), so the name
    the journal recorded may be another updater's by the time anyone looks.
    """
    if not contract.DIGEST.fullmatch(digest or ""):
        return False
    try:
        image = client.inspect_image(f"{UPDATER_REPOSITORY}@{digest}")
    except (eng.EngineError, eng.NotAllowed):
        return False
    wanted = _image_id(image.get("Id"))
    return any(survey.running(c) and _image_id(c.get("ImageID")) == wanted for c in client.containers())


def present(client: eng.EngineClient, ref: str) -> dict | None:
    try:
        return client.inspect_image(ref)
    except eng.EngineError as e:
        if e.status == 404:
            return None
        raise


# --------------------------------------------------------------------------- #
# The handover itself
# --------------------------------------------------------------------------- #


class Successions:
    """One updater's side of every handover: as U1, as U2, or as a `-previous`.

    `mode` says which it is now; the service answers requests only while it
    is `current`. `after_write(step)` is called after each journal write --
    H1, H2, H3, H4, H6, H7 by U1, H3 (`ready`) and H5 by U2 -- and is where a
    test kills either updater (U9).
    """

    def __init__(
        self,
        kit: Kit,
        *,
        successor_of: str | None = None,
        own_id: str | None = None,
        after_write: Callable[[str], None] | None = None,
    ) -> None:
        self.kit = kit
        self.me: Owner = kit.site.me
        self.successor_of = successor_of
        self.after_write = after_write or (lambda step: None)
        self.mode: str = Mode.SUCCESSOR if successor_of else Mode.CURRENT
        #: This updater's container, by id: its name changes under it (H5),
        #: and a name looked up a moment late may already be the other one's.
        self.own_id: str | None = own_id
        #: The handover this updater is standby for (H6).
        self.watching: str | None = None
        self.started_at = kit.clock.now()
        #: `(request id, sentence)` to add to a record that may not exist yet.
        self.notes: list[tuple[str, str]] = []
        #: Writes the heartbeat at once (H5); the entry point wires it to the beat.
        self.on_beat: Callable[[], object] = lambda: None
        self._standby: Deadline | None = None
        self._saved_at = 0.0
        #: H6: when U2's container was first seen not running.
        self._gone_since: float | None = None

    # ------------------------------------------------------------------ #

    @property
    def vol(self) -> volume.Volume:
        return self.kit.site.volume

    @property
    def client(self) -> eng.EngineClient:
        return self.kit.client

    def now(self) -> float:
        return self.kit.clock.now()

    @property
    def heartbeat_role(self) -> str | None:
        if self.mode == Mode.CURRENT:
            return "current"
        if self.mode == Mode.HANDING:
            return "standby"
        return None

    def _own(self) -> dict | None:
        """This updater's container as the engine lists it now: by id once known."""
        for c in self.client.containers():
            if c.get("Id") == self.own_id or (
                self.own_id is None and self.me.container and self.me.container in survey.names(c)
            ):
                self.own_id = c["Id"]
                return c
        return None

    def _name(self) -> str:
        own = self._own()
        return survey.names(own)[0] if own else self.me.container

    def _by_id(self, cid: object) -> dict | None:
        if not isinstance(cid, str) or not cid:
            return None
        return next((c for c in self.client.containers() if c.get("Id") == cid), None)

    def _save(self, doc: dict) -> None:
        body = {k: v for k, v in doc.items() if k not in ("protocol", "id", "at")}
        journal.write_handover(self.vol, doc["id"], "request", body, self.now())

    def _beat(self) -> dict | None:
        return volume.read_own_json(self.vol.heartbeat)

    def _pin(self, who: Owner) -> None:
        if self.kit.site.project_dir is None:
            return
        if not (contract.DIGEST.fullmatch(who.image_digest) and contract.VERSION.fullmatch(who.version)):
            return
        with contextlib.suppress(OSError):
            pin.write_updater(
                self.kit.site.project_dir, pin.image_ref(UPDATER_REPOSITORY, who.version, who.image_digest)
            )

    def _rename(self, cid: str, name: str) -> None:
        c = self._by_id(cid)
        if c is not None and name not in survey.names(c):
            self.client.rename(cid, name)

    def _clear(self, name: str, keep: tuple[str | None, ...] = ()) -> None:
        """Free `name`: what holds it is stopped and removed -- or, where removing
        is not allowed (`outdated`), renamed aside."""
        found = survey.find(self.client, name)
        if found is None or found.get("Id") in keep:
            return
        if survey.running(found):
            self.client.stop(found["Id"], grace=STOP_GRACE_SECONDS)
        try:
            self.client.remove(found["Id"], force=True)
        except eng.NotAllowed:
            self.client.rename(found["Id"], f"{name}-{found['Id'][:8]}")

    def claim(self) -> None:
        holder = lock_holder(self.vol)
        if holder is None or not holder.is_(self.me) or holder.container != self.me.container:
            write_lock(self.vol, self.me, self.now())

    # ------------------------------------------------------------------ #
    # Start: which side of which handover this updater is on
    # ------------------------------------------------------------------ #

    def startup(self) -> None:
        with contextlib.suppress(eng.EngineError, eng.NotAllowed):
            self._own()
        if self.mode == Mode.SUCCESSOR:
            self.successor_tick()
            return
        self.recover()
        if self.mode != Mode.CURRENT:
            return
        name = self._name()
        if name and name.endswith(PREVIOUS_SUFFIX):
            self.mode = Mode.PREVIOUS
            self.started_at = self.now()
            return
        if name:
            self.me = replace(self.me, container=name)
        self.claim()

    def _mine(self) -> list[dict]:
        """Unsettled handovers this updater started, oldest first."""
        found = []
        for path in sorted((self.vol.root / "handover").glob("*.request")):
            rid = path.name[: -len(".request")]
            if not contract.is_uuid4(rid):
                continue
            doc = load(self.vol, rid)
            pred = Owner.from_dict((doc or {}).get("predecessor"))
            if doc is None or doc.get("settled") or pred is None or not pred.is_(self.me):
                continue
            if self._another_predecessor(doc):
                continue
            found.append(doc)
        found.sort(key=lambda d: str(d.get("at")))
        return found

    def _another_predecessor(self, doc: Mapping) -> bool:
        """The handover's U1 is a container that still exists, and not this one (#259).

        Compose recreating the canonical updater from U1's image makes a new
        container that `pred.is_(me)` cannot tell from U1. Settling U1's
        handover in its place, it went on standby -- silent and busy -- and
        took back from a successor that had done nothing wrong. An engine
        restart keeps container ids, so U1 restarted is still U1.
        """
        pid = doc.get("predecessor_id")
        if not self.own_id or not isinstance(pid, str) or pid == self.own_id:
            return False
        try:
            return self._by_id(pid) is not None
        except (eng.EngineError, eng.NotAllowed):
            return False

    def recover(self) -> None:
        """U1 after a restart: settle every handover it started, by its own journal."""
        if self.mode not in (Mode.CURRENT,):
            return
        for doc in self._mine():
            rid = doc["id"]
            go = journal.read_handover(self.vol, rid, "go")
            side = current_side(doc, go is not None, self._beat())
            if side == "predecessor":
                if doc.get("outcome") is None:
                    if go is None:
                        self._fail(doc, "the updater restarted during the handover", live=False)
                    else:
                        self._take_back(doc, "did not take over before the updater restarted")
                else:
                    self._settle_back(doc, live=False)
            elif doc.get("outcome") == DONE:
                doc["settled"] = True
                self._save(doc)
            else:
                self._record_success(doc)
                self.mode = Mode.STANDBY
                self.watching = rid

    def tick(self) -> None:
        if self.mode == Mode.STANDBY:
            self.watch()
        elif self.mode == Mode.SUCCESSOR:
            self.successor_tick()
        elif self.mode == Mode.PREVIOUS:
            self.previous_tick()

    def flush(self) -> None:
        """Add the handover's sentences to records that exist by now."""
        left = []
        for rid, sentence in self.notes:
            path = self.vol.history(rid)
            record = volume.read_own_json(path)
            if record is None:
                left.append((rid, sentence))
                continue
            notes = [n for n in record.get("notes") or [] if isinstance(n, str)]
            if sentence not in notes:
                record["notes"] = [*notes, sentence]
                volume.write_json(path, record)
        self.notes = left

    # ------------------------------------------------------------------ #
    # U1: H1-H4, then the standby
    # ------------------------------------------------------------------ #

    def first(self, *, request_id: str, me: Owner, successor: Owner) -> Outcome:
        return self.hand_over(request_id, "first", successor)

    def after(
        self,
        *,
        request_id: str,
        me: Owner,
        successor: Owner,
        before_go: Callable[[], object] | None = None,
        handover_id: str | None = None,
    ) -> Outcome:
        return self.hand_over(
            handover_id or request_id, "after", successor, before_go=before_go, request_id=request_id
        )

    def update_updater(self, *, request_id: str, successor: Owner) -> Outcome:
        return self.hand_over(request_id, "update_updater", successor)

    def hand_over(
        self,
        handover_id: str,
        kind: str,
        successor: Owner,
        *,
        before_go: Callable[[], object] | None = None,
        request_id: str | None = None,
    ) -> Outcome:
        """H1-H4 as U1, under `handover/<handover_id>`, for the request `request_id`
        (the same id, but at step 10, which is a second attempt after 2a's)."""
        if kind not in KINDS:
            raise ValueError(kind)
        existing = load(self.vol, handover_id)
        if existing is not None:
            # 5.6 row 10: the handover's own journal answers.
            go = journal.read_handover(self.vol, handover_id, "go")
            if current_side(existing, go is not None, self._beat()) == "successor":
                return Outcome(True, str(existing.get("sentence") or ""), owner=successor)
            return Outcome(False, str(existing.get("sentence") or "the handover was interrupted"))
        own = self._own()
        if own is None or not contract.DIGEST.fullmatch(self.me.image_digest):
            return Outcome(
                False,
                f"The updater stayed on {self.me.version}: it cannot find its own container, "
                "so it cannot start a successor.",
            )
        name = survey.names(own)[0]
        canon = canonical(name)
        self.me = replace(self.me, container=name)
        doc: dict = {
            "id": handover_id,
            "request": request_id or handover_id,
            "kind": kind,
            "step": "H1",
            "predecessor": replace(self.me, container=canon).to_dict(),
            "predecessor_id": own["Id"],
            "successor": replace(successor, container=canon + NEXT_SUFFIX).to_dict(),
            "canonical": canon,
            "outcome": None,
            "settled": False,
        }
        self._save(doc)
        self.after_write("H1")
        try:
            image, problem = self._h1(successor)
            if problem:
                return self._fail(doc, problem)
            self._h2(doc, own, successor, image)
            doc["step"] = "H3"
            self._save(doc)
            self.mode = Mode.HANDING
            self.after_write("H3")
            ready = self._await(READY_SECONDS, lambda: journal.read_handover(self.vol, handover_id, "ready"))
            if ready is None:
                return self._fail(
                    doc, f"the updater of {successor.version} did not say it was ready within a minute"
                )
            problem = self._judge_ready(doc, ready, successor)
            if problem:
                return self._fail(doc, problem)
        except (eng.EngineError, eng.NotAllowed, ValueError, OSError) as e:
            return self._fail(doc, f"the container engine refused ({e})")
        return self._h4(doc, successor, before_go)

    def _h1(self, successor: Owner) -> tuple[dict | None, str | None]:
        ref = f"{UPDATER_REPOSITORY}@{successor.image_digest}"
        image = present(self.client, ref)
        if image is None:
            return None, f"the updater image of {successor.version} is not on this machine"
        try:
            verified = self.kit.trust.verify(UPDATER_REPOSITORY, successor.image_digest, successor.version)
            verify.labels_agree(verified, (image.get("Config") or {}).get("Labels"))
        except verify.Refused as e:
            return None, f"the updater image of {successor.version} does not verify: {e.rule}: {e.detail}"
        return image, None

    def _h2(self, doc: dict, own: dict, successor: Owner, image: dict | None) -> None:
        doc["step"] = "H2"
        self._save(doc)
        self.after_write("H2")
        next_name = doc["canonical"] + NEXT_SUFFIX
        self._clear(next_name)
        seen = self.client.inspect(own["Id"])
        old_image = survey.image_of(self.client, seen)
        body = shapes.successor(
            seen,
            f"{UPDATER_REPOSITORY}@{successor.image_digest}",
            doc["id"],
            image_config=(old_image or {}).get("Config") if old_image else None,
            new_image_config=(image or {}).get("Config"),
        )
        sid = self.client.create(next_name, body)
        doc["successor_id"] = sid
        self._save(doc)
        self.client.start(sid)

    def _judge_ready(self, doc: dict, ready: dict, successor: Owner) -> str | None:
        version = successor.version
        if ready.get("ok") is not True:
            return f"the updater of {version} failed its own check: {ready.get('problem') or 'no reason given'}"
        if ready.get("image_digest") != successor.image_digest:
            return (
                f"the updater of {version} reported a different image of itself "
                f"({str(ready.get('image_digest'))[:19]}), not the verified one"
            )
        # Its word, and the engine's: the container runs the verified digest.
        seen = self.client.inspect(str(doc["successor_id"]))
        image = survey.image_of(self.client, seen) or {}
        if f"{UPDATER_REPOSITORY}@{successor.image_digest}" not in (image.get("RepoDigests") or []):
            return f"the updater of {version} does not run the verified image"
        return None

    def _await(self, seconds: float, probe: Callable[[], object]) -> object:
        deadline = Deadline(self.kit.clock, seconds)
        while True:
            found = probe()
            if found:
                return found
            if deadline.expired():
                return None
            self.kit.sleep(self.kit.poll)

    def _h4(self, doc: dict, successor: Owner, before_go: Callable[[], object] | None) -> Outcome:
        if before_go is not None:
            before_go()
        now = self.now()
        ping = {
            "protocol": contract.FROZEN_PROTOCOL,
            "id": str(uuid.uuid4()),
            "kind": "ping",
            "created_at": contract.iso(now),
        }
        doc.update(step="H4", ping=ping["id"])
        self._save(doc)
        write_lock(self.vol, None, now, released_to=successor)
        journal.write_handover(self.vol, doc["id"], "go", {"ping": ping}, now)
        self.mode = Mode.STANDBY
        self.watching = doc["id"]
        self.after_write("H4")
        if not self._await(TAKEOVER_SECONDS, lambda: self._took_over(doc)):
            sentence = self._take_back(doc, "did not take over within a minute")
            return Outcome(False, sentence)
        self._start_standby(doc)
        return Outcome(
            True,
            f"The updater of {successor.version} took over.",
            owner=replace(successor, container=doc["canonical"]),
        )

    def _took_over(self, doc: dict) -> bool:
        pong = volume.read_own_json(self.vol.history(str(doc.get("ping"))))
        return successor_is_current(doc, self._beat()) and (pong or {}).get("state") == "succeeded"

    def _start_standby(self, doc: dict) -> None:
        doc.update(step="H6", standby={"seconds": STANDBY_SECONDS, "spent": 0.0})
        self._save(doc)
        self._standby = Deadline(self.kit.clock, STANDBY_SECONDS)
        self._saved_at = self.now()
        self.after_write("H6")

    def watch(self) -> None:
        """H6: one look at the successor. Take back over, or stop after ten healthy minutes."""
        doc = load(self.vol, str(self.watching))
        if doc is None or doc.get("outcome") is not None:
            self.mode, self.watching = Mode.CURRENT, None
            return
        if self._standby is None:
            self._standby = Deadline.from_dict(
                self.kit.clock, doc.get("standby") or {"seconds": STANDBY_SECONDS}
            )
            self._saved_at = self.now()
            if doc.get("step") != "H6":
                self._start_standby(doc)
        if self.kit.clock.settling():
            # Just back from a gap (8.6): the heartbeat's age is the gap's.
            return
        replacement = self._replacement(doc)
        if replacement is not None:
            self._stand_down(doc, f"was replaced by another container under its name ({replacement[:12]})")
            return
        problem = self._successor_problem(doc)
        if problem:
            self._take_back(doc, problem)
            return
        if self._standby.expired():
            self._h7(doc)
            return
        if self.now() - self._saved_at >= SAVE_EVERY_SECONDS:
            doc["standby"] = self._standby.to_dict()
            self._save(doc)
            self._saved_at = self.now()

    def _replacement(self, doc: dict) -> str | None:
        """H6: the id of a running container that holds the canonical name and is
        not U2 -- neither the container H2 made nor one of U2's image (#259)."""
        holder = survey.find(self.client, str(doc.get("canonical") or ""))
        if holder is None or not survey.running(holder):
            return None
        hid = str(holder.get("Id") or "")
        if hid in (doc.get("successor_id"), self.own_id, doc.get("predecessor_id")):
            return None
        succ = Owner.from_dict(doc.get("successor"))
        if succ is not None and _runs_image(self.client, holder, succ.image_digest):
            # compose recreated U2 from its own image: that is U2 still (#169).
            return None
        return hid

    def _successor_problem(self, doc: dict) -> str | None:
        beat = self._beat()
        if not successor_is_current(doc, beat):
            return "stopped writing its heartbeat"
        try:
            age = self.now() - contract.parse_iso(str((beat or {}).get("seen_at")))
        except ValueError:
            return "wrote a heartbeat the updater cannot read"
        if age > STALE_SECONDS:
            return f"wrote no heartbeat for {int(age)} seconds"
        found = self._by_id(doc.get("successor_id"))
        if survey.running(found):
            self._gone_since = None
            return None
        # Not the container H2 made. H6 watches U2's heartbeat, and it is
        # fresh: if another container of the project runs U2's image, compose
        # (or a person) replaced it, and that one is U2 now. If none does yet,
        # a moment's grace for the replacement to start.
        succ = Owner.from_dict(doc.get("successor"))
        if succ is not None and runs(self.client, succ.image_digest):
            self._gone_since = None
            return None
        if self._gone_since is None:
            self._gone_since = self.now()
        if self.now() - self._gone_since < GONE_GRACE_SECONDS:
            return None
        return "stopped running"

    def _h7(self, doc: dict) -> None:
        succ = Owner.from_dict(doc.get("successor"))
        doc.update(
            step="H7",
            outcome=DONE,
            settled=True,
            sentence=f"The updater of {succ.version if succ else 'the new release'} ran ten minutes without trouble.",
        )
        self._save(doc)
        self.after_write("H7")
        self.mode, self.watching = Mode.RETIRED, None
        self._stop_myself()

    def _stop_myself(self) -> None:
        """Ask the engine to stop this updater's own container, without waiting for it (#257).

        The engine answers a stop only once the container has exited, and it
        exits when this process does: on the SIGTERM the stop sends, `serve`'s
        loop sees its `stop` event and returns. Waiting for the answer on that
        loop held it until the grace ran out and the engine killed it, exit
        137. So the call goes out on a thread of its own, and the loop goes
        back to its ticks after a short wait -- long enough for a refusal to
        come back, short enough to leave the grace for exiting 0.

        The engine's stop is still what ends it, not the process leaving on
        its own: that is what marks it stopped, and keeps `unless-stopped`
        from starting it again.
        """
        own_id = self.own_id
        if not own_id:
            return

        def ask() -> None:
            with contextlib.suppress(eng.EngineError, eng.NotAllowed, eng.EngineUnavailable):
                self.client.stop(own_id, grace=STOP_GRACE_SECONDS)

        asking = threading.Thread(target=ask, name="stop-myself", daemon=True)
        asking.start()
        asking.join(STOP_ANSWER_SECONDS)

    # ------------------------------------------------------------------ #
    # U1: failing, taking back
    # ------------------------------------------------------------------ #

    def _fail(self, doc: dict, why: str, live: bool = True) -> Outcome:
        """Before `go`: U2 never becomes the updater. Removed; U1 carries on."""
        sentence = f"The updater stayed on {self.me.version}: {why}."
        doc.update(outcome=FAILED, sentence=sentence)
        self._save(doc)
        self._settle_back(doc, live=live)
        return Outcome(False, sentence)

    def _take_back(self, doc: dict, why: str) -> str:
        """H6: after `go`, U2 failed. The outcome first, then the containers."""
        with contextlib.suppress(eng.EngineError, eng.NotAllowed):
            replacement = self._replacement(doc)
            if replacement is not None:
                # Its name is another updater's now: taking it back would
                # rename onto a live container (#259).
                return self._stand_down(
                    doc, f"{why}, and another container holds its name ({replacement[:12]})"
                )
        succ = Owner.from_dict(doc.get("successor"))
        sentence = (
            f"The updater of {succ.version if succ else 'the new release'} {why}; "
            f"the updater of {self.me.version} took back over."
        )
        doc.update(outcome=TAKEN_BACK, sentence=sentence, step="H6")
        self._save(doc)
        self._settle_back(doc, live=False)
        return sentence

    def _successor_container(self, doc: dict) -> dict | None:
        found = self._by_id(doc.get("successor_id"))
        if found is None:
            named = survey.find(self.client, doc["canonical"] + NEXT_SUFFIX)
            found = named if named is not None and named.get("Id") != doc.get("predecessor_id") else None
        return found

    def _settle_back(self, doc: dict, live: bool) -> None:
        """U1 current again, idempotently: U2 stopped and `-next`, U1 canonical, the lock, the pin."""
        canon = doc["canonical"]
        succ = Owner.from_dict(doc.get("successor"))
        found = self._successor_container(doc)
        if found is not None:
            sid = found["Id"]
            with contextlib.suppress(eng.EngineError, eng.NotAllowed):
                if survey.running(found):
                    self.client.stop(sid, grace=STOP_GRACE_SECONDS)
            with contextlib.suppress(eng.EngineError, eng.NotAllowed):
                self._rename(sid, canon + NEXT_SUFFIX)
            if doc.get("outcome") == FAILED:
                # Never the updater: nothing of it is kept. Where removing is
                # not allowed (`outdated`) it stays, stopped, as `-next`.
                with contextlib.suppress(eng.EngineError, eng.NotAllowed):
                    self.client.remove(sid, force=True)
        own_id = self.own_id or doc.get("predecessor_id")
        if isinstance(own_id, str):
            try:
                self._rename(own_id, canon)
            except (eng.EngineError, eng.NotAllowed) as e:
                if doc.get("outcome") != TAKEN_BACK:
                    raise
                # The name is not free: another container took it since the
                # outcome was written. Not a take-back, and not a crash (#259).
                self._stand_down(doc, f"could not take its name back ({e})", rewrite=True)
                return
        self.me = replace(self.me, container=canon)
        write_lock(self.vol, self.me, self.now())
        pinned = (
            pin.read(self.kit.site.project_dir).get(pin.UPDATER_KEY, "")
            if self.kit.site.project_dir
            else ""
        )
        if succ is not None and succ.image_digest in pinned:
            self._pin(self.me)
        if doc.get("kind") == "first" and succ is not None:
            j = journal.load(self.vol, request_of(doc))
            if j is not None and j.step == "2a" and j.owner is not None and j.owner.is_(succ):
                # Taken at 2a and nothing started since: the request is U1's
                # again, it carries on from step 3, and the handover is tried
                # again at step 10 (4.2, 2a).
                journal.hand_over(self.vol, j, self.me, self.now())
                journal.remember(self.vol, j, first_handover="taken_back")
        sentence = str(doc.get("sentence") or "")
        if doc.get("kind") == "update_updater":
            self._record_back(doc, sentence)
        elif not live and sentence:
            self.notes.append((request_of(doc), sentence))
        self.mode, self.watching, self._standby = Mode.CURRENT, None, None
        doc["settled"] = True
        self._save(doc)

    def _stand_down(self, doc: dict, why: str, rewrite: bool = False) -> str:
        """H6: U2 is not there to take back from, and its name is another's (#259).

        Recorded as what happened -- no take-back -- and then as H7: the lock,
        the pin and the canonical name are left to whoever holds them, and this
        updater stops itself, staying as it is named.
        """
        succ = Owner.from_dict(doc.get("successor"))
        if rewrite:
            why = f"{why}; it had recorded taking back over, which did not happen"
        sentence = (
            f"The updater of {succ.version if succ else 'the new release'} {why}; "
            f"the updater of {self.me.version} stood down instead of taking back over."
        )
        doc.update(outcome=STOOD_DOWN, sentence=sentence, step="H6", settled=True)
        self._save(doc)
        self.after_write("H6")
        self.notes.append((request_of(doc), sentence))
        with contextlib.suppress(OSError, volume.UnsafeFile):
            self.flush()
        self.mode, self.watching, self._standby = Mode.RETIRED, None, None
        self._stop_myself()
        return sentence

    def _record_back(self, doc: dict, sentence: str) -> None:
        """An `update_updater` request's record, when the handover ended without U2."""
        path = self.vol.history(request_of(doc))
        record = volume.read_own_json(path)
        state = "rolled_back" if doc.get("outcome") == TAKEN_BACK else "not_started"
        if record is not None and record.get("state") == state:
            return
        record = contract.History(
            id=request_of(doc),
            kind="update_updater",
            state=state,
            sentence=sentence,
            finished_at=contract.iso(self.now()),
            requested_by=(record or {}).get("requested_by"),
        ).to_dict()
        record["handover"] = doc.get("outcome")
        volume.write_json(path, record)

    def _record_success(self, doc: dict) -> None:
        """An `update_updater` request whose record U1 did not live to write."""
        rid = request_of(doc)
        if doc.get("kind") != "update_updater" or volume.read_own_json(self.vol.history(rid)):
            return
        succ = Owner.from_dict(doc.get("successor"))
        record = contract.History(
            id=rid,
            kind="update_updater",
            state="succeeded",
            sentence=f"The updater now runs {succ.version if succ else 'the new release'}.",
            finished_at=contract.iso(self.now()),
        ).to_dict()
        record["handover"] = "done"
        volume.write_json(self.vol.history(rid), record)

    # ------------------------------------------------------------------ #
    # U2: the self-check, then H5
    # ------------------------------------------------------------------ #

    def successor_tick(self) -> None:
        rid = str(self.successor_of)
        doc = load(self.vol, rid) if contract.is_uuid4(rid) else None
        if doc is None or doc.get("outcome") in (FAILED, TAKEN_BACK):
            self._retire()
            return
        holder = lock_holder(self.vol)
        go = journal.read_handover(self.vol, rid, "go")
        if (
            holder is not None
            and not holder.is_(self.me)
            and (go is not None or doc.get("outcome") == DONE)
        ):
            # Another updater holds the lock after U1 let go of it: whoever
            # took over since. Only U1's release makes this one current.
            self._retire()
            return
        if go is not None:
            self.take_over(doc, go)
            return
        ready = journal.read_handover(self.vol, rid, "ready")
        if ready is None or ready.get("container") != self.own_id:
            journal.write_handover(self.vol, rid, "ready", self.self_check(doc), self.now())
            self.after_write("H3")

    def self_check(self, doc: dict) -> dict:
        """H3: everything this updater needs to work, proven before it is trusted."""
        problem = None
        try:
            if self.client.ping().strip() != "OK":
                problem = "the container engine did not answer _ping"
            elif self._own() is None:
                problem = "it cannot find its own container in the project"
        except (eng.EngineError, eng.EngineUnavailable, eng.NotAllowed) as e:
            problem = f"it cannot use the container engine ({e})"
        if problem is None:
            probe = self.vol.root / "handover" / f"{doc['id']}.check-{secrets.token_hex(4)}"
            nonce = secrets.token_hex(16)
            try:
                volume.write_json(probe, {"nonce": nonce})
                back = volume.read_own_json(probe)
                probe.unlink()
                if (back or {}).get("nonce") != nonce:
                    problem = "what it wrote in the update volume did not read back"
            except (OSError, volume.UnsafeFile) as e:
                problem = f"it cannot write in the update volume ({e})"
        expected = (Owner.from_dict(doc.get("successor")) or Owner("", "", "")).image_digest
        if problem is None and self.me.image_digest != expected:
            problem = f"it runs {self.me.image_digest[:19] or 'an unknown image'}, not {expected[:19]}"
        return {
            "ok": problem is None,
            "problem": problem,
            "image_digest": self.me.image_digest,
            "version": self.me.version,
            "container": self.own_id,
        }

    def take_over(self, doc: dict, go: dict) -> None:
        """H5, idempotently: the names, the lock, the pin, the journal, the pong, the heartbeat."""
        canon = doc["canonical"]
        pred_id = doc.get("predecessor_id")
        self._clear(canon + PREVIOUS_SUFFIX, keep=(pred_id,))
        if isinstance(pred_id, str):
            self._rename(pred_id, canon + PREVIOUS_SUFFIX)
        own = self._own()
        if own is not None:
            self._rename(own["Id"], canon)
        self.me = replace(self.me, container=canon)
        now = self.now()
        write_lock(self.vol, self.me, now)
        self._pin(self.me)
        if doc.get("kind") == "first":
            j = journal.load(self.vol, request_of(doc))
            if j is not None and not (j.owner is not None and j.owner.is_(self.me)):
                journal.hand_over(self.vol, j, self.me, now)
                journal.remember(self.vol, j, first_handover="done")
        self._pong(go.get("ping"))
        self.mode = Mode.CURRENT
        self.successor_of = None
        self.on_beat()
        self.after_write("H5")

    def _pong(self, ping: object) -> None:
        """Answer the one ping U1 put in `go` (H5), as intake answers the app's."""
        if (
            not isinstance(ping, dict)
            or ping.get("kind") != "ping"
            or not contract.is_uuid4(ping.get("id"))
        ):
            return
        path = self.vol.history(ping["id"])
        if volume.read_own_json(path) is not None:
            return
        pong = contract.History(
            id=ping["id"],
            kind="ping",
            state="succeeded",
            sentence="The updater answered.",
            finished_at=contract.iso(self.now()),
        )
        volume.write_json(path, pong.to_dict())

    def _retire(self) -> None:
        self.mode = Mode.RETIRED
        own = self._own()
        if own is not None and survey.running(own):
            self._stop_myself()

    # ------------------------------------------------------------------ #
    # A `-previous` started by hand
    # ------------------------------------------------------------------ #

    def previous_tick(self) -> None:
        if self.kit.clock.settling():
            return
        name = self._name()
        canon = canonical(name)
        beat = self._beat()
        try:
            age = self.now() - contract.parse_iso(str((beat or {}).get("seen_at")))
        except ValueError:
            age = float("inf")
        found = survey.find(self.client, canon)
        if found is None or age > STALE_SECONDS:
            self.take_over_from(found, canon)
            return
        if self.now() - self.started_at >= PREVIOUS_PATIENCE_SECONDS:
            # The canonical updater is fine: nothing to take over.
            self._retire()

    def take_over_from(self, broken: dict | None, canon: str) -> None:
        """Stop and rename the broken canonical updater, and take its place."""
        if broken is not None:
            if survey.running(broken):
                self.client.stop(broken["Id"], grace=STOP_GRACE_SECONDS)
            self._clear(canon + NEXT_SUFFIX, keep=(broken["Id"],))
            self._rename(broken["Id"], canon + NEXT_SUFFIX)
        own = self._own()
        if own is not None:
            self._rename(own["Id"], canon)
        self.me = replace(self.me, container=canon)
        write_lock(self.vol, self.me, self.now())
        self._pin(self.me)
        self.mode = Mode.CURRENT
        self.on_beat()
