"""The journal and what an updater does after it was killed (design notes 5.6, 6.6, 15.1 U5).

Each test writes the journal for a step, throws the in-memory object away --
the kill -- reads the file back as a fresh start would, and asserts the 5.6
branch `resume` picks. The orchestration that acts on the branch is built on
top; what is asserted here is the decision, and what the files hold.
"""

from __future__ import annotations

import json
import uuid

import pytest

from updater import contract, journal
from updater.journal import Action, Observed, Owner
from updater.volume import Volume

NOW = 1_792_000_000.0
HASH = "scrypt$ln=15,r=8,p=1$c2FsdHNhbHRzYWx0c2FsdA$" + "A" * 43
OLD = Owner("sha256:" + "1" * 64, "0.7.1", "spend-tracker-updater-1")
NEW = Owner("sha256:" + "2" * 64, "0.9.0", "spend-tracker-updater-1-next")


@pytest.fixture
def vol(tmp_path):
    v = Volume(tmp_path / "update")
    v.init()
    return v


def apply_request(**over) -> contract.Request:
    return contract.Request(
        protocol=1,
        id=str(uuid.uuid4()),
        kind="apply",
        created_at=contract.iso(NOW),
        requested_by="usr_one",
        from_version="0.7.1",
        to_version="0.9.0",
        recovery_hash=HASH,
        **over,
    )


def killed_at(vol, step: str, owner: Owner = OLD, attempts: int = 0) -> journal.Journal:
    """Walk the journal to `step` the way an apply would, then reload it from disk."""
    j = journal.begin(vol, apply_request(), owner, NOW)
    order = journal.APPLY_STEPS if step in journal.APPLY_STEPS else ("0", "1", "2", "3", "4", "5", "6")
    for s in order[1 : order.index(step) + 1] if step in order else order[1:]:
        journal.start_step(vol, j, s, NOW)
    if step in journal.ROLLBACK_STEPS:
        for s in journal.ROLLBACK_STEPS[: journal.ROLLBACK_STEPS.index(step) + 1]:
            journal.start_step(vol, j, s, NOW)
    for _ in range(attempts):
        journal.count_rollback_attempt(vol, j)
    del j
    return journal.load(vol, _only_id(vol))


def _only_id(vol) -> str:
    (path,) = (vol.root / "journal").glob("*.json")
    return path.stem


# --------------------------------------------------------------------------- #
# U5: the 5.6 table, step by step
# --------------------------------------------------------------------------- #

TABLE = {
    "0": (Action.NOT_STARTED, None),
    "1": (Action.NOT_STARTED, None),
    "2": (Action.NOT_STARTED, None),
    "2a": (Action.CONTINUE, "3"),
    "3": (Action.RESTORE_PREVIOUS, "R3"),
    "4": (Action.RESTORE_PREVIOUS, "R3"),
    "5": (Action.ROLLBACK, "R3"),  # no drill report, no verified backup
    "6": (Action.ROLLBACK, "R1"),
    "7": (Action.ROLLBACK, "R1"),
    "8": (Action.ROLLBACK, "R1"),
    "9": (Action.FINISH_RECORD, None),
    "10": (Action.RESUME_HANDOVER, None),
    "R1": (Action.RESUME_ROLLBACK, "R1"),
    "R2": (Action.RESUME_ROLLBACK, "R2"),
    "R3": (Action.RESUME_ROLLBACK, "R3"),
    "R4": (Action.RESUME_ROLLBACK, "R4"),
    "R5": (Action.FINISH_RECORD, None),
}


def test_the_table_covers_every_step():
    assert set(TABLE) == set(journal.STEPS)


@pytest.mark.parametrize("step", journal.STEPS)
def test_killed_after_each_journal_write_the_resumed_branch_is_5_6s(vol, step):
    j = killed_at(vol, step)
    assert j.step == step
    got = journal.resume(j, Observed(), OLD)
    assert (got.action, got.from_step) == TABLE[step]
    assert got.carry_forward and got.owner == OLD


@pytest.mark.parametrize("step", ["5", "6", "7", "8"])
def test_once_the_drill_may_have_migrated_nothing_goes_forward(vol, step):
    j = killed_at(vol, step)
    for seen in (Observed(), Observed(drill_report=True, report_backup_verified=True)):
        got = journal.resume(j, seen, OLD)
        assert got.action in (Action.ROLLBACK, Action.WAIT_FOR_DRILL)


@pytest.mark.parametrize(
    "seen,expected",
    [
        (Observed(drill_running=True), (Action.WAIT_FOR_DRILL, None)),
        (Observed(drill_report=False, newer_backup_verifies=True), (Action.ROLLBACK, "R1")),
        (Observed(drill_report=False, newer_backup_verifies=False), (Action.ROLLBACK, "R3")),
        (Observed(drill_report=True, report_backup_verified=True), (Action.ROLLBACK, "R1")),
        (Observed(drill_report=True, report_backup_verified=False), (Action.ROLLBACK, "R3")),
    ],
)
def test_step_5_depends_on_the_drill_and_its_backup(vol, seen, expected):
    j = killed_at(vol, "5")
    got = journal.resume(j, seen, OLD)
    assert (got.action, got.from_step) == expected


def test_three_rollback_attempts_then_needs_recovery(vol):
    j = journal.begin(vol, apply_request(), OLD, NOW)
    for s in ("1", "2", "3", "4", "5", "6", "R1", "R2"):
        journal.start_step(vol, j, s, NOW)
    assert j.rollback_attempts == 1
    seen = []
    for _ in range(3):
        j = journal.load(vol, j.id)
        got = journal.resume(j, Observed(), OLD)
        seen.append(got.action)
        if got.action is Action.RESUME_ROLLBACK:
            journal.count_rollback_attempt(vol, j)
    assert seen == [Action.RESUME_ROLLBACK, Action.RESUME_ROLLBACK, Action.NEEDS_RECOVERY]
    assert journal.load(vol, j.id).rollback_attempts == 3


def test_a_rollback_entered_at_r3_is_one_attempt_too(vol):
    first = journal.begin(vol, apply_request(), OLD, NOW)
    for s in ("1", "2", "3", "4", "5", "R3"):
        journal.start_step(vol, first, s, NOW)
    second = journal.begin(vol, apply_request(), OLD, NOW)
    for s in ("1", "2", "3", "4", "5", "6", "R1", "R2", "R3"):
        journal.start_step(vol, second, s, NOW)
    assert (journal.load(vol, first.id).rollback_attempts, journal.load(vol, second.id).rollback_attempts) == (1, 1)


def test_a_step_this_updater_never_heard_of_is_rolled_back(vol):
    j = killed_at(vol, "5")
    j.step = "5b"
    journal.save(vol, j)
    got = journal.resume(journal.load(vol, j.id), Observed(), OLD)
    assert (got.action, got.from_step, got.carry_forward) == (Action.ROLLBACK, "R1", False)


# --------------------------------------------------------------------------- #
# C1: the owner, across a 2a handover
# --------------------------------------------------------------------------- #


def at_2a(vol, *, handed: bool) -> journal.Journal:
    j = journal.begin(vol, apply_request(), OLD, NOW)
    for s in ("1", "2", "2a"):
        journal.start_step(vol, j, s, NOW)
    if handed:
        journal.hand_over(vol, j, NEW, NOW + 30)
    return journal.load(vol, j.id)


def test_the_journal_records_each_owner_across_the_handover(vol):
    j = at_2a(vol, handed=True)
    assert j.owner == NEW
    assert [(Owner.from_dict(o["owner"]), o["from_step"]) for o in j.owners] == [(OLD, "0"), (NEW, "2a")]
    on_disk = json.loads(vol.journal(j.id).read_text())
    assert on_disk["owner"]["image_digest"] == NEW.image_digest and on_disk["protocol"] == 1


@pytest.mark.parametrize(
    "go,current,me,alive,expected",
    [
        # Before `go` and the successor's first heartbeat, the old updater owns it.
        (False, False, OLD, False, (Action.CONTINUE, OLD)),
        (True, False, OLD, False, (Action.CONTINUE, OLD)),
        (False, False, NEW, True, (Action.DEFER, OLD)),
        # After both, the successor does.
        (True, True, NEW, False, (Action.CONTINUE, NEW)),
        (True, True, OLD, True, (Action.DEFER, NEW)),
        # The owner is gone before step 3: the one left carries on (4.2, 2a).
        (True, True, OLD, False, (Action.CONTINUE, OLD)),
    ],
)
def test_after_a_restart_at_2a_exactly_one_updater_carries_on(vol, go, current, me, alive, expected):
    j = at_2a(vol, handed=False)
    seen = Observed(successor=NEW, go_written=go, successor_current=current, owner_alive=alive)
    got = journal.resume(j, seen, me)
    assert (got.action, got.owner) == expected
    if got.action is Action.CONTINUE:
        assert got.from_step == "3"


def test_a_third_updater_marks_a_2a_request_not_started(vol):
    stranger = Owner("sha256:" + "3" * 64, "0.8.0", "spend-tracker-updater-previous")
    j = at_2a(vol, handed=True)
    got = journal.resume(j, Observed(successor=NEW, owner_alive=False), stranger)
    assert got.action is Action.NOT_STARTED


@pytest.mark.parametrize(
    "step,seen,expected",
    [
        ("6", Observed(), (Action.ROLLBACK, "R1")),
        ("8", Observed(), (Action.ROLLBACK, "R1")),
        ("5", Observed(drill_running=True), (Action.WAIT_FOR_DRILL, None)),
        ("3", Observed(), (Action.RESTORE_PREVIOUS, "R3")),
    ],
)
def test_older_code_never_carries_a_newer_updaters_apply_forward(vol, step, seen, expected):
    """H6: the successor took the apply at 2a and died at `step`; the old one resumes."""
    j = journal.begin(vol, apply_request(), OLD, NOW)
    for s in ("1", "2", "2a"):
        journal.start_step(vol, j, s, NOW)
    journal.hand_over(vol, j, NEW, NOW)
    for s in journal.APPLY_STEPS[journal.APPLY_STEPS.index("3") : journal.APPLY_STEPS.index(step) + 1]:
        journal.start_step(vol, j, s, NOW)
    got = journal.resume(journal.load(vol, j.id), seen, OLD)
    assert (got.action, got.from_step) == expected
    assert got.carry_forward is False
    # While the successor still runs, the old one leaves it alone.
    assert journal.resume(journal.load(vol, j.id), Observed(owner_alive=True), OLD).action is Action.DEFER


def test_the_same_updater_restarted_carries_on_as_before(vol):
    j = killed_at(vol, "5", owner=OLD)
    restarted = Owner(OLD.image_digest, OLD.version, "spend-tracker-updater-1-renamed")
    got = journal.resume(j, Observed(drill_running=True), restarted)
    assert (got.action, got.carry_forward) == (Action.WAIT_FOR_DRILL, True)


# --------------------------------------------------------------------------- #
# Frozen at protocol 1; the hash, never the code
# --------------------------------------------------------------------------- #


def test_keys_a_newer_updater_wrote_survive_an_older_ones_rewrite(vol):
    j = killed_at(vol, "4")
    doc = json.loads(vol.journal(j.id).read_text())
    doc["drill_container"] = {"id": "abc", "deadline": {"seconds": 1800, "spent": 60}}
    doc["steps_v2"] = ["5a"]
    vol.journal(j.id).write_text(json.dumps(doc))
    older = journal.load(vol, j.id)
    journal.start_step(vol, older, "5", NOW)
    after = json.loads(vol.journal(j.id).read_text())
    assert after["drill_container"] == doc["drill_container"] and after["steps_v2"] == ["5a"]
    assert after["step"] == "5"


def test_a_journal_not_at_protocol_1_is_not_read(vol):
    j = killed_at(vol, "3")
    doc = json.loads(vol.journal(j.id).read_text())
    doc["protocol"] = 2
    vol.journal(j.id).write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        journal.load(vol, j.id)


def test_the_recovery_hash_is_kept_until_the_update_settles(vol):
    settled, unsettled = killed_at(vol, "9"), None
    unsettled = journal.begin(vol, apply_request(), OLD, NOW)
    assert HASH in vol.journal(settled.id).read_text()
    journal.settle(vol, settled)
    assert journal.load(vol, settled.id).recovery_hash is None
    assert "scrypt$" not in vol.journal(settled.id).read_text()
    assert journal.load(vol, unsettled.id).recovery_hash == HASH


def test_handover_files_are_protocol_1_and_a_newer_one_is_not_trusted(vol):
    rid = str(uuid.uuid4())
    journal.write_handover(vol, rid, "request", {"successor_digest": NEW.image_digest}, NOW)
    journal.write_handover(vol, rid, "go", {}, NOW + 5)
    assert journal.read_handover(vol, rid, "request")["successor_digest"] == NEW.image_digest
    assert journal.read_handover(vol, rid, "go")["protocol"] == 1
    assert journal.read_handover(vol, rid, "ready") is None
    doc = json.loads(vol.handover(rid, "go").read_text())
    vol.handover(rid, "go").write_text(json.dumps({**doc, "protocol": 2}))
    assert journal.read_handover(vol, rid, "go") is None
    with pytest.raises(ValueError):
        journal.write_handover(vol, rid, "launch", {}, NOW)


def test_starting_an_unknown_step_is_refused(vol):
    j = journal.begin(vol, apply_request(), OLD, NOW)
    with pytest.raises(ValueError):
        journal.start_step(vol, j, "11", NOW)
    assert journal.load(vol, j.id).step == "0"
