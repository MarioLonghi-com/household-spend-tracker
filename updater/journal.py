"""The journal: which step of an apply has *started*, and what to do after a crash (5.6).

`journal/<id>.json` is written atomically **before** each step starts. On
start, and after every detected gap (8.6), the updater reads it and `resume`
says which branch of the 5.6 table applies. Every step is idempotent, so
re-running the one that was interrupted is always safe; the table only
decides whether to go on, go back, or wait.

**The owner (C1).** Since the updater goes first (step 2a), the updater that
finishes an apply may not be the one that took it. The journal records which
updater owns the request, and every hand-over of it, so that after an engine
restart exactly one of them carries on -- and an older updater never carries
a newer one's apply forward (6.6, H6).

**Frozen at protocol 1.** The journal and the handover files only ever gain
keys, like requests, so an older standby updater can read a newer one's. A
key this updater does not know is kept, untouched, when it rewrites the file.

The recovery code's hash is stored here from step 0 until the update settles;
the code itself never is.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from updater import contract, volume
from updater.contract import FROZEN_PROTOCOL, Request
from updater.volume import Volume

#: The apply steps of 4.2 and the rollback steps of 4.3, in order.
APPLY_STEPS = ("0", "1", "2", "2a", "3", "4", "5", "6", "7", "8", "9", "10")
ROLLBACK_STEPS = ("R1", "R2", "R3", "R4", "R5")
STEPS = APPLY_STEPS + ROLLBACK_STEPS

#: Rollback attempts in all before the update `needs_recovery` (4.3, 5.6).
MAX_ROLLBACK_ATTEMPTS = 3


@dataclass(frozen=True)
class Owner:
    """An updater, as the journal names it. Its image is what identifies it:
    the container is renamed during a handover (H5), the image is not.

    An image has more than one digest (#287). Pulled by the digest of a
    multi-arch index, Podman -- and possibly Docker's containerd image store
    -- records both that index digest and the digest of the platform's own
    manifest in `RepoDigests`, in no order anybody promises. `image_digest`
    is the one this updater writes in files: the index digest a release
    publishes, wherever it can tell which that is (`handover.known_as`).
    `digests` is every digest its image carries for the updater repository,
    known only for the updater itself -- an Owner read back from a file has
    the one it was written with. Identity is membership, never one entry.
    """

    image_digest: str
    version: str
    container: str
    digests: tuple[str, ...] = field(default=(), compare=False)

    @property
    def ids(self) -> frozenset[str]:
        """Every digest known to name this updater's image."""
        return frozenset(d for d in (self.image_digest, *self.digests) if d)

    def carries(self, digest: str) -> bool:
        """Whether `digest` names this updater's image."""
        return bool(digest) and digest in self.ids

    def preferring(self, *wanted: str | None) -> Owner:
        """This updater named by the first of `wanted` its image carries, else as it is."""
        for d in wanted:
            if d and self.carries(d):
                return replace(self, image_digest=d)
        return self

    def is_(self, other: Owner | None) -> bool:
        if other is None:
            return False
        if self.ids or other.ids:
            return bool(self.ids & other.ids)
        # Neither knows its image (outside a container): as before, the same.
        return True

    def newer_than(self, other: Owner) -> bool:
        return contract.parse_version(self.version) > contract.parse_version(other.version)

    def to_dict(self) -> dict:
        return {"image_digest": self.image_digest, "version": self.version, "container": self.container}

    @classmethod
    def from_dict(cls, d: object) -> Owner | None:
        if not isinstance(d, dict):
            return None
        try:
            return cls(str(d["image_digest"]), str(d["version"]), str(d["container"]))
        except KeyError:
            return None


_KNOWN = (
    "protocol",
    "id",
    "kind",
    "step",
    "owner",
    "owners",
    "started",
    "recovery_hash",
    "rollback_attempts",
    "created_at",
    "context",
)


@dataclass
class Journal:
    id: str
    step: str
    owner: Owner | None
    created_at: str
    kind: str = "apply"
    recovery_hash: str | None = None
    #: One entry per step started: `{"step": ..., "at": ...}`.
    started: list[dict] = field(default_factory=list)
    #: The owner trail: `{"owner": {...}, "from_step": ..., "at": ...}`.
    owners: list[dict] = field(default_factory=list)
    rollback_attempts: int = 0
    protocol: int = FROZEN_PROTOCOL
    #: What the orchestration learnt and must not learn twice: the app's name
    #: and the previous container's id, the sidecar as preflight found it, the
    #: drill's backup folder. Only ever gains keys (frozen at protocol 1).
    context: dict = field(default_factory=dict)
    #: Keys a newer updater wrote, kept as they were.
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.extra)
        d.update(
            {
                "protocol": self.protocol,
                "id": self.id,
                "kind": self.kind,
                "step": self.step,
                "owner": self.owner.to_dict() if self.owner else None,
                "owners": list(self.owners),
                "started": list(self.started),
                "recovery_hash": self.recovery_hash,
                "rollback_attempts": self.rollback_attempts,
                "created_at": self.created_at,
                "context": dict(self.context),
            }
        )
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Journal:
        if d.get("protocol") != FROZEN_PROTOCOL:
            raise ValueError("a journal is always protocol 1")
        return cls(
            id=str(d["id"]),
            kind=str(d.get("kind", "apply")),
            step=str(d["step"]),
            owner=Owner.from_dict(d.get("owner")),
            owners=list(d.get("owners") or []),
            started=list(d.get("started") or []),
            recovery_hash=d.get("recovery_hash"),
            rollback_attempts=int(d.get("rollback_attempts") or 0),
            created_at=str(d.get("created_at", "")),
            context=dict(d.get("context") or {}),
            extra={k: v for k, v in d.items() if k not in _KNOWN},
        )


def save(vol: Volume, j: Journal) -> None:
    volume.write_json(vol.journal(j.id), j.to_dict())


def load(vol: Volume, request_id: str) -> Journal | None:
    d = volume.read_own_json(vol.journal(request_id))
    return Journal.from_dict(d) if d is not None else None


def begin(vol: Volume, request: Request, owner: Owner | None, now: float) -> Journal:
    """Step 0 of an apply: the journal exists, owned, holding the code's hash."""
    at = contract.iso(now)
    j = Journal(
        id=request.id,
        kind=request.kind,
        step="0",
        owner=owner,
        created_at=at,
        recovery_hash=request.recovery_hash,
        started=[{"step": "0", "at": at}],
        owners=[{"owner": owner.to_dict(), "from_step": "0", "at": at}] if owner else [],
    )
    save(vol, j)
    return j


def start_step(vol: Volume, j: Journal, step: str, now: float) -> Journal:
    """Record that `step` starts. Durable before the step does anything."""
    if step not in STEPS:
        raise ValueError(f"no step {step!r}")
    if step in ROLLBACK_STEPS and j.step not in ROLLBACK_STEPS:
        # Entering a rollback, at R1 or (nothing migrated) at R3: one attempt.
        j.rollback_attempts += 1
    j.step = step
    j.started.append({"step": step, "at": contract.iso(now)})
    save(vol, j)
    return j


def remember(vol: Volume, j: Journal, **facts: object) -> Journal:
    """Add facts to the journal's context, durably, before acting on them."""
    j.context.update(facts)
    save(vol, j)
    return j


def count_rollback_attempt(vol: Volume, j: Journal) -> Journal:
    """A rollback resumed part-way (R2-R4) is an attempt too."""
    j.rollback_attempts += 1
    save(vol, j)
    return j


def hand_over(vol: Volume, j: Journal, successor: Owner, now: float) -> Journal:
    """The successor owns the request from here (2a, H5)."""
    j.owner = successor
    j.owners.append({"owner": successor.to_dict(), "from_step": j.step, "at": contract.iso(now)})
    save(vol, j)
    return j


def settle(vol: Volume, j: Journal) -> Journal:
    """Succeeded, rolled back or recovered: the recovery code opens nothing any more."""
    j.recovery_hash = None
    save(vol, j)
    return j


# --------------------------------------------------------------------------- #
# The handover's own files (6.6), frozen at protocol 1 like the journal.
# --------------------------------------------------------------------------- #

HANDOVER_PARTS = ("request", "ready", "go")


def write_handover(vol: Volume, request_id: str, part: str, body: dict, now: float) -> None:
    if part not in HANDOVER_PARTS:
        raise ValueError(part)
    doc = {"protocol": FROZEN_PROTOCOL, "id": request_id, "at": contract.iso(now), **body}
    volume.write_json(vol.handover(request_id, part), doc)


def read_handover(vol: Volume, request_id: str, part: str) -> dict | None:
    doc = volume.read_own_json(vol.handover(request_id, part))
    if doc is None or doc.get("protocol") != FROZEN_PROTOCOL:
        return None
    return doc


def handover_owner(predecessor: Owner, successor: Owner | None, go_written: bool, successor_current: bool) -> Owner:
    """Who owns a request after an engine restart mid-handover (6.6).

    The predecessor, until `go` was written *and* the successor's first
    heartbeat as `current` exists.
    """
    if successor is not None and go_written and successor_current:
        return successor
    return predecessor


# --------------------------------------------------------------------------- #
# Resuming (5.6)
# --------------------------------------------------------------------------- #


class Action(StrEnum):
    #: 0-2: mark *not started*; make sure the app is running.
    NOT_STARTED = "not_started"
    #: 2a: the owner the handover settled on continues from step 3.
    CONTINUE = "continue"
    #: 3-4: make sure the previous container runs under its own name; *not started*.
    RESTORE_PREVIOUS = "restore_previous"
    #: 5, drill container still running: wait, the deadline extended by any gap.
    WAIT_FOR_DRILL = "wait_for_drill"
    #: Roll back, from `from_step` (R1, or R3 when nothing can have been migrated).
    ROLLBACK = "rollback"
    #: R1-R4: resume the rollback where it was.
    RESUME_ROLLBACK = "resume_rollback"
    #: Three rollback attempts used: `needs_recovery` (Part 11).
    NEEDS_RECOVERY = "needs_recovery"
    #: 9, or R5: finish the record.
    FINISH_RECORD = "finish_record"
    #: 10: resume the handover from its own journal.
    RESUME_HANDOVER = "resume_handover"
    #: Another updater owns this request and is alive: leave it.
    DEFER = "defer"


@dataclass(frozen=True)
class Observed:
    """What the resuming updater found, re-inspecting before it decides (8.6)."""

    drill_running: bool = False
    #: Whether the drill left a report in `work/<id>/`.
    drill_report: bool = False
    #: Whether that report names a verified backup.
    report_backup_verified: bool = False
    #: Without a report: whether a backup folder newer than the request verifies.
    newer_backup_verifies: bool = False
    #: Whether the owner of record, when it is not the resuming updater, still runs.
    owner_alive: bool = False
    #: During a 2a handover: the successor, and how far the handover got.
    successor: Owner | None = None
    go_written: bool = False
    successor_current: bool = False


@dataclass(frozen=True)
class Resume:
    action: Action
    from_step: str | None = None
    #: False when the resuming updater is older than the owner of record:
    #: older code never carries a newer updater's apply forward (H6). A
    #: `WAIT_FOR_DRILL` with this False ends in a rollback, not step 6.
    carry_forward: bool = True
    owner: Owner | None = None


def resume(j: Journal, seen: Observed, me: Owner) -> Resume:
    """The 5.6 branch for the last step started. Pure: decides, does nothing."""
    step = j.step
    if step not in STEPS:
        # A step this updater has never heard of was written by a newer one.
        return Resume(Action.ROLLBACK, "R1", carry_forward=False, owner=me)

    if step == "2a":
        before = [Owner.from_dict(o.get("owner")) for o in j.owners if o.get("from_step") != "2a"]
        predecessor = before[-1] if before and before[-1] else (j.owner or me)
        recorded = j.owner if j.owner is not None and not j.owner.is_(predecessor) else None
        successor = seen.successor or recorded
        if recorded is not None:
            # The successor wrote itself in as owner at H5, after `go`.
            owner = recorded
        else:
            owner = handover_owner(predecessor, successor, seen.go_written, seen.successor_current)
        if owner.is_(me):
            return Resume(Action.CONTINUE, "3", owner=me)
        if seen.owner_alive:
            return Resume(Action.DEFER, owner=owner)
        if me.is_(predecessor) or me.is_(successor):
            # The other one is gone before step 3: nothing has stopped, and the
            # one left carries on (4.2, 2a).
            return Resume(Action.CONTINUE, "3", owner=me)
        return Resume(Action.NOT_STARTED, owner=me)

    carry = True
    if j.owner is not None and not j.owner.is_(me):
        if seen.owner_alive:
            return Resume(Action.DEFER, owner=j.owner)
        carry = not j.owner.newer_than(me)

    if step in ("0", "1", "2"):
        return Resume(Action.NOT_STARTED, carry_forward=carry, owner=me)
    if step in ("3", "4"):
        return Resume(Action.RESTORE_PREVIOUS, "R3", carry_forward=carry, owner=me)
    if step == "5":
        if seen.drill_running:
            return Resume(Action.WAIT_FOR_DRILL, carry_forward=carry, owner=me)
        verified = seen.report_backup_verified if seen.drill_report else seen.newer_backup_verifies
        # No verified backup: the drill backs up before it migrates, so
        # nothing was migrated, and only the previous container needs its name.
        return Resume(Action.ROLLBACK, "R1" if verified else "R3", carry_forward=carry, owner=me)
    if step in ("6", "7", "8"):
        return Resume(Action.ROLLBACK, "R1", carry_forward=carry, owner=me)
    if step == "9":
        return Resume(Action.FINISH_RECORD, carry_forward=carry, owner=me)
    if step == "10":
        return Resume(Action.RESUME_HANDOVER, carry_forward=carry, owner=me)
    if step == "R5":
        return Resume(Action.FINISH_RECORD, carry_forward=carry, owner=me)
    # R1-R4
    if j.rollback_attempts >= MAX_ROLLBACK_ATTEMPTS:
        return Resume(Action.NEEDS_RECOVERY, step, carry_forward=carry, owner=me)
    return Resume(Action.RESUME_ROLLBACK, step, carry_forward=carry, owner=me)
