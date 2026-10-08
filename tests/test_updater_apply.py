"""Prepare, apply and the automatic rollback, against a simulated installation (design notes 4.2-4.4, Part 12).

Each test runs the updater's real request path -- a request file, `Service.tick`,
intake, the orchestration, the restricted client, a unix socket -- against the
recording fake engine with the behaviour of `tests/updater_world.py`: an app
that answers health only at the stamp its code matches, a drill that backs up
and migrates, a restore that puts a backup back.

The Part 12 rows a fake engine can reach, each asserting what it did to the
ledger and to the containers, not only the state word:

  3 ghcr.io unreachable at prepare       13 no verified backup: R3
  4 an attestation refused               14 a migration fails: restored
  5 labels disagree with it               15 rows dropped: restored
  6 not enough disk or memory             16 the new app does not start
  8 a local build                         17 health fails, wrong version, no port
 11 the sidecar is not running            18 the sidecar restarts mid-update
 12 the hook fails or does not answer     19 the laptop sleeps mid-drill
                                          20 the engine restarts mid-drill
 22 restore fails three times: needs_recovery
 23 the old app does not come back: needs_recovery
 25 the parked app is out of reach of a restart by name during the drill
 32 the updater-first handover fails: the old updater runs the apply

Rows 1, 2, 9, 10 and 24 belong to the app or to intake (U1, A1-A11); 21 is U5
(`tests/test_updater_resume.py`); 26-28 and 31 are the handover's (#162);
29 and 30 are the app's; 12, the pre-update hook, is
`tests/test_updater_hook.py`'s.
"""

from __future__ import annotations

import os
import stat
import uuid
from dataclasses import replace

import pytest

from tests.self_update.compare import ALLOWED, differences
from tests.updater_world import APP, HEAD, PROJECT, REVISION, UPD, A, B, C, World, digest, ref
from updater import contract, journal, pin, volume
from updater import engine as eng
from updater.handover import NotAvailable, Outcome
from updater.journal import Owner


@pytest.fixture
def world(tmp_path):
    with World(tmp_path) as w:
        yield w


def apply(w: World, service=None, report=None, to=B, frm=A) -> tuple[dict, dict]:
    service = service or w.service()
    service.startup()
    report = report or w.prepared(service, to, frm)
    req = w.apply_request(report)
    w.write_request(req)
    service.tick()
    record = w.history(req["id"])
    assert record is not None
    return req, record


def creates(w: World, name: str | None = None) -> list:
    return [
        c
        for c in w.fake.calls
        if c.bare == "/containers/create" and (name is None or c.query.get("name") == name)
    ]


def roles_created(w: World) -> list[str]:
    return [(c.body.get("Labels") or {}).get(eng.ROLE_LABEL) for c in creates(w)]


def touched(w: World, cid: str) -> list:
    """Every mutating call naming a container."""
    return [c for c in w.fake.calls if c.method != "GET" and cid in c.bare]


def the_app(w: World) -> dict:
    found = w.by_name(w.app_name)
    assert found is not None
    return found


# --------------------------------------------------------------------------- #
# Prepare (4.4)
# --------------------------------------------------------------------------- #


def test_prepare_resolves_verifies_pulls_and_asks_the_new_image_without_touching_the_app(world):
    service = world.service()
    service.startup()
    app_before = dict(the_app(world))
    report = world.prepared(service)

    assert report["digest"] == digest(APP, B) and report["updater_digest"] == digest(UPD, B)
    assert [m["revision"] for m in report["pending"]] == ["b2c3d4e5f6a1", "c3d4e5f6a1b2"]
    assert report["database_stamp"] == HEAD[A] and report["code_head"] == HEAD[B]
    assert report["attestations"]["app"]["commit"] == REVISION[B] == report["attestations"]["updater"]["commit"]
    assert contract.parse_iso(report["expires_at"]) - world.clock.now() == pytest.approx(24 * 3600, abs=5)
    # Both images are on the engine now, by digest; A's are still there.
    assert {ref(APP, B), ref(UPD, B), ref(APP, A)} <= set(world.fake.images)
    # No downtime: the app was never stopped, renamed or replaced.
    assert touched(world, world.app_id) == []
    assert the_app(world)["Id"] == app_before["Id"] and the_app(world)["State"] == "running"
    # The two one-offs it ran are gone, and ran with no network against the ledger.
    assert roles_created(world) == ["measure", "check"]
    assert all(c.body["HostConfig"]["NetworkMode"] == "none" for c in creates(world))
    assert not any(eng.ROLE_LABEL in c["Labels"] for c in world.fake.containers.values())
    assert world.ledger.stamp == A and world.ledger.drills == 0


def _refused_prepare(w: World, to: str = B) -> dict:
    service = w.service()
    service.startup()
    req = w.prepare_request(to)
    w.write_request(req)
    service.tick()
    record = w.history(req["id"])
    assert record is not None and record["state"] == "refused", record
    assert volume.read_own_json(w.volume.prepared(req["id"])) is None
    return record


def pulls(w: World) -> list:
    return [c for c in w.fake.calls if c.bare == "/images/create"]


def test_row_3_ghcr_unreachable_refuses_prepare_and_pulls_nothing(world):
    world.offline = True
    record = _refused_prepare(world)
    assert record["sentence"] == "Preparing failed: ghcr.io could not be reached."
    assert pulls(world) == [] and ref(APP, B) not in world.fake.images


@pytest.mark.parametrize("repo", [APP, UPD])
def test_row_4_an_attestation_refused_for_either_image_refuses_before_any_pull(world, repo):
    world.forged.add((repo, B))
    record = _refused_prepare(world)
    assert record["sentence"].startswith(
        "Preparing failed: the image's origin could not be proven: identity"
    )
    assert pulls(world) == []
    assert ref(APP, B) not in world.fake.images and ref(UPD, B) not in world.fake.images


def test_row_5_labels_that_disagree_with_the_attestation_delete_what_was_pulled(world):
    world.relabelled.add((UPD, B))
    record = _refused_prepare(world)
    assert record["sentence"] == "Preparing failed: the downloaded image is not the one that was verified."
    assert len(pulls(world)) >= 1
    assert ref(APP, B) not in world.fake.images and ref(UPD, B) not in world.fake.images
    assert ref(APP, A) in world.fake.images


def test_row_6_short_disk_names_the_need_and_the_remedy_and_pulls_nothing(tmp_path):
    with World(tmp_path, engine="docker-desktop", fixture="docker-desktop") as w:
        w.measured = {**w.measured, "free": 300 * 1024**2}
        record = _refused_prepare(w)
        assert record["sentence"].startswith("Needs about 1.2 GB free, there is 0.3 GB.")
        assert "Docker Desktop's disk limit in Settings → Resources" in record["sentence"]
        assert pulls(w) == []


def test_row_6_short_memory_points_at_the_podman_machine(tmp_path):
    with World(tmp_path, engine="podman-machine", fixture="podman-machine") as w:
        w.measured = {**w.measured, "mem_available": 100 * 1024**2}
        record = _refused_prepare(w)
        assert record["sentence"].startswith("Needs about 256 MB of memory free, there is 100 MB.")
        assert "podman machine set --memory" in record["sentence"]
        assert pulls(w) == []


def test_a_release_that_is_not_published_is_refused(world):
    world.published.discard(C)
    record = _refused_prepare(world, to=C)
    assert "could not be found" in record["sentence"] and pulls(world) == []


def test_row_8_a_local_build_is_refused_with_no_engine_call_after_detection(world):
    app = world.fake.inspect_of(the_app(world))
    image = world.fake.image_by_id(app["Image"])
    image["RepoDigests"] = []
    app["Config"]["Image"] = "household-spend-tracker:dev"
    service = world.service()
    service.startup()
    req = world.prepare_request()
    world.write_request(req)
    calls = len(world.fake.calls)
    service.tick()
    record = world.history(req["id"])
    assert record["state"] == "refused" and "locally built image" in record["sentence"]
    assert not any(c.method != "GET" for c in world.fake.calls[calls:])


def test_an_app_in_a_podman_pod_is_refused_with_its_sentence(world):
    app = world.fake.inspect_of(the_app(world))
    app["Pod"] = "f" * 64
    record = _refused_prepare(world)
    assert "Podman pod" in record["sentence"] and "in_pod: false" in record["sentence"]


def test_discard_deletes_the_report(world):
    service = world.service()
    service.startup()
    report = world.prepared(service)
    req = {**world.base("discard"), "prepared_id": report["id"]}
    world.write_request(req)
    service.tick()
    assert world.history(req["id"])["state"] == "succeeded"
    assert not world.volume.prepared(report["id"]).exists()


# --------------------------------------------------------------------------- #
# Apply (4.2): the whole of it, then twice
# --------------------------------------------------------------------------- #


def test_apply_moves_the_ledger_and_the_app_to_b_and_records_it(world):
    before = world.fake.inspect_of(the_app(world))
    old_image = world.fake.images[ref(APP, A)]["Config"]
    req, record = apply(world)

    assert record["state"] == "succeeded" and record["sentence"] == "Updated to 0.8.0."
    # The ledger: migrated once, backed up first, at B's schema.
    assert world.ledger.stamp == B and world.ledger.drills == 1
    assert list(world.ledger.backups.values()) == [A]
    assert record["backup"] == f"/var/lib/spend-tracker/backups/{next(iter(world.ledger.backups))}"
    # The app: B runs under the app's name; A is parked, stopped, as -previous.
    new = world.fake.inspect_of(the_app(world))
    assert new["Config"]["Image"] == ref(APP, B) and new["State"]["Running"]
    parked = world.by_name(f"{world.app_name}-previous")
    assert parked["Id"] == before["Id"] and parked["State"] == "exited"
    assert world.running_apps() == [the_app(world)]
    # The copy differs only in the image and AUTO_MIGRATE (C10).
    diff = differences(before, new, old_image, world.fake.images[ref(APP, B)]["Config"])
    assert set(diff) == ALLOWED
    # No one-off is left behind, and the maintenance page came and went.
    assert not any(eng.ROLE_LABEL in c["Labels"] for c in world.fake.containers.values())
    assert roles_created(world).count("placard") == 1
    # The pin: B in .env, every other line kept, the mode kept; the record beside it.
    env = (world.project_dir / ".env").read_text().splitlines()
    assert "TS_AUTHKEY=tskey-auth-placeholder" in env and "SPENDTRACKER_VERSION=0.7.1" in env
    assert f"SPENDTRACKER_IMAGE={APP}:{B}@{digest(APP, B)}" in env
    assert f"SPENDTRACKER_UPDATER_IMAGE={UPD}:{A}@{digest(UPD, A)}" in env
    assert stat.S_IMODE(os.stat(world.project_dir / ".env").st_mode) == 0o600
    assert pin.read(world.project_dir)[pin.APP_KEY] == f"{APP}:{B}@{digest(APP, B)}"
    assert (world.project_dir / "pin" / "release.env").read_text().startswith("# Written by the updater")
    # C9: the previous container's configuration is kept beside the record.
    saved = volume.read_own_json(world.volume.root / "history" / f"{req['id']}.previous.json")
    assert saved["Id"] == before["Id"] and saved["Config"]["Image"] == f"{APP}:{A}"
    # The recovery code opens nothing any more; the journal says every step started, in order.
    j = world.journal(req["id"])
    assert j.recovery_hash is None
    steps = [s["step"] for s in j.started]
    assert steps == ["0", "1", "2a", "3", "4", "5", "6", "7", "8", "9", "10"]
    world.no_running_app_at_a_mismatched_stamp()


def test_two_updates_in_a_row_replace_the_parked_container_and_its_image(world):
    service = world.service()
    first, _ = apply(world, service)
    parked_a = world.by_name(f"{world.app_name}-previous")["Id"]
    second, record = apply(world, service, to=C, frm=B)
    assert record["state"] == "succeeded" and world.ledger.stamp == C
    parked = world.by_name(f"{world.app_name}-previous")
    # The parked A of the first update is gone; the parked one is now B.
    assert parked_a not in world.fake.containers
    assert world.version_of(parked) == B
    assert ref(APP, A) not in world.fake.images and ref(APP, B) in world.fake.images
    assert world.ledger.drills == 2 and len(world.ledger.backups) == 2


def earlier_updates(w: World, stamps: list[str]) -> None:
    """A history record per earlier update, each naming its backup (R22), and the folders."""
    for stamp in stamps:
        w.ledger.backups[stamp] = A
        old = str(uuid.uuid4())
        record = contract.History(
            id=old,
            kind="apply",
            state="succeeded",
            sentence="Updated.",
            finished_at="2026-10-01T00:00:00Z",
            backup=f"/var/lib/spend-tracker/backups/{stamp}",
        )
        volume.write_json(w.volume.history(old), record.to_dict())


def test_update_backups_beyond_the_newest_five_are_pruned_after_success_only(world):
    stamps = [f"20261001-00000{i}" for i in range(6)]
    earlier_updates(world, stamps)
    # A backup the owner made by hand is not an update backup, and is never pruned.
    world.ledger.backups["20260901-000000"] = A
    _, record = apply(world)
    assert record["state"] == "succeeded"
    # Seven update backups now (six earlier, this one): the newest five stay.
    kept = sorted(world.ledger.backups)
    assert kept == ["20260901-000000", *stamps[2:], record["backup"].rsplit("/", 1)[1]]
    assert stamps[0] not in world.ledger.backups and stamps[1] not in world.ledger.backups


def test_a_rollback_prunes_nothing(world):
    world.drill = "migration-fails"
    earlier_updates(world, [f"20261001-00000{i}" for i in range(6)])
    _, record = apply(world)
    assert record["state"] == "rolled_back" and len(world.ledger.backups) == 7
    assert "prune" not in roles_created(world)


# --------------------------------------------------------------------------- #
# Not started (rows 11, 12, 6 at preflight)
# --------------------------------------------------------------------------- #


def test_row_11_the_sidecar_not_running_is_not_started_and_nothing_moved(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        service = w.service()
        service.startup()
        report = w.prepared(service)
        sidecar = w.fake.containers[w.sidecar_id]
        w.fake.set_state(sidecar, "exited")
        req, record = apply(w, service, report)
        assert (
            record["state"] == "not_started"
            and record["sentence"] == "Not started: The Tailscale sidecar is not running."
        )
        assert touched(w, w.app_id) == [] and touched(w, w.sidecar_id) == []
        assert the_app(w)["Id"] == w.app_id and w.ledger.stamp == A and w.ledger.drills == 0
        assert world_journal_steps(w, req) == ["0", "1"]


def world_journal_steps(w: World, req: dict) -> list[str]:
    return [s["step"] for s in w.journal(req["id"]).started]


def test_row_6_at_preflight_short_memory_is_not_started(world):
    service = world.service()
    service.startup()
    report = world.prepared(service)
    world.measured = {**world.measured, "mem_available": 512 * 1024**2}
    _, record = apply(world, service, report)
    # 768 MiB of the app's mem_limit, plus 128.
    assert (
        record["state"] == "not_started"
        and "Needs about 896 MB of memory free, there is 512 MB" in record["sentence"]
    )
    assert touched(world, world.app_id) == []


def test_a_setting_the_updater_will_not_copy_is_refused_before_the_app_stops(world):
    app = world.fake.inspect_of(the_app(world))
    app["HostConfig"]["Binds"].append("/srv/secrets:/secrets:ro")
    service = world.service()
    service.startup()
    report = world.prepared(service)
    _, record = apply(world, service, report)
    assert record["state"] == "not_started" and "will not copy" in record["sentence"]
    assert touched(world, world.app_id) == [] and world.ledger.drills == 0


# --------------------------------------------------------------------------- #
# Rolled back (rows 13-18, 20)
# --------------------------------------------------------------------------- #


def test_row_13_no_verified_backup_goes_straight_to_r3_and_restores_nothing(world):
    world.drill = "no-backup"
    req, record = apply(world)
    assert record["state"] == "rolled_back"
    assert (
        record["sentence"]
        == "Rolled back to 0.7.1: the backup could not be taken or verified; nothing was migrated."
    )
    assert world.ledger.stamp == A and world.ledger.aside == []
    assert "restore" not in roles_created(world)
    assert the_app(world)["Id"] == world.app_id and the_app(world)["State"] == "running"
    steps = world_journal_steps(world, req)
    assert steps[-3:] == ["R3", "R4", "R5"] and "R1" not in steps and "R2" not in steps
    world.no_running_app_at_a_mismatched_stamp()


@pytest.mark.parametrize(
    "mode,phrase",
    [
        ("migration-fails", "it failed while migrating"),
        ("rows-dropped", "the result did not verify: a table lost rows"),
    ],
)
def test_rows_14_and_15_restore_the_backup_with_the_old_image(world, mode, phrase):
    world.drill = mode
    req, record = apply(world)
    assert (
        record["state"] == "rolled_back"
        and phrase in record["sentence"]
        and "the ledger was restored" in record["sentence"]
    )
    # The migrated database moved aside, the backup's put back: A's stamp, A's rows.
    assert world.ledger.stamp == A and world.ledger.aside == [
        "half-migrated" if mode == "migration-fails" else B
    ]
    restore = next(c for c in creates(world) if c.body["Labels"].get(eng.ROLE_LABEL) == "restore")
    assert restore.body["Image"] == ref(APP, A) and restore.body["HostConfig"]["NetworkMode"] == "none"
    assert the_app(world)["Id"] == world.app_id and the_app(world)["State"] == "running"
    assert world.by_name(f"{world.app_name}-previous") is None
    assert world_journal_steps(world, req)[-5:] == ["R1", "R2", "R3", "R4", "R5"]
    assert world.journal(req["id"]).recovery_hash is None
    world.no_running_app_at_a_mismatched_stamp()


def test_row_16_a_new_app_the_engine_will_not_create_is_rolled_back(world):
    world.fake.refuse_create = lambda name, body: (
        "no space left on device" if name == world.app_name else None
    )
    _, record = apply(world)
    assert record["state"] == "rolled_back" and "the new version did not start" in record["sentence"]
    assert record["failed_step"] == "7"
    assert world.ledger.stamp == A and the_app(world)["Id"] == world.app_id
    world.no_running_app_at_a_mismatched_stamp()


def test_row_17_a_new_version_that_does_not_answer_is_removed_and_restored(world):
    world.broken.add(B)
    req, record = apply(world)
    assert record["state"] == "rolled_back" and "the new version did not answer" in record["sentence"]
    assert record["failed_step"] == "8"
    # The B container is gone, A is back under the name, at A's stamp.
    assert [world.version_of(c) for c in world.apps()] == [A]
    assert world.ledger.stamp == A
    world.no_running_app_at_a_mismatched_stamp()


def test_row_17_a_missing_port_binding_found_by_inspection_is_a_failed_health_check(world):
    original = world.fake._ports

    def no_ports_for_b(c):
        original(c)
        seen = c.get("_inspect") or {}
        if (seen.get("Config") or {}).get("Image") == ref(APP, B):
            seen["NetworkSettings"]["Ports"] = {}

    world.fake._ports = no_ports_for_b
    _, record = apply(world)
    assert record["state"] == "rolled_back" and record["failed_step"] == "8"
    assert world.ledger.stamp == A


def test_row_17_on_docker_desktop_the_port_is_probed_through_the_gateway_with_a_localhost_host(tmp_path):
    with World(tmp_path, engine="docker-desktop", fixture="docker-desktop") as w:
        _, record = apply(w)
        assert record["state"] == "succeeded"
        probe = next(c for c in creates(w) if c.body["Labels"].get(eng.ROLE_LABEL) == "probe")
        assert probe.body["Cmd"][-2:] == ["http://host.docker.internal:8848/api/health", "localhost:8848"]
        assert probe.body["Image"] == ref(APP, B) and probe.body["HostConfig"]["NetworkMode"] == "bridge"


def test_row_18_the_sidecar_restarting_mid_update_gets_one_extra_recreate(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        sidecar_before = dict(w.fake.inspect_of(w.fake.containers[w.sidecar_id])["State"])
        restarted = []

        def strand_the_first_b(c):
            if w.version_of(c) == B and not restarted:
                restarted.append(c["Id"])
                w.restart_sidecar()

        w.on_app_start = strand_the_first_b
        req, record = apply(w)
        assert record["state"] == "succeeded", record
        assert len(creates(w, w.app_name)) == 2 and restarted[0] not in w.fake.containers
        assert w.journal(req["id"]).context["sidecar_recreated"] is True
        # The new app joined the sidecar as it is now; the updater never touched the sidecar.
        assert w.fake.inspect_of(the_app(w))["HostConfig"]["NetworkMode"] == f"container:{w.sidecar_id}"
        assert [c for c in touched(w, w.sidecar_id) if not c.bare.endswith("/exec")] == []
        assert (
            w.fake.inspect_of(w.fake.containers[w.sidecar_id])["State"]["StartedAt"]
            != sidecar_before["StartedAt"]
        )


def test_row_18_a_second_sidecar_restart_is_a_failed_health_check(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        w.on_app_start = lambda c: w.restart_sidecar() if w.version_of(c) == B else None
        _, record = apply(w)
        assert record["state"] == "rolled_back" and len(creates(w, w.app_name)) == 2
        assert w.ledger.stamp == A


def test_row_20_a_drill_lost_to_an_engine_restart_is_never_run_twice(world):
    """The drill backed up and migrated, then the engine restarted and took the one-off with it."""
    world.drill = "vanish"
    req, record = apply(world)
    assert world.ledger.drills == 1
    # No report, so the backup folder it left decided: R1, restored.
    assert "find-backup" in roles_created(world)
    assert record["state"] == "rolled_back" and world.ledger.stamp == A and world.ledger.aside == [B]
    assert roles_created(world).count("drill") == 1
    world.no_running_app_at_a_mismatched_stamp()


def test_row_20_the_engine_going_away_mid_drill_resumes_from_the_journal(world):
    service = world.service()
    service.startup()
    report = world.prepared(service)

    def engine_goes(c):
        world.drill = "hang"
        world.time.on_sleep = lambda: setattr(world.fake, "gone", True)

    world.on_drill = engine_goes
    req = world.apply_request(report)
    world.write_request(req)
    service.tick()
    assert world.history(req["id"]) is None and service.resume_pending
    assert world.journal(req["id"]).step == "5"
    # The engine comes back without the one-off (it had no restart policy).
    world.time.on_sleep = None
    world.fake.gone = False
    drill = next(c for c in world.fake.containers.values() if c["Labels"].get(eng.ROLE_LABEL) == "drill")
    del world.fake.containers[drill["Id"]]
    world.on_drill = None
    service.tick()
    record = world.history(req["id"])
    assert record["state"] == "rolled_back" and world.ledger.drills == 1
    assert world.ledger.stamp == A
    world.no_running_app_at_a_mismatched_stamp()


def test_row_19_a_laptop_asleep_mid_drill_does_not_expire_it(world):
    """U7 through the orchestration: an hour asleep inside a 30-minute drill."""
    service = world.service()
    service.startup()
    report = world.prepared(service)
    world.drill = "hang"
    polls = []

    def sleep_then_finish():
        polls.append(1)
        if len(polls) == 3:
            world.time.doze(3600)
        if len(polls) == 6:
            world.finish_hung_drill()

    world.time.on_sleep = sleep_then_finish
    req = world.apply_request(report)
    world.write_request(req)
    service.tick()
    world.time.on_sleep = None
    record = world.history(req["id"])
    assert record["state"] == "succeeded" and world.ledger.stamp == B
    assert record["gap_s"] >= 3600


def test_a_drill_past_its_deadline_is_stopped_and_rolled_back(world):
    world.drill = "hang"
    req, record = apply(world)
    assert record["state"] == "rolled_back"
    assert not any(c["Labels"].get(eng.ROLE_LABEL) == "drill" for c in world.fake.containers.values())
    assert world.ledger.stamp == A


# --------------------------------------------------------------------------- #
# needs_recovery (rows 22, 23)
# --------------------------------------------------------------------------- #


def test_row_22_restore_failing_three_times_needs_recovery_and_nothing_serves(world):
    world.drill = "migration-fails"
    world.restore_fails = 3
    req, record = apply(world)
    assert record["state"] == "needs_recovery" and "recovery code" in record["sentence"]
    j = world.journal(req["id"])
    assert j.rollback_attempts == 3 and j.recovery_hash is not None
    assert world.running_apps() == []
    # Both files: the migrated database where it was, and the verified backup.
    assert world.ledger.stamp == "half-migrated" and world.ledger.backups
    placards = [c for c in creates(world) if c.body["Labels"].get(eng.ROLE_LABEL) == "placard"]
    assert placards[-1].body["Cmd"] == ["-m", "scripts.placard", "--recovery"]
    assert roles_created(world).count("restore") == 3


def test_a_restore_that_fails_twice_succeeds_on_the_third_attempt(world):
    world.drill = "migration-fails"
    world.restore_fails = 2
    req, record = apply(world)
    assert record["state"] == "rolled_back" and world.ledger.stamp == A
    assert world.journal(req["id"]).rollback_attempts == 3


def test_row_23_an_old_app_that_does_not_come_back_needs_recovery(world):
    world.drill = "migration-fails"
    world.broken.add(A)
    req, record = apply(world)
    assert record["state"] == "needs_recovery"
    assert world.ledger.stamp == A  # restored, at the old stamp
    assert world.running_apps() == []


# --------------------------------------------------------------------------- #
# Row 25, the handover points (2a, 10)
# --------------------------------------------------------------------------- #


def test_row_25_during_the_drill_nothing_answers_to_the_app_name(world):
    seen = {}

    def look(c):
        seen["by_name"] = world.by_name(world.app_name)
        seen["parked"] = world.by_name(f"{world.app_name}-previous")

    world.on_drill = look
    _, record = apply(world)
    assert record["state"] == "succeeded"
    assert seen["by_name"] is None and seen["parked"]["State"] == "exited"


class Taker(NotAvailable):
    """A handover that works: the successor takes the request at 2a, writing
    itself in as the journal's owner as a real one does at H5."""

    def __init__(self, vol, clock):
        self.vol, self.clock = vol, clock

    def first(self, *, request_id, me, successor):
        owner = replace(successor, container=f"{PROJECT}-updater-1-next")
        j = journal.load(self.vol, request_id)
        journal.hand_over(self.vol, j, owner, self.clock.now())
        journal.remember(self.vol, j, first_handover="done")
        return Outcome(True, "handed over", owner=owner)

    def after(self, *, request_id, me, successor, before_go=None):  # pragma: no cover - not reached
        raise AssertionError


def test_updater_first_hands_over_before_the_app_stops(tmp_path):
    with World(tmp_path, updater_protocols="1-2") as w:
        req, _ = None, None
        service = w.service(handover=Taker(w.volume, w.clock))
        service.startup()
        report = w.prepared(service)
        req = w.apply_request(report)
        w.write_request(req)
        service.tick()
        j = w.journal(req["id"])
        assert [s["step"] for s in j.started] == ["0", "1", "2a"]
        assert j.owner.container == f"{PROJECT}-updater-1-next" and j.owner.version == B
        assert touched(w, w.app_id) == [] and w.ledger.drills == 0
        assert w.history(req["id"]) is None


def test_row_32_a_failed_updater_first_handover_leaves_the_apply_to_the_old_updater(world):
    req, record = apply(world)
    steps = world_journal_steps(world, req)
    assert steps.index("2a") < steps.index("3")
    assert record["state"] == "succeeded" and world.ledger.stamp == B
    assert any("did not take over first" in n for n in record["notes"])


def test_no_updater_first_when_the_successor_does_not_speak_the_apps_protocol(tmp_path):
    with World(tmp_path, updater_protocols="2-3") as w:
        req, record = apply(w)
        assert "2a" not in world_journal_steps(w, req) and record["state"] == "succeeded"


def test_step_10_is_skipped_when_the_running_updater_is_newer(tmp_path):
    with World(tmp_path) as w:
        newer = Owner(image_digest=digest(UPD, C), version=C, container=f"{PROJECT}-updater-1")
        req, record = apply(w, w.service(me=newer))
        steps = world_journal_steps(w, req)
        assert "10" not in steps and "2a" not in steps
        assert (
            record["state"] == "succeeded"
            and "The updater stays on 0.9.0, which is newer." in record["notes"]
        )
        assert pin.read(w.project_dir)[pin.UPDATER_KEY] == f"{UPD}:{C}@{digest(UPD, C)}"


# --------------------------------------------------------------------------- #
# E7 (#169): the parked app started by hand
# --------------------------------------------------------------------------- #


def policy_of(w: World, cid: str) -> dict:
    return w.fake.inspect_of(w.fake.containers[cid])["HostConfig"]["RestartPolicy"]


def updates(w: World) -> list:
    return [c for c in w.fake.calls if c.bare.endswith("/update")]


def started_by_hand_during_the_drill(w: World) -> None:
    """Docker Desktop's Start button on `-previous`, while the drill runs: the
    engine starts it whatever its restart policy, and it holds its port."""

    def start_it(_drill):
        w.fake.set_state(w.fake.containers[w.app_id], "running")

    w.on_drill = start_it


def test_e7_the_parked_app_is_parked_with_restart_policy_no_and_the_journal_keeps_its_own(world):
    req, record = apply(world)
    assert record["state"] == "succeeded"
    parked = world.by_name(f"{world.app_name}-previous")
    assert parked["Id"] == world.app_id and parked["State"] == "exited"
    assert policy_of(world, world.app_id) == {"Name": "no"}
    assert [c.body for c in updates(world)] == [{"RestartPolicy": {"Name": "no"}}]
    ctx = world.journal(req["id"]).context
    assert ctx["previous_restart_policy"] == {"Name": "unless-stopped"}
    assert ctx["restart_policy_parked"] is True


def test_e7_a_previous_started_by_hand_holding_the_port_is_stopped_once_and_b_starts(world):
    started_by_hand_during_the_drill(world)
    req, record = apply(world)
    assert record["state"] == "succeeded", record
    # E1 holds: B runs under the name, A is parked, stopped, with policy no.
    new = world.fake.inspect_of(the_app(world))
    assert new["Config"]["Image"] == ref(APP, B) and new["State"]["Running"]
    assert world.ledger.stamp == B and world.running_apps() == [the_app(world)]
    assert world.fake.containers[world.app_id]["State"] == "exited"
    assert policy_of(world, world.app_id) == {"Name": "no"}
    # Once: the first start was refused for the port, the second went through.
    starts = [c for c in world.fake.calls if c.bare == f"/containers/{new['Id']}/start"]
    assert len(starts) == 2 and len(creates(world, world.app_name)) == 1
    assert any("started while the update ran" in n for n in record["notes"])
    assert world.journal(req["id"]).context["previous_stopped_for_new"] is True
    world.no_running_app_at_a_mismatched_stamp()


def test_e7_the_retry_is_bounded_then_rolled_back_with_the_restart_policy_put_back(world):
    started_by_hand_during_the_drill(world)
    # The port stays taken for B's app whatever is stopped: something else holds it.
    refused = []

    def taken(c):
        if world.version_of(c) == B and eng.ROLE_LABEL not in c["Labels"]:
            refused.append(c["Id"])
            return "127.0.0.1:8848"
        return None

    world.fake._port_taken = taken
    req, record = apply(world)
    assert record["state"] == "rolled_back" and record["failed_step"] == "7"
    assert "the new version did not start" in record["sentence"]
    # Two starts of the one B container, then the rollback: no third.
    assert len(refused) == 2 and len(set(refused)) == 1 and len(creates(world, world.app_name)) == 1
    # A is home, running, with its own restart policy back.
    assert the_app(world)["Id"] == world.app_id and the_app(world)["State"] == "running"
    assert policy_of(world, world.app_id) == {"Name": "unless-stopped"}
    assert [c.body["RestartPolicy"]["Name"] for c in updates(world)] == ["no", "unless-stopped"]
    assert world.journal(req["id"]).context["restart_policy_parked"] is False


def test_e7_any_rollback_puts_the_previous_restart_policy_back(world):
    world.broken.add(B)
    _, record = apply(world)
    assert record["state"] == "rolled_back"
    assert the_app(world)["Id"] == world.app_id
    assert policy_of(world, world.app_id) == {"Name": "unless-stopped"}


def test_e7_in_the_sidecar_layout_a_previous_answering_in_the_namespace_is_stopped_and_b_recreated(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        started_by_hand_during_the_drill(w)
        req, record = apply(w)
        assert record["state"] == "succeeded", record
        assert w.version_of(the_app(w)) == B and w.running_apps() == [the_app(w)]
        assert w.fake.containers[w.app_id]["State"] == "exited"
        assert policy_of(w, w.app_id) == {"Name": "no"}
        assert len(creates(w, w.app_name)) == 2
        assert w.journal(req["id"]).context["previous_stopped_for_new"] is True


def test_e7_an_engine_that_cannot_change_a_restart_policy_is_noted_and_the_update_goes_on(world):
    world.fake.no_update = True
    started_by_hand_during_the_drill(world)
    req, record = apply(world)
    assert record["state"] == "succeeded"
    assert any("could not be set to `no`" in n for n in record["notes"])
    assert "restart_policy_parked" not in world.journal(req["id"]).context
    assert world.fake.containers[world.app_id]["State"] == "exited"
