"""U4 and U5 of the design notes' test plan (15.1), against the simulated installation.

U4  The sidecar's id and `StartedAt` are unchanged across an apply and every
    rollback path, and no call that changes a container ever names it -- only
    reads and the health probe's exec.

U5  The journal: the updater is killed after **each** journal write, in turn,
    and started again. The branch it resumes on is 5.6's for the step the
    journal had reached, and the end state is never a running app at a stamp
    its code does not match. The drill never runs twice (one backup folder
    per request). Run for an update that succeeds and for one whose migration
    fails, so the kills land inside the rollback as well.
"""

from __future__ import annotations

import pytest

from tests.updater_world import A, B, World
from updater import journal


def apply_in(w: World, service) -> dict:
    service.startup()
    report = w.prepared(service)
    req = w.apply_request(report)
    w.write_request(req)
    service.tick()
    return req


# --------------------------------------------------------------------------- #
# U4
# --------------------------------------------------------------------------- #

PATHS = {
    "succeeds": dict(),
    "no backup: R3": dict(drill="no-backup"),
    "migration fails: R1-R5": dict(drill="migration-fails"),
    "health fails: R1-R5": dict(broken={B}),
    "restore fails: needs_recovery": dict(drill="migration-fails", restore_fails=3),
    "old app does not return: needs_recovery": dict(drill="migration-fails", broken={A}),
}
ENDS = {
    "succeeds": "succeeded",
    "no backup: R3": "rolled_back",
    "migration fails: R1-R5": "rolled_back",
    "health fails: R1-R5": "rolled_back",
    "restore fails: needs_recovery": "needs_recovery",
    "old app does not return: needs_recovery": "needs_recovery",
}


@pytest.mark.parametrize("path", sorted(PATHS))
def test_u4_the_sidecar_is_never_restarted_on_any_path(tmp_path, path):
    with World(tmp_path, layout="sidecar") as w:
        for key, value in PATHS[path].items():
            setattr(w, key, value)
        sidecar = w.fake.containers[w.sidecar_id]
        before = dict(w.fake.inspect_of(sidecar)["State"])
        req = apply_in(w, w.service())
        record = w.history(req["id"])
        assert record["state"] == ENDS[path], record

        after = w.fake.inspect_of(w.fake.containers[w.sidecar_id])
        assert after["Id"] == w.sidecar_id
        assert after["State"]["StartedAt"] == before["StartedAt"] and after["State"]["Running"]
        changing = [c for c in w.fake.calls if c.method != "GET" and w.sidecar_id in c.bare]
        assert all(c.bare == f"/containers/{w.sidecar_id}/exec" for c in changing), changing
        assert all(c.body["Cmd"][0] == "wget" for c in changing)
        # Everything the updater created in that layout joined the sidecar as it is.
        for call in w.fake.calls:
            if call.bare == "/containers/create" and call.body["HostConfig"]["NetworkMode"] != "none":
                assert call.body["HostConfig"]["NetworkMode"] == f"container:{w.sidecar_id}"
        w.no_running_app_at_a_mismatched_stamp()


# --------------------------------------------------------------------------- #
# U5
# --------------------------------------------------------------------------- #


class Killed(BaseException):
    """The updater died: nothing after this write happened."""


#: 5.6: the last step started, and how the request ends after a restart.
def expected(step: str, mode: str) -> str:
    if step in ("0", "1", "2", "3", "4"):
        return "not_started"
    if step in ("2a", "9", "10"):
        # 2a: the handover's owner -- this updater, the handover being
        # unavailable -- carries on from step 3.
        return "succeeded"
    return "rolled_back"


def kill_after(monkeypatch, n: int) -> list[str]:
    writes: list[str] = []
    real = journal.save

    def save(vol, j):
        real(vol, j)
        writes.append(j.step)
        if len(writes) == n:
            raise Killed(j.step)

    monkeypatch.setattr(journal, "save", save)
    return writes


def journal_writes(tmp_path, mode: str) -> int:
    """How many journal writes an uninterrupted apply makes."""
    with World(tmp_path / "count") as w:
        w.drill = mode
        writes: list[str] = []
        real = journal.save
        journal.save = lambda vol, j: (real(vol, j), writes.append(j.step))  # type: ignore[assignment]
        try:
            apply_in(w, w.service())
        finally:
            journal.save = real  # type: ignore[assignment]
        return len(writes)


@pytest.mark.parametrize("mode", ["ok", "migration-fails"])
def test_u5_killed_after_every_journal_write_resumes_on_the_right_branch(tmp_path, monkeypatch, mode):
    total = journal_writes(tmp_path, mode)
    assert total >= 15
    seen_steps = set()
    for n in range(1, total + 1):
        with World(tmp_path / f"run-{n}") as w:
            w.drill = mode
            service = w.service()
            service.startup()
            report = w.prepared(service)
            req = w.apply_request(report)
            w.write_request(req)
            writes = kill_after(monkeypatch, n)
            with pytest.raises(Killed) as killed:
                service.tick()
            monkeypatch.undo()
            step = str(killed.value)
            seen_steps.add(step)
            assert w.journal(req["id"]).step == step

            # The updater starts again.
            again = w.service()
            again.startup()
            record = w.history(req["id"])
            assert record is not None, (n, step)
            settled_before = len(writes) == total
            want = record["state"] if settled_before else expected(step, mode)
            if mode == "migration-fails" and want == "succeeded":
                want = "rolled_back"
            assert record["state"] == want, (n, step, record["sentence"])

            # Never a running app at a stamp its code does not match, and one app.
            w.no_running_app_at_a_mismatched_stamp()
            assert len(w.running_apps()) == 1, (n, step)
            assert w.ledger.drills <= 1 and len(w.ledger.backups) <= 1, (n, step)
            if record["state"] == "succeeded":
                assert w.ledger.stamp == B
            else:
                assert w.ledger.stamp == A
                assert w.version_of(w.running_apps()[0]) == A
            assert w.journal(req["id"]).recovery_hash is None
    # The kills landed on every step an apply has, rollback steps included.
    must = (
        {"0", "1", "2a", "3", "4", "5", "6", "7", "8", "9", "10"}
        if mode == "ok"
        else {"5", "R1", "R2", "R3", "R4", "R5"}
    )
    assert must <= seen_steps, sorted(seen_steps)


def test_u5_a_resumed_drill_still_running_is_waited_for_not_started_again(tmp_path):
    with World(tmp_path) as w:
        service = w.service()
        service.startup()
        report = w.prepared(service)
        w.drill = "hang"
        req = w.apply_request(report)
        w.write_request(req)
        polls = []

        def die_on_the_third_poll():
            polls.append(1)
            if len(polls) == 3:
                raise Killed("5")

        w.time.on_sleep = die_on_the_third_poll
        with pytest.raises(Killed):
            service.tick()
        w.time.on_sleep = None
        assert w.journal(req["id"]).step == "5" and w.ledger.drills == 1
        # While the updater was down, the drill finished.
        w.finish_hung_drill()
        again = w.service()
        again.startup()
        record = w.history(req["id"])
        assert record["state"] == "rolled_back"  # 5.6 R13: a report with a verified backup is R1.
        assert w.ledger.drills == 1 and w.ledger.stamp == A


def test_u5_a_drill_still_running_after_a_restart_is_waited_for_and_carried_forward(tmp_path):
    with World(tmp_path) as w:
        service = w.service()
        service.startup()
        report = w.prepared(service)
        w.drill = "hang"
        req = w.apply_request(report)
        w.write_request(req)
        polls = []

        def die_on_the_second_poll():
            polls.append(1)
            if len(polls) == 2:
                raise Killed("5")

        w.time.on_sleep = die_on_the_second_poll
        with pytest.raises(Killed):
            service.tick()
        polls.clear()
        # Restarted while the drill still runs: it is waited for, and finishes.
        w.time.on_sleep = lambda: (polls.append(1), len(polls) == 2 and w.finish_hung_drill())
        again = w.service()
        again.startup()
        w.time.on_sleep = None
        record = w.history(req["id"])
        assert record["state"] == "succeeded" and w.ledger.stamp == B and w.ledger.drills == 1
