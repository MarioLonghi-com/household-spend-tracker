"""Going back one step from the browser (#173), against the simulated installation.

An apply A -> B succeeds, the ledger gains rows at B, and a `go_back`
request takes it back: the update's backup restored with A's image, A's
container made again from `history/<update>.previous.json` (C9), the pin at
A. Each test asserts what it did to the ledger and the containers.
"""

from __future__ import annotations

import os

import pytest

from tests.updater_world import APP, UPD, A, B, World, digest, platform_digest, ref
from updater import engine as eng
from updater import journal, pin, volume
from updater.apply import previous_name


class Killed(BaseException):
    """The updater died after this journal write."""


@pytest.fixture
def world(tmp_path):
    with World(tmp_path) as w:
        yield w


def updated(w: World, service=None) -> tuple[object, dict]:
    """A -> B, succeeded, then three rows written at B."""
    service = service or w.service()
    service.startup()
    report = w.prepared(service)
    req = w.apply_request(report)
    w.write_request(req)
    service.tick()
    record = w.history(req["id"])
    assert record["state"] == "succeeded", record
    w.ledger.rows += 3
    return service, req


def go_back(w: World, service, uid: str, **over) -> tuple[dict, dict | None]:
    req = {**w.go_back_request(uid), **over}
    w.write_request(req)
    service.tick()
    return req, w.history(req["id"])


def the_app(w: World) -> dict:
    found = w.by_name(w.app_name)
    assert found is not None
    return found


def creates(w: World, name: str) -> list:
    return [c for c in w.fake.calls if c.bare == "/containers/create" and c.query.get("name") == name]


def test_going_back_restores_the_backup_with_a_and_makes_a_again_from_its_saved_configuration(world):
    service, upd = updated(world)
    b_app = the_app(world)
    rows_before_update = world.ledger.backup_rows[next(iter(world.ledger.backups))]
    assert world.ledger.rows == rows_before_update + 3
    req, record = go_back(world, service, upd["id"])

    assert record["state"] == "succeeded" and record["sentence"] == f"Went back to {A}.", record
    assert (record["from_version"], record["to_version"], record["update_id"]) == (B, A, upd["id"])
    # The ledger: the update's backup, A's stamp; the three rows written at B are gone,
    # and the ledger they were in is kept aside.
    assert world.ledger.stamp == A and world.ledger.rows == rows_before_update
    assert world.ledger.aside == [B]
    # The app: A under the app's name, a new container made from the saved
    # configuration; B's container gone, and no -previous left over.
    now = world.fake.inspect_of(the_app(world))
    assert now["Config"]["Image"] == ref(APP, A) and now["State"]["Running"]
    assert now["Id"] not in (world.app_id, b_app["Id"])
    assert b_app["Id"] not in world.fake.containers and world.app_id not in world.fake.containers
    assert world.by_name(previous_name(world.app_name)) is None
    assert len(creates(world, previous_name(world.app_name))) == 1
    assert world.running_apps() == [the_app(world)]
    # Its own restart policy, not the parked `no`.
    assert now["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    # The pin names A again; the updater's line stays this updater's.
    pinned = pin.read(world.project_dir)
    assert pinned[pin.APP_KEY] == f"{APP}:{A}@{digest(APP, A)}"
    assert pinned[pin.UPDATER_KEY] == f"{UPD}:{A}@{digest(UPD, A)}"
    # The journal: every step, in order; the recovery code opens nothing now.
    j = world.journal(req["id"])
    assert j.kind == "go_back" and j.recovery_hash is None
    assert [s["step"] for s in j.started] == ["0", "G1", "G2", "G3", "R1", "R2", "R3", "R4", "G4"]
    # Nothing of the update's own was changed, and B's image is kept.
    assert world.history(upd["id"])["state"] == "succeeded"
    assert ref(APP, B) in world.fake.images
    world.no_running_app_at_a_mismatched_stamp()


def test_a_parked_container_compose_removed_does_not_matter(world):
    """C9: a later `compose up` may remove `-previous`. Going back does not need it."""
    service, upd = updated(world)
    parked = world.by_name(previous_name(world.app_name))
    del world.fake.containers[parked["Id"]]
    _, record = go_back(world, service, upd["id"])
    assert record["state"] == "succeeded", record
    assert world.version_of(the_app(world)) == A and world.ledger.stamp == A


def test_a_previous_image_recorded_by_its_platform_digest_is_verified_by_the_index_one(world):
    """Podman lists the platform manifest's digest beside the index digest a
    release attests (#287). The update recorded the one the app ran by; going
    back verifies, runs and pins by a digest that verifies."""
    service, upd = updated(world)
    platform = f"{APP}@{platform_digest(APP, A)}"
    world.fake.images[ref(APP, A)]["RepoDigests"] = [platform, ref(APP, A)]
    world.fake.images[platform] = world.fake.images[ref(APP, A)]
    path = world.volume.journal(upd["id"])
    doc = volume.read_own_json(path)
    doc["context"]["old_ref"] = platform
    volume.write_json(path, doc)
    _, record = go_back(world, service, upd["id"])
    assert record["state"] == "succeeded", record
    assert world.fake.inspect_of(the_app(world))["Config"]["Image"] == ref(APP, A)
    assert pin.read(world.project_dir)[pin.APP_KEY] == f"{APP}:{A}@{digest(APP, A)}"


@pytest.mark.parametrize("order", ["index_first", "platform_first"])
def test_going_back_on_podman_verifies_by_the_index_digest_in_either_order(tmp_path, order):
    """The update recorded the old image by whichever digest Podman listed
    first (#287); going back verifies, runs and pins by the one a release
    attests, in either order (#421)."""
    with World(tmp_path, multiarch=order) as w:
        service, upd = updated(w)
        recorded = w.journal(upd["id"]).context["old_ref"]
        assert recorded == (f"{APP}@{platform_digest(APP, A)}" if order == "platform_first" else ref(APP, A))
        _, record = go_back(w, service, upd["id"])
        assert record["state"] == "succeeded", record
        assert w.version_of(the_app(w)) == A and w.ledger.stamp == A
        assert w.fake.inspect_of(the_app(w))["Config"]["Image"] == ref(APP, A)
        assert pin.read(w.project_dir)[pin.APP_KEY] == f"{APP}:{A}@{digest(APP, A)}"


def test_going_back_twice_is_refused_one_step_only(world):
    service, upd = updated(world)
    _, record = go_back(world, service, upd["id"])
    assert record["state"] == "succeeded"
    stamp_rows = (world.ledger.stamp, world.ledger.rows)
    _, again = go_back(world, service, upd["id"], from_version=A, to_version="0.6.0")
    assert again["state"] == "refused" and again["code"] in ("from_mismatch", "not_last", "cannot_go_back"), again
    assert (world.ledger.stamp, world.ledger.rows) == stamp_rows


@pytest.mark.parametrize(
    "change,code",
    [
        (dict(protocol=1), "protocol"),
        (dict(to_version="0.9.9"), "not_older"),
        (dict(update_id="6d1b8bb0-4f6c-4a1e-9c33-2f8d7c6b5a41"), "not_last"),
        (dict(from_version=A), "from_mismatch"),
        (dict(to_version="0.6.0"), "not_last"),
        (dict(recovery_hash="scrypt$nope"), "recovery_hash"),
    ],
)
def test_a_go_back_that_is_not_one_step_back_from_the_last_update_is_refused(world, change, code):
    service, upd = updated(world)
    calls = len(world.fake.calls)
    _, record = go_back(world, service, upd["id"], **change)
    assert record["state"] == "refused" and record["code"] == code, record
    # Nothing but detection's reads on a refusal (5.4).
    assert [c for c in world.fake.calls[calls:] if c.method != "GET"] == []
    assert world.version_of(the_app(world)) == B and world.ledger.stamp == B


def test_after_a_rollback_there_is_nothing_to_go_back_from(world):
    world.drill = "migration-fails"
    service = world.service()
    service.startup()
    report = world.prepared(service)
    req = world.apply_request(report)
    world.write_request(req)
    service.tick()
    assert world.history(req["id"])["state"] == "rolled_back"
    _, record = go_back(world, service, req["id"], from_version=A, to_version="0.6.0")
    assert record["state"] == "refused" and record["code"] in ("cannot_go_back", "not_last"), record


def test_without_the_saved_configuration_going_back_is_refused(world):
    service, upd = updated(world)
    os.unlink(world.volume.root / "history" / f"{upd['id']}.previous.json")
    _, record = go_back(world, service, upd["id"])
    assert record["state"] == "refused" and record["code"] == "cannot_go_back"
    assert "configuration" in record["sentence"]
    assert world.version_of(the_app(world)) == B


@pytest.mark.parametrize("what", ["image gone", "image forged", "backup corrupt", "backup gone"])
def test_before_anything_stops_a_missing_or_doubtful_part_is_not_started(world, what):
    service, upd = updated(world)
    stamp = next(iter(world.ledger.backups))
    if what == "image gone":
        del world.fake.images[ref(APP, A)]
    elif what == "image forged":
        world.forged.add((APP, A))
    elif what == "backup corrupt":
        world.corrupt_backups.add(stamp)
    else:
        del world.ledger.backups[stamp]
    b_app = the_app(world)
    _, record = go_back(world, service, upd["id"])
    assert record["state"] == "not_started" and record["failed_step"] == "G1", record
    # B serves on, never stopped, its ledger as it was.
    assert the_app(world)["Id"] == b_app["Id"] and the_app(world)["State"] == "running"
    stops = [c for c in world.fake.calls if c.bare == f"/containers/{b_app['Id']}/stop"]
    assert stops == [] and world.ledger.stamp == B and world.ledger.aside == []


def test_a_previous_running_by_hand_is_not_stopped_and_nothing_changes(world):
    service, upd = updated(world)
    parked = world.by_name(previous_name(world.app_name))
    world.fake.set_state(world.fake.containers[parked["Id"]], "running")
    b_app = the_app(world)
    _, record = go_back(world, service, upd["id"])
    assert record["state"] == "not_started" and record["failed_step"] == "G2", record
    assert "will not stop it" in record["sentence"]
    assert the_app(world)["Id"] == b_app["Id"] and the_app(world)["State"] == "running"
    assert world.ledger.stamp == B


def test_a_stop_refused_as_state_improper_is_asked_once_more_at_g3(world):
    """#344's rule holds for going back's stop too."""
    service, upd = updated(world)
    b_id = the_app(world)["Id"]
    world.fake.improper_stops[b_id] = 1
    _, record = go_back(world, service, upd["id"])
    assert record["state"] == "succeeded", record
    stops = [c for c in world.fake.calls if c.bare == f"/containers/{b_id}/stop"]
    assert len(stops) == 2 and world.version_of(the_app(world)) == A


def test_a_restore_that_keeps_failing_needs_recovery_and_keeps_the_code(world):
    service, upd = updated(world)
    world.restore_fails = 3
    req, record = go_back(world, service, upd["id"])
    assert record["state"] == "needs_recovery", record
    assert record["sentence"].startswith(f"Going back to {A} failed:")
    j = world.journal(req["id"])
    assert j.recovery_hash is not None and j.rollback_attempts == journal.MAX_ROLLBACK_ATTEMPTS
    # Nothing serves the ledger: the recovery page runs instead, from A's image.
    assert world.running_apps() == []
    pages = [
        c for c in world.fake.containers.values() if c["Labels"].get(eng.ROLE_LABEL) == "placard"
    ]
    assert len(pages) == 1 and pages[0]["State"] == "running"
    assert volume.read_own_json(world.volume.recovery_mode)["id"] == req["id"]


def test_a_restore_that_fails_once_is_retried_and_goes_back(world):
    service, upd = updated(world)
    world.restore_fails = 1
    _, record = go_back(world, service, upd["id"])
    assert record["state"] == "succeeded", record
    assert world.ledger.stamp == A and world.version_of(the_app(world)) == A


def _writes(w: World, update_id: str, service) -> int:
    real = journal.save
    seen: list[str] = []
    journal.save = lambda vol, j: (real(vol, j), seen.append(j.step))  # type: ignore[assignment]
    try:
        go_back(w, service, update_id)
    finally:
        journal.save = real  # type: ignore[assignment]
    return len(seen)


def test_killed_after_every_journal_write_it_ends_not_started_or_gone_back(tmp_path, monkeypatch):
    """5.6 for a go-back: before R1 nothing is lost, so B is put back and it is
    *not started*; from R1 on it carries on, and ends at A."""
    with World(tmp_path / "count") as w:
        service, upd = updated(w)
        total = _writes(w, upd["id"], service)
    assert total >= 10
    seen = set()
    for n in range(1, total + 1):
        with World(tmp_path / f"run-{n}") as w:
            service, upd = updated(w)
            req = w.go_back_request(upd["id"])
            w.write_request(req)
            writes: list[str] = []
            real = journal.save

            def save(vol, j, writes=writes, real=real, n=n):
                real(vol, j)
                if j.kind == "go_back":
                    writes.append(j.step)
                    if len(writes) == n:
                        raise Killed(j.step)

            monkeypatch.setattr(journal, "save", save)
            with pytest.raises(Killed) as killed:
                service.tick()
            monkeypatch.undo()
            step = str(killed.value)
            seen.add(step)
            again = w.service()
            again.startup()
            record = w.history(req["id"])
            assert record is not None, (n, step)
            if step in ("0", "G1", "G2", "G3"):
                assert record["state"] == "not_started", (n, step, record)
                assert w.ledger.stamp == B and w.version_of(the_app(w)) == B
            else:
                assert record["state"] == "succeeded", (n, step, record)
                assert w.ledger.stamp == A and w.version_of(the_app(w)) == A
            assert len(w.running_apps()) == 1, (n, step)
            w.no_running_app_at_a_mismatched_stamp()
            assert w.journal(req["id"]).recovery_hash is None
    assert {"0", "G1", "G2", "G3", "R1", "R2", "R3", "R4", "G4"} <= seen, sorted(seen)
