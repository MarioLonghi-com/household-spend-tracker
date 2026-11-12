"""#443: a `status.json` an updater stopped in the middle of no longer refuses everything after it.

Only an apply or a go-back keeps a journal, and only a journal is resumed. An
updater stopped during a prepare, a discard or an `update_updater` -- or
after intake took an apply and before its journal began, or between a
request's record and its status -- left the status `accepted` or `running`
with nothing to settle it, and the app refused every later request.

Each test stops the updater at one of those points (`Killed`, as a process
dying would leave the files), starts a fresh one on the same volume, and
asserts what the status and the record say afterwards, and that the next
prepare is taken and runs. The ones a journal or a handover still owns are
left as they are: a genuinely unfinished apply stays in flight.
"""

from __future__ import annotations

import pytest

from tests.updater_world import A, B, C, World
from updater import contract, journal, prepare, volume
from updater import engine as eng
from updater.site import Records


class Killed(BaseException):
    """The updater died: nothing after this point happened."""


@pytest.fixture
def world(tmp_path):
    with World(tmp_path) as w:
        yield w


def status(w: World) -> dict:
    doc = volume.read_own_json(w.volume.status)
    assert doc is not None
    return doc


def the_next_prepare_runs(w: World, service, to: str = C) -> dict:
    """A prepare after the restart is taken and gets as far as its report."""
    req = w.prepare_request(to)
    w.write_request(req)
    service.tick()
    record = w.history(req["id"])
    assert record is not None and record["state"] == "succeeded", record
    assert status(w)["id"] == req["id"] and status(w)["state"] == "succeeded"
    report = volume.read_own_json(w.volume.prepared(req["id"]))
    assert report is not None and report["to_version"] == to
    return report


def started_again(w: World):
    again = w.service()
    again.startup()
    return again


# --------------------------------------------------------------------------- #
# Stopped with no journal: settled as interrupted
# --------------------------------------------------------------------------- #


def test_a_prepare_stopped_mid_way_is_refused_at_restart_and_the_next_prepare_runs(world, monkeypatch):
    service = world.service()
    service.startup()
    earlier = world.prepared(service)  # a settled request of its own beside it
    earlier_record = world.history(earlier["id"])

    def dies(kit, request, records):
        records.say("Checking free disk and memory.", step="P3")
        raise Killed

    monkeypatch.setattr(prepare, "run", dies)
    req = world.prepare_request(C)
    world.write_request(req)
    with pytest.raises(Killed):
        service.tick()
    monkeypatch.undo()
    assert status(world)["id"] == req["id"] and status(world)["state"] == "running"
    assert world.history(req["id"]) is None

    again = started_again(world)
    record = world.history(req["id"])
    assert record["state"] == "refused"
    assert record["code"] == contract.INTERRUPTED == "interrupted"
    assert record["sentence"] == "Preparing failed: the updater restarted before it finished."
    assert record["kind"] == "prepare" and record["requested_by"] == "usr_owner"
    after = status(world)
    assert (after["id"], after["state"]) == (req["id"], "refused")
    # The step it reached is still told, with the outcome after it.
    assert after["sentences"][-2:] == ["Checking free disk and memory.", record["sentence"]]
    assert volume.read_own_json(world.volume.prepared(req["id"])) is None
    # The other request's record is not touched.
    assert world.history(earlier["id"]) == earlier_record

    the_next_prepare_runs(world, again)


def test_an_apply_taken_and_stopped_before_its_journal_began_did_not_start(world, monkeypatch):
    service = world.service()
    service.startup()
    report = world.prepared(service)
    req = world.apply_request(report)
    world.write_request(req)

    def dies(*args, **kwargs):
        raise Killed

    monkeypatch.setattr(journal, "begin", dies)
    with pytest.raises(Killed):
        service.tick()
    monkeypatch.undo()
    assert status(world)["state"] == "accepted" and world.journal(req["id"]) is None

    again = started_again(world)
    record = world.history(req["id"])
    assert (record["state"], record["code"]) == ("not_started", "interrupted")
    assert record["sentence"] == "Not started: the updater restarted before the update began."
    assert status(world)["state"] == "not_started"
    # Nothing happened to the installation: the app is A's, the ledger at A, no drill.
    assert world.ledger.stamp == A and world.ledger.drills == 0
    assert [world.version_of(c) for c in world.running_apps()] == [A]

    the_next_prepare_runs(world, again)


def test_an_update_updater_stopped_before_its_handover_stays_on_its_version(world):
    rid = "6f1c1c5e-6e3b-4f0a-9d5e-2a0c7d3f8b11"
    volume.write_json(
        world.volume.status,
        contract.Status(id=rid, kind="update_updater", state="running", updated_at="2026-11-12T10:00:00Z").to_dict(),
    )

    again = started_again(world)
    record = world.history(rid)
    assert (record["state"], record["code"]) == ("not_started", "interrupted")
    assert record["sentence"] == f"The updater stayed on {A}: it restarted before the handover began."
    assert status(world)["state"] == "not_started"

    the_next_prepare_runs(world, again, to=B)


# --------------------------------------------------------------------------- #
# Stopped after the record or the report: the files already say how it ended
# --------------------------------------------------------------------------- #


def test_stopped_between_the_record_and_the_status_the_record_decides(world, monkeypatch):
    service = world.service()
    service.startup()
    real = Records._status

    def dies_on_the_last_status(self, sentence, step, state):
        if state != "running":
            raise Killed
        real(self, sentence, step, state)

    monkeypatch.setattr(Records, "_status", dies_on_the_last_status)
    req = world.prepare_request(B)
    world.write_request(req)
    with pytest.raises(Killed):
        service.tick()
    monkeypatch.undo()
    assert world.history(req["id"])["state"] == "succeeded"
    assert status(world)["state"] == "running"
    before = status(world)["sentences"]

    again = started_again(world)
    after = status(world)
    assert (after["id"], after["state"]) == (req["id"], "succeeded")
    assert after["sentences"] == [*before, f"Prepared {B}."]
    # The record is the one the prepare wrote, not an interrupted one.
    assert world.history(req["id"]).get("code") is None

    the_next_prepare_runs(world, again, to=C)


def test_a_prepare_stopped_after_its_report_is_recorded_as_prepared(world, monkeypatch):
    service = world.service()
    service.startup()
    real = Records.finish

    def dies_before_the_record(self, state, sentence, **kw):
        if self.kind == "prepare" and state == "succeeded":
            raise Killed
        return real(self, state, sentence, **kw)

    monkeypatch.setattr(Records, "finish", dies_before_the_record)
    req = world.prepare_request(B)
    world.write_request(req)
    with pytest.raises(Killed):
        service.tick()
    monkeypatch.undo()
    assert volume.read_own_json(world.volume.prepared(req["id"]))["to_version"] == B
    assert world.history(req["id"]) is None

    started_again(world)
    record = world.history(req["id"])
    assert (record["state"], record["sentence"], record["code"]) == ("succeeded", f"Prepared {B}.", "interrupted")
    assert status(world)["state"] == "succeeded"


# --------------------------------------------------------------------------- #
# Still owned by something: left in flight
# --------------------------------------------------------------------------- #


def test_an_apply_its_journal_still_holds_is_left_in_flight(world):
    service = world.service()
    service.startup()
    report = world.prepared(service)

    def engine_goes(c):
        # The engine goes away in the middle of the drill (row 20).
        world.drill = "hang"
        world.time.on_sleep = lambda: setattr(world.fake, "gone", True)

    world.on_drill = engine_goes
    req = world.apply_request(report)
    world.write_request(req)
    service.tick()
    assert world.journal(req["id"]).step == "5" and world.history(req["id"]) is None
    held = status(world)
    assert held["id"] == req["id"] and held["state"] not in contract.SETTLED_STATES

    # The engine still away: the journal cannot be resumed, and a status a
    # journal holds is not settled under it.
    assert service.resume_pending
    assert service.settle_status() is None
    assert status(world) == held and world.history(req["id"]) is None

    # The engine back without the one-off, the resume finishes the apply,
    # and only then is it settled -- by the apply, not as interrupted.
    world.time.on_sleep = None
    world.fake.gone = False
    drill = next(c for c in world.fake.containers.values() if c["Labels"].get(eng.ROLE_LABEL) == "drill")
    del world.fake.containers[drill["Id"]]
    world.on_drill = None
    service.tick()
    record = world.history(req["id"])
    assert record is not None and record["state"] in contract.SETTLED_STATES
    assert record.get("code") != contract.INTERRUPTED
    assert status(world)["state"] == record["state"]


def test_an_update_updater_with_a_handover_of_its_own_is_left_to_it(world):
    rid = "0b7d6c2a-3f4e-4a51-8c9d-7e6f5a4b3c21"
    held = contract.Status(id=rid, kind="update_updater", state="running", updated_at="2026-11-12T10:00:00Z")
    volume.write_json(world.volume.status, held.to_dict())
    journal.write_handover(world.volume, rid, "request", {"id": rid, "kind": "update_updater"}, world.clock.now())

    service = world.service()
    assert service.settle_status() is None
    assert status(world) == held.to_dict() and world.history(rid) is None


def test_a_settled_status_is_left_as_it_is(world):
    service = world.service()
    service.startup()
    report = world.prepared(service)
    settled = status(world)
    assert settled["id"] == report["id"] and settled["state"] == "succeeded"
    assert service.settle_status() is None
    assert status(world) == settled
