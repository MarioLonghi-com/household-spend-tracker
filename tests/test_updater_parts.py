"""The orchestration's smaller parts: the pin, health's judgement, the handover's
decisions, one-offs, the floors, and the engine client's two additions (R27, logs).
"""

from __future__ import annotations

import argparse
import json
import os
import stat

import pytest

from tests.updater_fake_engine import FakeEngine, Running, engine_fixture, frame
from tests.updater_world import APP, PROJECT, UPD, A, B, C, World, digest, ref
from updater import __main__ as main
from updater import engine as eng
from updater import health, oneoff, pin, prepare, shapes, trust
from updater.clock import Deadline, GapClock
from updater.engine import EngineClient, NotAllowed, Scope
from updater.handover import PROTOCOLS_LABEL, NotAvailable, goes_first, protocol_window, stays_newer
from updater.journal import Owner

# --------------------------------------------------------------------------- #
# The pin (9.1, S1)
# --------------------------------------------------------------------------- #

APP_A = pin.image_ref(eng.REPOSITORIES[0], A, "sha256:" + "a" * 64)
APP_B = pin.image_ref(eng.REPOSITORIES[0], B, "sha256:" + "b" * 64)
UPD_B = pin.image_ref(eng.REPOSITORIES[1], B, "sha256:" + "c" * 64)


def test_the_pin_replaces_only_its_two_keys_in_env_and_keeps_its_mode(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# my settings\nTS_AUTHKEY=tskey-auth-placeholder\nSPENDTRACKER_IMAGE=old\nSPENDTRACKER_VERSION=0.7.1\n"
    )
    os.chmod(env, 0o640)
    assert pin.write(tmp_path, APP_B, UPD_B) == {pin.APP_KEY: APP_B, pin.UPDATER_KEY: UPD_B}
    lines = env.read_text().splitlines()
    assert lines == [
        "# my settings",
        "TS_AUTHKEY=tskey-auth-placeholder",
        f"SPENDTRACKER_IMAGE={APP_B}",
        "SPENDTRACKER_VERSION=0.7.1",
        f"SPENDTRACKER_UPDATER_IMAGE={UPD_B}",
    ]
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o640
    record = (tmp_path / "pin" / "release.env").read_text()
    assert record == f"{pin.RECORD_HEADER}SPENDTRACKER_IMAGE={APP_B}\nSPENDTRACKER_UPDATER_IMAGE={UPD_B}\n"
    # Nothing left behind: no temporary file beside either.
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", "pin"]


def test_writing_the_same_pin_twice_leaves_the_same_files_and_a_new_pin_replaces_the_old(tmp_path):
    pin.write(tmp_path, APP_A, None)
    first = (tmp_path / ".env").read_text()
    pin.write(tmp_path, APP_A, None)
    assert (tmp_path / ".env").read_text() == first == f"SPENDTRACKER_IMAGE={APP_A}\n"
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600
    pin.write(tmp_path, APP_B, None)
    assert pin.read(tmp_path) == {pin.APP_KEY: APP_B}


def test_a_duplicated_or_exported_key_is_left_once(tmp_path):
    (tmp_path / ".env").write_text(
        "export SPENDTRACKER_IMAGE=a\nSPENDTRACKER_IMAGE=b\n#SPENDTRACKER_IMAGE=c\n"
    )
    pin.write(tmp_path, APP_B, None)
    assert (tmp_path / ".env").read_text() == f"SPENDTRACKER_IMAGE={APP_B}\n#SPENDTRACKER_IMAGE=c\n"


# --------------------------------------------------------------------------- #
# Health's judgement (8.4)
# --------------------------------------------------------------------------- #

REV = "2b3c4d5e6f7a" + "1" * 28
OK = json.dumps({"status": "ok", "version": B, "commit": REV[:7]})


@pytest.mark.parametrize(
    "body,problem",
    [
        (OK, None),
        ("<html>", "it did not answer with JSON"),
        (json.dumps({"status": "starting"}), "it did not say ok"),
        (
            json.dumps({"status": "ok", "version": A, "commit": REV[:7]}),
            "it answered as version 0.7.1, not 0.8.0",
        ),
        (
            json.dumps({"status": "ok", "version": B, "commit": "1a2b3c4"}),
            "it runs commit 1a2b3c4, not 2b3c4d5",
        ),
        (json.dumps({"status": "ok", "version": B, "commit": None}), "it did not say which commit it runs"),
    ],
)
def test_a_200_alone_is_not_health(body, problem):
    assert health.judge(body, health.Expect(B, REV)) == problem


def test_a_binding_is_checked_by_inspection_where_no_gateway_reaches_it():
    previous = {"HostConfig": {"PortBindings": {"8848/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8848"}]}}}
    same = {"NetworkSettings": {"Ports": {"8848/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8848"}]}}}
    moved = {"NetworkSettings": {"Ports": {"8848/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8848"}]}}}
    gone = {"NetworkSettings": {"Ports": {}}}
    assert health.binding_problem(previous, same) is None
    assert health.binding_problem(previous, moved) == "port 8848/tcp is not published on 127.0.0.1:8848"
    assert health.binding_problem(previous, gone) == "port 8848/tcp is not published on 127.0.0.1:8848"


# --------------------------------------------------------------------------- #
# The handover's decisions (6.6, C1, C4)
# --------------------------------------------------------------------------- #

ME = Owner(image_digest=digest(UPD, A), version=A, container="u")


def test_the_successor_goes_first_only_when_newer_and_speaking_the_apps_protocol():
    successor = Owner(image_digest=digest(UPD, B), version=B, container="")
    older = Owner(image_digest="x", version="0.7.0", container="")
    assert goes_first(ME, successor, (1, 2), 1) is True
    assert goes_first(ME, successor, (2, 3), 1) is False
    assert goes_first(ME, successor, None, 1) is False
    assert goes_first(ME, older, (1, 2), 1) is False
    assert protocol_window({"x": "1"}) is None
    assert protocol_window({PROTOCOLS_LABEL: "1-3"}) == (1, 3)
    assert protocol_window({PROTOCOLS_LABEL: "3-1"}) is None


def test_never_downgrade_an_updater():
    assert stays_newer(Owner("d", C, "u"), B) is True
    assert stays_newer(Owner("d", B, "u"), B) is False
    assert stays_newer(Owner("d", A, "u"), B) is False


def test_the_handover_not_yet_built_says_so_and_touches_nothing():
    outcome = NotAvailable().first(request_id="r", me=ME, successor=ME)
    assert outcome.done is False and outcome.owner is None and "not available" in outcome.sentence
    assert NotAvailable().after(request_id="r", me=ME, successor=ME).done is False


# --------------------------------------------------------------------------- #
# The floors (8.5, C14)
# --------------------------------------------------------------------------- #

MIB = 1024 * 1024


def test_the_disk_floor_counts_unpacked_images_twice_the_database_and_half_a_gigabyte():
    measured = {"free": 2000 * MIB, "mem_available": 4000 * MIB, "database": 100 * MIB}
    # 400 MiB compressed -> 1200 unpacked + 200 + 512 = 1912 MiB: enough.
    assert (
        prepare.floor_problem(measured, engine="docker-engine", new_image_bytes=400 * MIB, memory_needed=0)
        is None
    )
    short = prepare.floor_problem(
        measured, engine="docker-engine", new_image_bytes=500 * MIB, memory_needed=0
    )
    assert short.startswith("Needs about 2.2 GB free, there is 2.0 GB.")


def test_with_no_mem_limit_the_app_counts_as_768_mib():
    assert prepare.app_memory({"HostConfig": {"Memory": 0}}) == 768 * MIB
    assert prepare.app_memory({"HostConfig": {"Memory": 512 * MIB}}) == 512 * MIB
    assert prepare.app_memory({}) == 768 * MIB


def test_the_architecture_comes_from_the_engines_machine():
    assert trust.architecture({"Architecture": "aarch64"}) == "arm64"
    assert trust.architecture({"Architecture": "x86_64"}) == "amd64"
    assert trust.architecture({}) == "amd64"


# --------------------------------------------------------------------------- #
# The engine client's additions: R27 and logs
# --------------------------------------------------------------------------- #


@pytest.fixture
def engine_world():
    fake = FakeEngine(engine_fixture("docker-29.0"))
    ours = fake.add_image(ref(eng.REPOSITORIES[0], A))
    theirs = fake.add_image("docker.io/library/postgres@sha256:" + "9" * 64)
    fake.add_container("spend-tracker-app-1", PROJECT, ImageID=ours)
    fake.add_container("other-db-1", "other", ImageID=theirs)
    one = fake.add_container(
        "spend-tracker-app-1-check-5d3c0b8e", PROJECT,
        labels={eng.ONEOFF_LABEL: "True", eng.ROLE_LABEL: "check"},
    )  # fmt: skip
    fake.containers[one]["_logs"] = frame('{"pending": []}\n') + frame("a warning\n", 2)
    with Running(fake) as running:
        client = EngineClient(running.socket_path, Scope(project=PROJECT))
        client.negotiate()
        fake.calls.clear()
        yield fake, client, ours, theirs


def test_r27_an_image_a_project_container_runs_is_inspected_by_id(engine_world):
    fake, client, ours, _ = engine_world
    seen = client.inspect_image_id(ours)
    assert seen["RepoDigests"] == [ref(eng.REPOSITORIES[0], A)]
    assert fake.calls[-1].bare == f"/images/{ours.removeprefix('sha256:')}/json"
    # Podman gives the id without its prefix; the same image.
    assert client.inspect_image_id(ours.removeprefix("sha256:"))["Id"] == ours


def test_r27_any_other_image_id_is_refused_before_it_is_sent(engine_world):
    fake, client, _, theirs = engine_world
    for refused in (theirs, "sha256:" + "1" * 64, "postgres", "../../containers/json"):
        calls = len(fake.calls)
        with pytest.raises(NotAllowed):
            client.inspect_image_id(refused)
        assert not any(c.bare.startswith("/images/") for c in fake.calls[calls:])


def test_logs_are_read_from_the_updaters_own_one_offs_and_nothing_else(engine_world):
    fake, client, _, _ = engine_world
    assert client.logs("spend-tracker-app-1-check-5d3c0b8e", stderr=False) == '{"pending": []}\n'
    assert client.logs("spend-tracker-app-1-check-5d3c0b8e") == '{"pending": []}\na warning\n'
    calls = len(fake.calls)
    for refused in ("spend-tracker-app-1", "other-db-1"):
        with pytest.raises(NotAllowed):
            client.logs(refused)
    assert not any(c.bare.endswith("/logs") for c in fake.calls[calls:])


def test_unframed_output_is_taken_as_it_is():
    assert eng.demux(b"plain text") == "plain text"
    assert eng.demux(frame("out") + frame("err", 2), (2,)) == "err"


# --------------------------------------------------------------------------- #
# One-offs
# --------------------------------------------------------------------------- #


def test_a_one_off_past_its_deadline_is_stopped_removed_and_reported(tmp_path):
    with World(tmp_path) as w:
        kit = w.kit()
        w.drill = "hang"
        app = w.fake.inspect_of(w.fake.containers[w.app_id])
        body = shapes.oneoff(app, ref(eng.REPOSITORIES[0], B), "drill", "5d3c0b8e-7f0a-4b8e-9f43-0f6f1a2b9c11",
                             ["python", "-m", "scripts.upgrade", "--yes", "--report",
                              "/var/lib/spend-tracker-update/work/x/drill.json"], ledger_volume="l")  # fmt: skip
        (w.volume.root / "work" / "x").mkdir()
        runner = kit.runner
        with pytest.raises(oneoff.TimedOut):
            runner.run("spend-tracker-app-1-drill-5d3c0b8e", body, 10)
        assert w.by_name("spend-tracker-app-1-drill-5d3c0b8e") is None
        assert w.time() > 0


def test_a_deadline_does_not_count_a_gap():
    t = {"wall": 1000.0, "mono": 1000.0}
    clock = GapClock(lambda: t["wall"], lambda: t["mono"])
    deadline = Deadline(clock, 60)
    t["wall"] += 3600  # asleep for an hour
    clock.tick()
    t["wall"] += 10
    t["mono"] += 10
    assert not deadline.expired() and deadline.elapsed() == pytest.approx(10)


# --------------------------------------------------------------------------- #
# `python -m updater`
# --------------------------------------------------------------------------- #


def test_the_updater_learns_its_own_digest_and_volume_from_its_container(tmp_path):
    with World(tmp_path) as w:
        kit = w.kit()
        me, update_volume = main.identify(kit.client, mountinfo="", hostname=w.updater_id[:12])
        assert me == Owner(image_digest=digest(UPD, A), version=A, container=f"{PROJECT}-updater-1")
        assert update_volume == f"{PROJECT}_update"
        nobody, none = main.identify(kit.client, mountinfo="", hostname="not-a-container")
        assert nobody.image_digest == "" and nobody.container == "" and none is None


def test_the_entry_point_builds_its_kit_with_sigstore_and_no_way_to_change_it(tmp_path):
    with World(tmp_path) as w:
        args = main.parser().parse_args(
            [
                "--socket",
                w.running.socket_path,
                "--update",
                str(w.volume.root),
                "--project-dir",
                str(w.project_dir),
            ]
        )
        kit, identity = main.build(args, trust.Sigstore())
        assert isinstance(kit.trust, trust.Sigstore) and kit.site.project == PROJECT
        assert kit.site.project_dir == w.project_dir and kit.site.engine == "docker-engine"
        assert kit.site.update_volume == f"{PROJECT}_update"
        assert {a.dest for a in main.parser()._actions} == {
            "help",
            "socket",
            "update",
            "update_volume",
            "project_dir",
            "project",
            "hook",
            "successor",
        }
        assert isinstance(args, argparse.Namespace) and identity.updater_version in ("0.0.0", A)


# --------------------------------------------------------------------------- #
# The request loop's edges
# --------------------------------------------------------------------------- #


def test_update_updater_without_a_handover_pulls_only_the_updater_and_stays(tmp_path):
    with World(tmp_path) as w:
        service = w.service()
        service.startup()
        req = {**w.base("update_updater"), "to_version": B}
        w.write_request(req)
        service.tick()
        record = w.history(req["id"])
        assert record["state"] == "not_started" and "not available" in record["sentence"]
        assert record["sentence"].startswith("The updater stayed on 0.7.1")
        # The short prepare pulled the updater image of B, and nothing else changed.
        changing = [c for c in w.fake.calls if c.method != "GET"]
        assert [(c.bare, c.query.get("fromImage")) for c in changing] == [("/images/create", ref(UPD, B))]
        assert ref(APP, B) not in w.fake.images


def test_a_request_a_crash_left_half_taken_is_answered_at_start(tmp_path):
    with World(tmp_path) as w:
        req = w.prepare_request()
        w.write_request(req)
        os.rename(w.volume.request, w.volume.root / "journal" / ".intake-0123456789abcdef.taken")
        service = w.service()
        service.startup()
        assert w.history(req["id"])["state"] == "succeeded"
        assert w.volume.prepared(req["id"]).exists()


def test_the_engine_going_away_during_prepare_refuses_it(tmp_path):
    with World(tmp_path) as w:
        service = w.service()
        service.startup()
        req = w.prepare_request()
        w.write_request(req)
        w.on_drill = None
        original = w._measure

        def vanish(fake, c, cmd, version):
            original(fake, c, cmd, version)
            fake.gone = True

        w._measure = vanish
        service.tick()
        w.fake.gone = False
        record = w.history(req["id"])
        assert record["state"] == "refused" and "stopped answering" in record["sentence"]
        assert not w.volume.prepared(req["id"]).exists()


def test_the_loop_stops_when_told_and_a_request_meets_an_absent_engine_with_a_sentence(tmp_path):
    import threading

    with World(tmp_path) as w:
        service = w.service()
        stop = threading.Event()
        stop.set()
        service.run(stop)
        assert service.resume_pending is False
        w.fake.gone = True
        req = w.prepare_request()
        w.write_request(req)
        service.tick()
        w.fake.gone = False
        record = w.history(req["id"])
        assert (
            record["state"] == "refused"
            and record["sentence"] == "The container engine's socket does not answer."
        )
