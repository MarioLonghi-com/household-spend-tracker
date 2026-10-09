"""#275: the updater rejoins the app to its sidecar's network after the sidecar restarts.

Against the fake engine and the simulated installation (`tests/updater_world.py`),
whose sidecar answers `wget` only for an app that joined its *current*
namespace. Two of everything: a second compose project on the same engine,
with its own sidecar and app, is broken the same way in every test and must
never be touched.
"""

from __future__ import annotations

import copy
import os

from tests.updater_world import APP, PROJECT, A, World, app_inspect, ref
from updater import contract, intake, rejoin, shapes, volume
from updater import engine as eng

OTHER = "other-tracker"


def add_other_project(w: World) -> tuple[str, str]:
    """A second installation on the same engine: its own sidecar and app, in the sidecar layout."""
    sidecar = {
        "Id": os.urandom(32).hex(),
        "Name": f"/{OTHER}-tailscale-1",
        "Image": "sha256:" + "5" * 64,
        "Config": {
            "Image": "tailscale/tailscale:v1.102.5",
            "Labels": {"com.docker.compose.project": OTHER, "com.docker.compose.service": "tailscale"},
        },
        "HostConfig": {"NetworkMode": f"{OTHER}_default"},
        "State": {"Status": "running", "Running": True},
    }
    sidecar_id = w.fake.add_inspected(sidecar)
    app = app_inspect(
        f"{OTHER}-app-1", w.fake.images[ref(APP, A)], layout="sidecar", sidecar_id=sidecar_id, version=A
    )
    app["Config"]["Labels"]["com.docker.compose.project"] = OTHER
    app_id = w.fake.add_inspected(app)
    return sidecar_id, app_id


def restart_other(w: World, sidecar_id: str) -> None:
    c = w.fake.containers[sidecar_id]
    w.fake.set_state(c, "exited")
    w.fake.set_state(c, "running")


def answers(w: World) -> bool:
    """Whether the app answers inside the sidecar, as the sidecar's own healthcheck asks."""
    code, _ = w._on_exec(w.fake, w.fake.containers[w.sidecar_id], ["wget"])
    return code == 0


def mutating(w: World, cid: str) -> list:
    return [c for c in w.fake.calls if c.method != "GET" and cid in c.bare]


def rejoins(w: World) -> list[dict]:
    out = []
    for path in sorted((w.volume.root / "history").glob("*.json")):
        doc = volume.read_own_json(path) or {}
        if doc.get("kind") == rejoin.KIND:
            out.append(doc)
    return out


def started_at(w: World, cid: str) -> float:
    seen = w.fake.inspect_of(w.fake.containers[cid])
    return rejoin.started(seen) or 0.0


def later(w: World, seconds: float = rejoin.CHECK_EVERY_SECONDS + 1) -> None:
    w.time.sleep(seconds)


def test_a_restarted_sidecar_gets_the_app_restarted_once_and_recorded(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        other_sidecar, other_app = add_other_project(w)
        service = w.service()
        service.tick()
        assert answers(w) and rejoins(w) == []

        w.restart_sidecar()
        restart_other(w, other_sidecar)
        assert not answers(w)
        later(w)
        service.tick()

        # Restarted in place: the same container, stopped and started, nothing created.
        assert [c.bare.rsplit("/", 1)[1] for c in mutating(w, w.app_id)] == ["stop", "start"]
        assert not [c for c in w.fake.calls if c.bare == "/containers/create"]
        assert answers(w)
        assert started_at(w, w.app_id) > started_at(w, w.sidecar_id)
        # The sidecar itself is never acted on, here or in the other project.
        assert mutating(w, w.sidecar_id) == []
        assert mutating(w, other_sidecar) == [] and mutating(w, other_app) == []
        [record] = rejoins(w)
        assert record["state"] == "succeeded" and record["how"] == "restart"
        assert record["sidecar"] == f"{PROJECT}-tailscale-1"
        assert record["sentence"].startswith("The Tailscale sidecar restarted")
        assert contract.is_uuid4(record["id"])
        assert service.heartbeat_problem is None

        # Once: the next check finds them agreeing.
        later(w)
        service.tick()
        assert len(mutating(w, w.app_id)) == 2 and len(rejoins(w)) == 1


def test_a_healthy_sidecar_layout_is_left_alone(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        add_other_project(w)
        service = w.service()
        before = started_at(w, w.app_id)
        for _ in range(3):
            service.tick()
            later(w)
        assert [c for c in w.fake.calls if c.method != "GET"] == []
        assert started_at(w, w.app_id) == before and rejoins(w) == []


def test_the_loopback_layout_is_never_repaired(tmp_path):
    with World(tmp_path, layout="loopback") as w:
        service = w.service()
        # Even a sidecar-looking service restarting next to it is none of its business.
        other_sidecar, other_app = add_other_project(w)
        restart_other(w, other_sidecar)
        for _ in range(3):
            service.tick()
            later(w)
        assert [c for c in w.fake.calls if c.method != "GET"] == []
        assert rejoins(w) == [] and service.rejoin.problem is None


def test_nothing_is_repaired_while_an_apply_is_in_flight(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        service = w.service()
        report = w.prepared(service)
        seen: list[object] = []

        def mid_drill(c):
            # The sidecar restarts while the drill runs, and the check comes due.
            w.restart_sidecar()
            later(w)
            seen.append(service.idle())
            seen.append(service.rejoin.tick())

        w.on_drill = mid_drill
        req = w.apply_request(report)
        w.write_request(req)
        service.tick()
        assert seen == [False, None]
        assert rejoins(w) == []
        # The apply started the new version after the restart, inside the new namespace:
        # afterwards there is nothing to repair either.
        assert w.history(req["id"])["state"] == "succeeded"
        later(w)
        service.tick()
        assert answers(w) and rejoins(w) == []


def test_a_waiting_request_or_an_unfinished_journal_keeps_it_idle(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        service = w.service()
        service.tick()
        assert service.idle()
        w.write_request(w.prepare_request())
        assert not service.idle()
        os.unlink(w.volume.request)
        assert service.idle()
        # A taken request whose answer was never written: an apply half-taken.
        (w.volume.root / "journal" / f"{intake.INTAKE_PREFIX}abc.taken").write_text("{}")
        assert not service.idle()


def test_repeated_breakage_is_rate_limited_and_reported(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        service = w.service()
        service.tick()
        for _ in range(rejoin.MAX_PER_HOUR):
            w.restart_sidecar()
            later(w)
            service.tick()
            assert answers(w)
        assert len(rejoins(w)) == rejoin.MAX_PER_HOUR

        w.restart_sidecar()
        later(w)
        service.tick()
        assert not answers(w)
        assert len(rejoins(w)) == rejoin.MAX_PER_HOUR
        assert len(mutating(w, w.app_id)) == 2 * rejoin.MAX_PER_HOUR
        problem = service.heartbeat_problem
        assert problem is not None and rejoin.BY_HAND in problem and "within an hour" in problem

        # A restarted updater counts the hour from the history, not from zero.
        fresh = w.service()
        fresh.tick()
        later(w)
        fresh.tick()
        assert len(rejoins(w)) == rejoin.MAX_PER_HOUR and fresh.heartbeat_problem is not None

        # Once the hour has passed, it rejoins again and the problem clears.
        later(w, rejoin.WINDOW_SECONDS)
        fresh.tick()
        assert answers(w) and len(rejoins(w)) == rejoin.MAX_PER_HOUR + 1
        assert fresh.heartbeat_problem is None


def test_a_failed_repair_is_reported_not_retried_in_a_loop(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        service = w.service()
        service.tick()
        w.fake.refuse_stop.add(w.app_id)
        w.restart_sidecar()
        for _ in range(rejoin.MAX_PER_HOUR + 3):
            later(w)
            service.tick()
        stops = [c for c in mutating(w, w.app_id) if c.bare.endswith("/stop")]
        assert len(stops) == rejoin.MAX_PER_HOUR
        assert rejoins(w) == [] and rejoin.BY_HAND in (service.heartbeat_problem or "")


def test_a_replaced_sidecar_gets_the_app_recreated_inside_the_new_one(tmp_path):
    with World(tmp_path, layout="sidecar") as w:
        other_sidecar, other_app = add_other_project(w)
        service = w.service()
        service.tick()
        old_env = list(w.fake.inspect_of(w.fake.containers[w.app_id])["Config"]["Env"])
        # Recreated outside compose: the old id is gone, a new container of the service runs.
        old = copy.deepcopy(w.fake.inspect_of(w.fake.containers[w.sidecar_id]))
        del w.fake.containers[w.sidecar_id]
        old["Id"] = os.urandom(32).hex()
        old.pop("State")
        old["State"] = {"Status": "running", "Running": True}
        w.sidecar_id = w.fake.add_inspected(old)
        w.epoch += 1
        later(w)
        service.tick()

        [create] = [c for c in w.fake.calls if c.bare == "/containers/create"]
        assert create.query["name"] == w.app_name
        assert create.body["HostConfig"]["NetworkMode"] == f"container:{w.sidecar_id}"
        assert create.body["Image"] == ref(APP, A)
        # The same image, the same environment: the migration switch is the app's own, not the apply's 0.
        assert (
            shapes.env_dict(create.body["Env"])[shapes.AUTO_MIGRATE]
            == shapes.env_dict(old_env)[shapes.AUTO_MIGRATE]
        )
        assert set(create.body["Env"]) <= set(old_env)
        assert w.app_id not in w.fake.containers
        assert answers(w)
        assert mutating(w, other_sidecar) == [] and mutating(w, other_app) == []
        [record] = rejoins(w)
        assert record["how"] == "recreate" and record["state"] == "succeeded"


def test_the_engine_client_still_refuses_to_restart_the_sidecar(tmp_path):
    """The repair names the sidecar in the client's scope, so a slip could not touch it."""
    with World(tmp_path, layout="sidecar") as w:
        service = w.service()
        service.tick()
        w.restart_sidecar()
        later(w)
        service.tick()
        client = service.kit.client
        assert client.scope.sidecar == f"{PROJECT}-tailscale-1"
        for act in (client.stop, client.start):
            try:
                act(w.sidecar_id)
            except eng.NotAllowed:
                continue
            raise AssertionError("the sidecar was acted on")
