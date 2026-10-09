"""The updater hands over to its successor (design notes 6.6, C1-C5; 15.1 U9; Part 12 rows 26-28, 31, 32).

Two updaters run against the simulated installation (`tests/updater_world.py`,
`fleet=True`): the running one, U1, of release A, and the successor it starts,
U2, of a newer release. Each has its own process -- engine client, handover,
request loop and heartbeat -- and the world's clock drives both. Every test
asserts what the handover did to the containers, the lock, the pin, the
heartbeat and the records, not only that it said so.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from tests.updater_fake_engine import engine_fixture
from tests.updater_world import (
    APP,
    MULTIARCH,
    PROJECT,
    UPD,
    A,
    B,
    C,
    Killed,
    World,
    digest,
    platform_digest,
    ref,
)
from updater import contract, journal, pin, shapes, volume
from updater import engine as eng
from updater import service as service_module
from updater.handover import (
    GONE_GRACE_SECONDS,
    STANDBY_SECONDS,
    Successions,
    current_side,
    goes_first,
    is_me,
    load,
    lock_holder,
    own_bind_sources,
)
from updater.journal import Owner

CANONICAL = f"{PROJECT}-updater-1"
PREVIOUS = f"{CANONICAL}-previous"
NEXT = f"{CANONICAL}-next"


@pytest.fixture(params=MULTIARCH, ids=["one-digest", "index-first", "platform-first"])
def world(tmp_path, request):
    """Every handover test on an engine that lists one digest per image, and on
    one that lists the index's and the platform manifest's, in both orders (#287)."""
    with World(tmp_path, fleet=True, multiarch=request.param) as w:
        boot(w)
        yield w


def boot(w: World) -> None:
    """U1's process starts."""
    w.pending.append(w.updater_id)
    w.fleet_tick()
    assert [u.mode for u in w.current()] == ["current"]


def send(w: World, doc: dict, seconds: float = 30) -> dict | None:
    w.write_request(doc)
    w.run_for(seconds)
    return w.history(doc["id"])


def update_updater(w: World, to: str = C, seconds: float = 30) -> tuple[dict, dict | None]:
    req = {**w.base("update_updater"), "to_version": to}
    return req, send(w, req, seconds)


def ping(w: World) -> dict | None:
    doc = {k: v for k, v in w.base("ping").items() if k != "requested_by"}
    return send(w, doc, 6)


def named(w: World, name: str) -> dict | None:
    return w.by_name(name)


def beat(w: World) -> dict:
    found = volume.read_own_json(w.volume.heartbeat)
    assert found is not None
    return found


def assert_exactly_one_current(w: World) -> Owner:
    """U9's end state: one updater current, holding the lock, writing the heartbeat, answering."""
    current = w.current()
    assert len(current) == 1, [(w.who(u.cid), u.mode) for u in w.fleet.values()]
    u = current[0]
    assert w.fake.containers[u.cid]["Names"] == [f"/{CANONICAL}"]
    holder = lock_holder(w.volume)
    assert holder is not None and holder.is_(u.me)
    w.run_for(4)
    assert beat(w)["role"] == "current" and beat(w)["image_digest"] == u.me.image_digest
    running = [c for c in w.updater_containers() if c["State"] == "running"]
    assert [c["Id"] for c in running] == [u.cid]
    answer = ping(w)
    assert answer is not None and answer["state"] == "succeeded"
    return u.me


def app_unchanged(w: World, before: dict) -> None:
    app = named(w, w.app_name)
    assert app is not None and app["Id"] == w.app_id and app["State"] == "running"
    assert w.fake.inspect_of(app)["State"]["StartedAt"] == before["StartedAt"]


# --------------------------------------------------------------------------- #
# H1-H7, and the updater-only refresh (C2, E14)
# --------------------------------------------------------------------------- #


def test_update_updater_hands_over_and_the_old_one_stops_after_ten_minutes(world):
    app_state = dict(world.fake.inspect_of(world.fake.containers[world.app_id])["State"])
    env_before = (world.project_dir / ".env").read_text()
    req, record = update_updater(world, C)

    assert record["state"] == "succeeded" and record["sentence"] == "The updater now runs 0.9.0."
    assert world.writes == [
        ("H1", "U1"),
        ("H2", "U1"),
        ("H3", "U1"),
        ("H3", "U2"),
        ("H4", "U1"),
        ("H5", "U2"),
        ("H6", "U1"),
    ]
    u2 = world.successor_id()
    # H5: the successor holds the canonical name, the old one is -previous.
    assert named(world, CANONICAL)["Id"] == u2 and named(world, PREVIOUS)["Id"] == world.updater_id
    assert world.version_of(world.fake.containers[u2]) == C
    # U1 is on standby: running, silent, and not the one answering.
    assert world.fleet[world.updater_id].mode == "standby" and world.is_running(world.updater_id)
    assert beat(world)["image_digest"] == digest(UPD, C) and beat(world)["container"] == CANONICAL
    assert lock_holder(world.volume).image_digest == digest(UPD, C)
    # Only the pin's updater line changed; the app's was never written.
    pinned = pin.read(world.project_dir)
    assert pinned == {pin.UPDATER_KEY: f"{UPD}:{C}@{digest(UPD, C)}"}
    assert (world.project_dir / ".env").read_text().startswith(env_before)
    # The app was not touched at all: same container, same start.
    app_unchanged(world, app_state)
    assert world.ledger.stamp == A and world.ledger.drills == 0

    world.run_for(STANDBY_SECONDS - 60)
    assert world.is_running(world.updater_id), "still on standby inside the ten minutes"
    world.run_for(90)
    # H7: ten healthy minutes, and U1 stopped itself; it stays as -previous.
    assert not world.is_running(world.updater_id)
    assert named(world, PREVIOUS)["State"] == "exited" and ("H7", "U1") in world.writes
    doc = load(world.volume, req["id"])
    assert doc["outcome"] == "done" and doc["settled"] is True
    assert assert_exactly_one_current(world).version == C


def test_h7_exits_on_the_sigterm_while_its_own_stop_is_still_in_flight(world, monkeypatch):
    """#257: the engine answers a stop only once the container has exited.

    U1's process loop -- `Service.run`, as `serve` runs it -- has to go on
    while its own stop is in flight, see the `stop` event the SIGTERM sets,
    and return, so the container exits 0 within the grace instead of being
    killed (137) when the grace runs out.
    """
    req, _ = update_updater(world, C)
    world.run_for(STANDBY_SECONDS - 60)
    # From here U1's process runs its own loop, on a thread, as `serve` does.
    u1 = world.fleet.pop(world.updater_id)
    assert u1.mode == "standby"
    world.run_for(90)  # U2 carries on beating; U1's ten minutes are up
    me = world.fake.containers[world.updater_id]
    sigterm, exited = threading.Event(), threading.Event()
    asked: list[str] = []

    def engine_stop(ref: str, grace: int = 30) -> None:
        # The engine: SIGTERM, then the answer once the process has exited --
        # or the kill after the grace. Five seconds stand in for the grace.
        asked.append(ref)
        sigterm.set()
        code = 0 if exited.wait(5) else 137
        world.fake.set_state(me, "exited", code)

    monkeypatch.setattr(u1.kit.client, "stop", engine_stop)
    monkeypatch.setattr(u1.service, "startup", lambda: None)  # it started long ago
    monkeypatch.setattr(service_module, "TICK_SECONDS", 0.05)

    def process() -> None:
        u1.service.run(sigterm)
        exited.set()  # `serve` returned: `main` returns 0 and the process ends

    loop = threading.Thread(target=process, daemon=True)
    loop.start()
    loop.join(4)
    assert not loop.is_alive(), "the loop was still held by its own stop"
    assert asked == [world.updater_id] and u1.mode == "retired"
    for _ in range(100):
        if me["State"] == "exited":
            break
        time.sleep(0.02)
    assert me["State"] == "exited" and world.fake.inspect_of(me)["State"]["ExitCode"] == 0
    assert named(world, PREVIOUS)["Id"] == world.updater_id and ("H7", "U1") in world.writes
    doc = load(world.volume, req["id"])
    assert doc["step"] == "H7" and doc["outcome"] == "done" and doc["settled"] is True


def test_the_next_handover_removes_the_previous_one_and_keeps_one_previous(world):
    update_updater(world, B)
    world.run_for(STANDBY_SECONDS + 30)
    first_previous = named(world, PREVIOUS)["Id"]
    assert first_previous == world.updater_id
    b_updater = named(world, CANONICAL)["Id"]

    req, record = update_updater(world, C)
    assert record["state"] == "succeeded"
    # B's updater is -previous now; A's, the old -previous, is gone (6.6, H7).
    assert world.updater_id not in world.fake.containers
    assert named(world, PREVIOUS)["Id"] == b_updater
    assert world.version_of(named(world, CANONICAL)) == C
    assert [c["Names"][0] for c in world.updater_containers()].count(f"/{PREVIOUS}") == 1


def test_the_successor_is_a_copy_of_the_running_updater_with_its_image_and_one_argument(world):
    update_updater(world, B)
    create = next(c for c in world.fake.calls if c.bare == "/containers/create" and c.query.get("name") == NEXT)
    body = create.body
    own = world.fake.inspect_of(world.fake.containers[world.updater_id])
    assert body["Image"] == ref(UPD, B)
    assert body["Cmd"][-2:] == ["--successor", load(world.volume, next(iter_requests(world)))["id"]]
    for key in ("Binds", "GroupAdd", "RestartPolicy", "Memory", "ReadonlyRootfs", "Tmpfs", "CapDrop", "SecurityOpt"):
        assert body["HostConfig"][key] == own["HostConfig"][key], key
    assert body["User"] == own["Config"]["User"]
    assert body["Labels"]["com.docker.compose.service"] == "updater"
    # What came from A's image is left to B's: its version label above all.
    assert "org.opencontainers.image.version" not in body["Labels"]
    assert body["NetworkingConfig"]["EndpointsConfig"][f"{PROJECT}_default"]["Aliases"] == [CANONICAL, "updater"]
    # The guard lets it bind exactly the socket and the project directory.
    sources = own_bind_sources(own, ("/run/engine.sock", "/project"))
    assert sources == ("/var/run/docker.sock", str(world.project_dir))
    eng.guard_create(body, eng.Scope(project=PROJECT, bind_sources=sources))
    with pytest.raises(eng.NotAllowed):
        eng.guard_create(body, eng.Scope(project=PROJECT, bind_sources=sources[:1]))


HOOK_HOST = "/srv/spend-tracker/hook"


def with_hook(w: World) -> None:
    """A server's `compose.override.yaml` mounts a host directory at `/hook` in the updater (6.5)."""
    seen = w.fake.containers[w.updater_id]["_inspect"]
    seen["HostConfig"]["Binds"].append(f"{HOOK_HOST}:/hook:rw")
    seen["Mounts"].append({"Type": "bind", "Source": HOOK_HOST, "Destination": "/hook"})


def successor_body(w: World) -> dict:
    return next(c for c in w.fake.calls if c.bare == "/containers/create" and c.query.get("name") == NEXT).body


def test_the_successor_keeps_the_pre_update_hooks_bind(tmp_path):
    with World(tmp_path, fleet=True) as w:
        with_hook(w)
        boot(w)
        _, record = update_updater(w, B)
        assert record["state"] == "succeeded"
        body = successor_body(w)
        assert f"{HOOK_HOST}:/hook:rw" in body["HostConfig"]["Binds"]
        u2 = w.fake.inspect_of(w.fake.containers[w.successor_id()])
        assert [m["Source"] for m in u2["Mounts"] if m["Destination"] == "/hook"] == [HOOK_HOST]
        # Exactly U1's own: the same bind from another host directory is refused.
        own = w.fake.inspect_of(w.fake.containers[w.updater_id])
        scope = eng.Scope(project=PROJECT, bind_sources=own_bind_sources(own, ("/run/engine.sock", "/project", "/hook")))
        eng.guard_create(body, scope)
        elsewhere = {**body, "HostConfig": {**body["HostConfig"], "Binds": ["/etc:/hook:rw"]}}
        with pytest.raises(eng.NotAllowed):
            eng.guard_create(elsewhere, scope)


def test_without_a_hook_the_successor_binds_no_hook_directory(world):
    _, record = update_updater(world, B)
    assert record["state"] == "succeeded"
    body = successor_body(world)
    assert not [b for b in body["HostConfig"]["Binds"] if ":/hook" in b]
    own = world.fake.inspect_of(world.fake.containers[world.updater_id])
    assert own_bind_sources(own, ("/run/engine.sock", "/project", "/hook")) == (
        "/var/run/docker.sock",
        str(world.project_dir),
    )


#: Each key the compose files give the `updater` service, and the inspect
#: field it becomes, which the successor's copy must carry (6.6, H2). `image`
#: is the one thing that changes; `build` never reaches the engine.
COMPOSE_TO_INSPECT = {
    "image": None,
    "build": None,
    "restart": ("HostConfig", "RestartPolicy"),
    "user": ("Config", "User"),
    "group_add": ("HostConfig", "GroupAdd"),
    "read_only": ("HostConfig", "ReadonlyRootfs"),
    "tmpfs": ("HostConfig", "Tmpfs"),
    "security_opt": ("HostConfig", "SecurityOpt"),
    "cap_drop": ("HostConfig", "CapDrop"),
    "mem_limit": ("HostConfig", "Memory"),
    "volumes": ("HostConfig", "Binds"),
}


@pytest.mark.parametrize("compose_file", ["compose.yaml", "deploy/tailnet/compose.yaml"])
def test_every_setting_of_the_compose_updater_service_is_carried_to_the_successor(compose_file):
    import pathlib

    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent
    service = yaml.safe_load((root / compose_file).read_text())["services"]["updater"]
    # A key nobody has mapped is a decision to make here, not a setting to lose.
    assert set(service) <= set(COMPOSE_TO_INSPECT), set(service) - set(COMPOSE_TO_INSPECT)
    for key in service:
        where = COMPOSE_TO_INSPECT[key]
        if where is None:
            continue
        section, field = where
        assert field in (shapes.HOST_FIELDS if section == "HostConfig" else shapes.CONFIG_FIELDS), key


def iter_requests(w: World):
    for path in sorted((w.volume.root / "handover").glob("*.request")):
        yield path.name[: -len(".request")]


def test_successor_shape_replaces_an_earlier_successor_argument_and_takes_the_new_images_arguments():
    own = {
        "Config": {"Cmd": ["--project", "x", "--successor", "old"], "Labels": {}, "Env": []},
        "HostConfig": {"NetworkMode": "default"},
    }
    body = shapes.successor(own, "img", "new", image_config={"Cmd": []})
    assert body["Cmd"] == ["--project", "x", "--successor", "new"]
    from_image = {"Config": {"Cmd": ["--a"], "Labels": {}}, "HostConfig": {"NetworkMode": "default"}}
    body = shapes.successor(from_image, "img", "id2", image_config={"Cmd": ["--a"]}, new_image_config={"Cmd": ["--b"]})
    assert body["Cmd"] == ["--b", "--successor", "id2"]
    with pytest.raises(ValueError):
        shapes.successor({"HostConfig": {"NetworkMode": "container:abc"}}, "img", "id3")


# --------------------------------------------------------------------------- #
# U9: kill either updater after each journal write
# --------------------------------------------------------------------------- #

#: Each step's journal write and who makes it: U1 writes H1, H2, H4 (`go`),
#: H6 and H7; U2 writes H3 (`ready`) and H5. At H1 and H2 there is no U2 yet.
U9 = [
    ("H1", "U1"),
    ("H2", "U1"),
    ("H3", "U1"),
    ("H3", "U2"),
    ("H4", "U1"),
    ("H4", "U2"),
    ("H5", "U1"),
    ("H5", "U2"),
    ("H6", "U1"),
    ("H6", "U2"),
    ("H7", "U1"),
    ("H7", "U2"),
]


@pytest.mark.parametrize(("step", "victim"), U9)
def test_u9_killing_either_updater_after_any_write_leaves_exactly_one_current(tmp_path, step, victim):
    with World(tmp_path, fleet=True) as w:
        boot(w)
        app_state = dict(w.fake.inspect_of(w.fake.containers[w.app_id])["State"])
        w.kill_at = (step, victim)
        req, _ = update_updater(w, C, seconds=60)
        w.run_for(STANDBY_SECONDS + 6 * 60)
        assert w.killed == [(step, victim)]

        me = assert_exactly_one_current(w)
        doc = load(w.volume, req["id"])
        go = journal.read_handover(w.volume, req["id"], "go")
        # U1's journal is what decides, and it agrees with what runs.
        side = current_side(doc, go is not None, beat(w))
        assert doc["settled"] is True
        assert me.image_digest == digest(UPD, C if side == "successor" else A)
        record = w.history(req["id"])
        pinned = pin.read(w.project_dir).get(pin.UPDATER_KEY)
        if side == "successor":
            assert record["state"] == "succeeded" and doc["outcome"] == "done"
            assert pinned == f"{UPD}:{C}@{digest(UPD, C)}"
        else:
            assert record["state"] in ("not_started", "rolled_back")
            assert doc["outcome"] in ("failed", "taken_back")
            assert pinned is None or pinned == f"{UPD}:{A}@{digest(UPD, A)}"
        app_unchanged(w, app_state)


def test_an_engine_restart_after_go_resumes_both_journals_and_one_carries_on(world):
    real = world._wrote

    def wrote(step, cid):
        real(step, cid)
        if step == "H4":
            # The engine goes away right after `go`: both processes die, and
            # both containers come back.
            world._wrote = real  # type: ignore[method-assign]
            world.engine_restart()
            raise Killed(step)

    world._wrote = wrote  # type: ignore[method-assign]
    req = {**world.base("update_updater"), "to_version": B}
    world.write_request(req)
    world.run_for(STANDBY_SECONDS + 6 * 60)
    me = assert_exactly_one_current(world)
    doc = load(world.volume, req["id"])
    assert doc["settled"] is True
    # `go` was written and U2 had no heartbeat as current yet: U1's journal
    # makes U1 current, U1 took back over, and U2 retired.
    assert doc["outcome"] == "taken_back" and me.version == A
    assert world.history(req["id"])["state"] == "rolled_back"
    assert named(world, NEXT)["State"] == "exited"


# --------------------------------------------------------------------------- #
# H3: the successor proves itself, or is refused (row 26)
# --------------------------------------------------------------------------- #


def test_a_successor_reporting_the_wrong_digest_of_itself_is_refused_at_h3(world):
    world.lying_digest = digest(UPD, A)
    req, record = update_updater(world, B)
    assert record["state"] == "not_started"
    assert record["sentence"].startswith("The updater stayed on 0.7.1: the updater of 0.8.0 failed its own check")
    assert ("H5", "U2") not in world.writes and journal.read_handover(world.volume, req["id"], "go") is None
    # U2 removed; U1 current, untouched, holding the lock.
    assert named(world, NEXT) is None and world.successor_id() is None
    assert named(world, CANONICAL)["Id"] == world.updater_id
    assert assert_exactly_one_current(world).version == A
    assert pin.UPDATER_KEY not in pin.read(world.project_dir)


def test_a_successor_whose_ready_names_another_digest_is_refused_by_the_engines_word_too(world, monkeypatch):
    real = Successions.self_check

    def lies(self, doc):
        out = real(self, doc)
        return {**out, "ok": True, "problem": None, "image_digest": digest(UPD, C)}

    monkeypatch.setattr(Successions, "self_check", lies)
    req, record = update_updater(world, B)
    assert record["state"] == "not_started" and "reported a different image of itself" in record["sentence"]
    assert named(world, NEXT) is None and assert_exactly_one_current(world).version == A


def test_a_successor_that_never_says_ready_is_removed_after_a_minute(world):
    real = World.spawn

    def spawn_dead(self, cid):
        u = real(self, cid)
        if cid != self.updater_id:
            u.dead = True  # its process never runs
        return u

    world.spawn = spawn_dead.__get__(world)  # type: ignore[method-assign]
    req, record = update_updater(world, B, seconds=90)
    assert record["state"] == "not_started" and "did not say it was ready within a minute" in record["sentence"]
    assert named(world, NEXT) is None
    assert assert_exactly_one_current(world).version == A


# --------------------------------------------------------------------------- #
# H6: take-back within ten minutes (row 27)
# --------------------------------------------------------------------------- #


def test_a_successor_that_dies_within_ten_minutes_is_taken_back_from(world):
    req, record = update_updater(world, C)
    assert record["state"] == "succeeded"
    u2 = world.successor_id()
    world.run_for(180)
    world.stay_down.add(u2)
    world.crash(u2, restart=False)
    # Taken back once the grace for a replacement has passed (GONE_GRACE_SECONDS).
    world.run_for(10)
    assert world.fleet[world.updater_id].mode != "current"
    world.run_for(GONE_GRACE_SECONDS)

    assert named(world, CANONICAL)["Id"] == world.updater_id and world.is_running(world.updater_id)
    assert named(world, NEXT)["Id"] == u2 and not world.is_running(u2)
    assert lock_holder(world.volume).image_digest == digest(UPD, A)
    assert pin.read(world.project_dir)[pin.UPDATER_KEY].endswith(digest(UPD, A))
    record = world.history(req["id"])
    assert record["state"] == "rolled_back" and "took back over" in record["sentence"]
    assert load(world.volume, req["id"])["outcome"] == "taken_back"
    world.run_for(STANDBY_SECONDS)
    # Current for good: the standby window does not stop a taken-back updater.
    assert assert_exactly_one_current(world).version == A


def test_a_successor_recreated_by_compose_within_ten_minutes_is_not_taken_back_from(world):
    """podman-compose's `up` removes and recreates the updater: the successor
    is a new container of the same image a few seconds later, and its
    heartbeat never went stale. That is U2 still (#169, E12 on rootless Podman)."""
    import copy

    req, record = update_updater(world, C)
    assert record["state"] == "succeeded"
    world.run_for(60)
    u2 = world.successor_id()
    seen = copy.deepcopy(world.fake.inspect_of(world.fake.containers[u2]))
    world.stay_down.add(u2)
    world.crash(u2, restart=False)
    del world.fake.containers[u2]
    world.run_for(6)
    # compose's new container: same image, the canonical name, its own command.
    cmd = seen["Config"].get("Cmd") or []
    if "--successor" in cmd:
        at = cmd.index("--successor")
        seen["Config"]["Cmd"] = cmd[:at] + cmd[at + 2 :]
    seen.pop("Id")
    seen["Name"] = "/" + CANONICAL
    seen["State"] = {"Running": True, "Status": "running"}
    fresh = world.fake.add_inspected(seen)
    world.pending.append(fresh)
    world.run_for(120)

    assert load(world.volume, req["id"]).get("outcome") != "taken_back"
    assert world.history(req["id"])["state"] == "succeeded"
    assert world.fleet[world.updater_id].mode != "current"
    assert [u.cid for u in world.current()] == [fresh]
    world.run_for(STANDBY_SECONDS)
    assert not world.is_running(world.updater_id)  # H7: retired after its ten minutes
    assert assert_exactly_one_current(world).version == C


def test_a_successor_replaced_by_compose_with_another_updater_is_stood_down_from_not_taken_back(world):
    """#259: a `compose up` during the standby, with `.env` pinned back to A's
    updater: compose removes U2 (C) and starts a new container of A's image
    under the canonical name. Two container ids, two digests."""
    import copy

    req, record = update_updater(world, C)
    assert record["state"] == "succeeded"
    world.run_for(60)
    u2 = world.successor_id()
    seen = copy.deepcopy(world.fake.inspect_of(world.fake.containers[u2]))
    world.stay_down.add(u2)
    world.crash(u2, restart=False)
    del world.fake.containers[u2]
    world.run_for(6)
    cmd = seen["Config"].get("Cmd") or []
    if "--successor" in cmd:
        at = cmd.index("--successor")
        seen["Config"]["Cmd"] = cmd[:at] + cmd[at + 2 :]
    seen.pop("Id")
    seen["Name"] = "/" + CANONICAL
    seen["State"] = {"Running": True, "Status": "running"}
    seen["Config"]["Image"] = f"{UPD}:{A}@{digest(UPD, A)}"  # the pin, put back by hand
    fresh = world.fake.add_inspected(seen, image_id=world.fake.images[ref(UPD, A)]["Id"])
    assert fresh != u2 and digest(UPD, A) != digest(UPD, C)
    # The new one starts before U1 next looks: U1's process is between ticks.
    u1 = world.fleet.pop(world.updater_id)
    world.pending.append(fresh)
    world.run_for(10)

    # The new container is not U1, though it runs U1's image: it does not
    # settle U1's handover in its place, and starts as current -- beating.
    assert world.fleet[fresh].mode == "current"
    assert load(world.volume, req["id"])["outcome"] is None
    assert beat(world)["role"] == "current" and beat(world)["image_digest"] == digest(UPD, A)
    assert beat(world)["container"] == CANONICAL and beat(world)["busy"] is False
    world.fleet[world.updater_id] = u1
    world.run_for(20)
    # U1 recorded what happened, and stopped itself as at H7, still -previous.
    doc = load(world.volume, req["id"])
    assert doc["outcome"] == "stood_down" and doc["settled"] is True
    assert "was replaced by another container under its name" in doc["sentence"]
    assert "stood down instead of taking back over" in doc["sentence"]
    assert world.fleet[world.updater_id].mode == "retired"
    assert not world.is_running(world.updater_id)
    assert named(world, PREVIOUS)["Id"] == world.updater_id and named(world, CANONICAL)["Id"] == fresh
    history = world.history(req["id"])
    assert history["state"] == "succeeded" and doc["sentence"] in history["notes"]
    assert not any("took back" in n for n in history["notes"])
    assert assert_exactly_one_current(world).image_digest == digest(UPD, A)


def test_a_take_back_the_engine_refuses_stands_down_instead_of_crashing(world, monkeypatch):
    """#259: the rename back onto the canonical name is refused (Podman: UNIQUE
    constraint failed). The standby records that it stood down -- not a
    take-back -- and stops itself; its service loop never sees the error."""
    req, record = update_updater(world, C)
    assert record["state"] == "succeeded"
    world.run_for(60)
    u2 = world.successor_id()
    u1 = world.fleet[world.updater_id]
    real_rename = u1.kit.client.rename

    def rename(ref: str, new_name: str) -> None:
        if new_name == CANONICAL:
            raise eng.EngineError(500, "UNIQUE constraint failed: ContainerConfig.Name")
        real_rename(ref, new_name)

    monkeypatch.setattr(u1.kit.client, "rename", rename)
    world.stay_down.add(u2)
    world.crash(u2, restart=False)  # crash-looping: no heartbeat, not running
    world.run_for(60)

    doc = load(world.volume, req["id"])
    assert doc["outcome"] == "stood_down" and doc["settled"] is True
    assert "could not take its name back" in doc["sentence"] and "which did not happen" in doc["sentence"]
    assert u1.mode == "retired" and not world.is_running(world.updater_id)
    assert named(world, PREVIOUS)["Id"] == world.updater_id and named(world, NEXT)["Id"] == u2
    history = world.history(req["id"])
    assert history["state"] == "succeeded" and doc["sentence"] in history["notes"]


def test_a_successor_whose_heartbeat_goes_stale_is_taken_back_from_after_two_minutes(world):
    req, _ = update_updater(world, C)
    u2 = world.fleet[world.successor_id()]
    u2.beat.role = lambda: None  # it runs, and stops writing its heartbeat
    stopped_at = world.time.t
    taken = []
    while not taken and world.time.t - stopped_at < 300:
        world.run_for(2)
        if world.fleet[world.updater_id].mode == "current":
            taken.append(world.time.t - stopped_at)
    assert taken and 120 <= taken[0] <= 140
    assert not world.is_running(u2.cid) and named(world, CANONICAL)["Id"] == world.updater_id
    assert world.history(req["id"])["state"] == "rolled_back"


# --------------------------------------------------------------------------- #
# Updater first (C1, E13; row 32) and step 10
# --------------------------------------------------------------------------- #


def prepare(w: World, to: str = B) -> dict:
    req = w.prepare_request(to)
    record = send(w, req, 20)
    assert record["state"] == "succeeded", record
    report = volume.read_own_json(w.volume.prepared(req["id"]))
    assert report is not None
    return report


def apply(w: World, to: str = B, seconds: float = 120) -> tuple[dict, dict | None]:
    report = prepare(w, to)
    req = w.apply_request(report)
    return req, send(w, req, seconds)


def test_updater_first_hands_over_before_the_app_stops_and_the_successor_applies(world):
    req, record = apply(world)
    assert record["state"] == "succeeded" and world.ledger.stamp == B
    j = world.journal(req["id"])
    owners = [(o["owner"]["version"], o["from_step"]) for o in j.owners]
    assert owners == [(A, "0"), (B, "2a")]
    steps = [s["step"] for s in j.started]
    assert steps.index("2a") < steps.index("3") and j.context["first_handover"] == "done"
    # The handover completed before the app was stopped: H5's rename of U1
    # comes before the first stop of the app container.
    calls = [c.bare for c in world.fake.calls if c.method == "POST"]
    rename_u1 = calls.index(f"/containers/{world.updater_id}/rename")
    stop_app = calls.index(f"/containers/{world.app_id}/stop")
    assert rename_u1 < stop_app
    # Run by B's updater: the pin names B's updater; no step 10.
    assert "10" not in steps
    assert pin.read(world.project_dir) == {
        pin.APP_KEY: f"{APP}:{B}@{digest(APP, B)}",
        pin.UPDATER_KEY: f"{UPD}:{B}@{digest(UPD, B)}",
    }
    world.run_for(STANDBY_SECONDS + 30)
    assert assert_exactly_one_current(world).version == B
    world.no_running_app_at_a_mismatched_stamp()


def test_a_failed_updater_first_leaves_the_apply_to_the_old_updater(world):
    world.lying_digest = digest(UPD, A)
    req, record = apply(world)
    assert record["state"] == "succeeded" and world.ledger.stamp == B
    j = world.journal(req["id"])
    assert [o["owner"]["version"] for o in j.owners] == [A]
    assert any("did not take over first" in n for n in record["notes"])
    # Step 10 tried again, and was refused the same way: A's updater stays.
    assert any(n.startswith("The updater stayed on 0.7.1.") for n in record["notes"])
    assert pin.read(world.project_dir)[pin.UPDATER_KEY].endswith(digest(UPD, A))
    assert assert_exactly_one_current(world).version == A
    world.no_running_app_at_a_mismatched_stamp()


def test_step_10_hands_over_after_the_apply_when_the_successor_cannot_go_first(tmp_path):
    with World(tmp_path, fleet=True, updater_protocols="2-3") as w:
        boot(w)
        req, record = apply(w)
        steps = [s["step"] for s in w.journal(req["id"]).started]
        assert "2a" not in steps and steps[-1] == "10"
        assert record["state"] == "succeeded" and w.ledger.stamp == B
        # Recorded before `go`, by the old updater, with the handover's note.
        assert any("takes over; this one stays on standby" in n for n in record["notes"])
        assert [o["owner"]["version"] for o in w.journal(req["id"]).owners] == [A]
        assert named(w, CANONICAL)["Id"] == w.successor_id()
        w.run_for(STANDBY_SECONDS + 30)
        assert assert_exactly_one_current(w).version == B
        assert pin.read(w.project_dir)[pin.UPDATER_KEY].endswith(digest(UPD, B))


def test_never_downgrade_no_successor_is_created_when_the_running_updater_is_newer(tmp_path):
    with World(tmp_path, fleet=True) as w:
        w.fake.images[ref(UPD, C)] = w.fake.registry[ref(UPD, C)]
        # The running updater is C's, beside an app at A.
        image = w.fake.images[ref(UPD, C)]
        w.fake.containers[w.updater_id]["_inspect"]["Image"] = image["Id"]
        w.fake.containers[w.updater_id]["ImageID"] = image["Id"].removeprefix("sha256:")
        boot(w)
        req, record = apply(w)
        assert record["state"] == "succeeded" and w.ledger.stamp == B
        assert "The updater stays on 0.9.0, which is newer." in record["notes"]
        assert not any(c.query.get("name") == NEXT for c in w.fake.calls if c.bare == "/containers/create")
        assert pin.read(w.project_dir)[pin.UPDATER_KEY] == f"{UPD}:{C}@{digest(UPD, C)}"
        # And an updater-only refresh to B is refused before anything is pulled.
        refused, answer = update_updater(w, B)
        assert answer["state"] == "refused" and answer["code"] == "updater_not_newer"


def test_an_updater_already_on_the_targets_image_does_not_hand_over_to_a_copy_of_itself(world):
    """#258: an updater-only update to B, then the app A -> B while A's updater is on standby."""
    update_updater(world, B)
    b_updater = named(world, CANONICAL)["Id"]
    assert named(world, PREVIOUS)["Id"] == world.updater_id and world.is_running(world.updater_id)
    calls_before = len(world.fake.calls)

    req, record = apply(world, B)
    assert record["state"] == "succeeded" and world.ledger.stamp == B
    steps = [s["step"] for s in world.journal(req["id"]).started]
    assert "2a" not in steps and "10" not in steps
    assert world.journal(req["id"]).context["updater_digest"] == digest(UPD, B) != digest(UPD, A)
    assert f"The updater stays on {B}: it is already the updater of {B}." in record["notes"]
    assert not any("takes over" in n for n in record["notes"])
    # No -next was made; B's updater is still the one, and A's -- still on its
    # standby, the one a take-back would need -- is still -previous.
    assert named(world, NEXT) is None
    assert not any(
        c.query.get("name") == NEXT
        for c in world.fake.calls[calls_before:]
        if c.bare == "/containers/create"
    )
    assert named(world, CANONICAL)["Id"] == b_updater
    assert named(world, PREVIOUS)["Id"] == world.updater_id and world.is_running(world.updater_id)
    assert world.fleet[world.updater_id].mode == "standby"
    assert pin.read(world.project_dir)[pin.UPDATER_KEY] == f"{UPD}:{B}@{digest(UPD, B)}"
    # And a refresh of the updater to the release it already runs is refused.
    _, answer = update_updater(world, B)
    assert answer["state"] == "refused" and answer["code"] == "updater_not_newer"
    world.run_for(STANDBY_SECONDS + 30)
    assert assert_exactly_one_current(world).image_digest == digest(UPD, B)


def test_the_updaters_identity_is_its_digest_not_its_version():
    """#258: the same image is never handed over to; a rebuild at the same version still is."""
    me = Owner(digest(UPD, B), B, CANONICAL)
    same = Owner(digest(UPD, B), B, "")
    rebuilt = Owner(digest(UPD, C), B, "")
    newer = Owner(digest(UPD, C), C, "")
    assert is_me(me, same) and not is_me(me, rebuilt) and not is_me(me, newer)
    assert not is_me(Owner("", "0.0.0", ""), Owner("", B, ""))
    assert goes_first(me, newer, (1, 1), 1) and not goes_first(me, same, (1, 1), 1)
    # Step 10 is what takes a rebuild at the same version: it is not newer, so not first.
    assert not goes_first(me, rebuilt, (1, 1), 1)


# --------------------------------------------------------------------------- #
# Standby take-back mid-apply (H6, R14)
# --------------------------------------------------------------------------- #


def test_a_successor_dying_mid_apply_is_rolled_back_by_the_old_updater_not_carried_forward(world, monkeypatch):
    real = journal.save
    died = []

    def save(vol, j):
        real(vol, j)
        if j.step == "7" and j.owner is not None and j.owner.version == B and not died:
            died.append(j.step)
            raise Killed("U2 died at step 7")

    monkeypatch.setattr(journal, "save", save)
    real_crash = world.crash
    world.crash = lambda cid, restart=True: real_crash(cid, restart=cid == world.updater_id)  # type: ignore[method-assign]
    report = prepare(world)
    req = world.apply_request(report)
    world.write_request(req)
    world.run_for(60)

    assert died == ["7"] and world.ledger.drills == 1
    record = world.history(req["id"])
    # B's migration had succeeded; A's updater does not carry B's apply
    # forward: it rolls back, and the ledger is restored.
    assert record is not None and record["state"] == "rolled_back", record
    assert world.ledger.stamp == A and world.ledger.aside == [B]
    j = world.journal(req["id"])
    assert "R2" in [s["step"] for s in j.started] and j.owner.version == B
    app = named(world, world.app_name)
    assert app["Id"] == world.app_id and app["State"] == "running"
    assert load(world.volume, req["id"])["outcome"] == "taken_back"
    assert any("took back over" in n for n in record["notes"])
    world.no_running_app_at_a_mismatched_stamp()
    assert assert_exactly_one_current(world).version == A


def test_a_successor_dying_right_after_taking_the_apply_gives_it_back_and_step_10_tries_again(world):
    world.kill_at = ("H5", "U2")
    first = []
    real = world.crash

    def crash(cid, restart=True):
        # The first successor stays down; the one step 10 starts is healthy.
        first.append(cid)
        real(cid, restart=False)

    world.crash = crash  # type: ignore[method-assign]
    req, record = apply(world, seconds=180)
    assert record["state"] == "succeeded" and world.ledger.stamp == B
    j = world.journal(req["id"])
    owners = [(o["owner"]["version"], o["from_step"]) for o in j.owners]
    # A, B at 2a, A again at 2a (taken back before step 3), and A ran it.
    assert owners == [(A, "0"), (B, "2a"), (A, "2a")]
    steps = [s["step"] for s in j.started]
    assert steps.count("3") == 1 and steps[-1] == "10"
    assert first and first[0] not in world.fake.containers  # the dead -next, removed at the retry's H2
    world.run_for(STANDBY_SECONDS + 30)
    assert assert_exactly_one_current(world).version == B


# --------------------------------------------------------------------------- #
# The -previous lifecycle (row 28)
# --------------------------------------------------------------------------- #


def test_a_previous_started_by_hand_takes_over_from_a_broken_successor_after_two_minutes(world):
    update_updater(world, C)
    world.run_for(STANDBY_SECONDS + 30)
    u2 = named(world, CANONICAL)["Id"]
    assert not world.is_running(world.updater_id)
    world.crash(u2, restart=False)  # crash-looping: no heartbeat any more
    died_at = world.time.t
    world.run_for(30)
    world.start_container(world.updater_id)  # Desktop: start spend-tracker-updater-1-previous
    world.run_for(60)
    assert named(world, PREVIOUS)["Id"] == world.updater_id, "not before two minutes of silence"
    assert world.fleet[world.updater_id].mode == "previous" and lock_holder(world.volume).version == C
    world.run_for(60)
    assert world.time.t - died_at > 120
    assert named(world, CANONICAL)["Id"] == world.updater_id and named(world, NEXT)["Id"] == u2
    assert lock_holder(world.volume).version == A
    assert pin.read(world.project_dir)[pin.UPDATER_KEY].endswith(digest(UPD, A))
    assert assert_exactly_one_current(world).version == A


def test_a_previous_started_beside_a_healthy_updater_leaves_it_alone_and_stops(world):
    update_updater(world, C)
    world.run_for(STANDBY_SECONDS + 30)
    u2 = named(world, CANONICAL)["Id"]
    world.start_container(world.updater_id)
    world.run_for(4 * 60)
    assert world.fleet[world.updater_id].mode == "previous" and world.is_running(world.updater_id)
    world.run_for(90)
    assert not world.is_running(world.updater_id) and named(world, PREVIOUS)["Id"] == world.updater_id
    assert named(world, CANONICAL)["Id"] == u2
    assert assert_exactly_one_current(world).version == C


# --------------------------------------------------------------------------- #
# The protocol window (C2, C4, R8) and `outdated` (C3, row 31)
# --------------------------------------------------------------------------- #


def test_update_updater_is_refused_when_the_target_does_not_speak_the_apps_protocol(tmp_path):
    with World(tmp_path, fleet=True, updater_protocols="2-3") as w:
        boot(w)
        req, record = update_updater(w, C)
        assert record["state"] == "refused"
        assert record["sentence"] == (
            "Updating the updater failed: the updater of 0.9.0 reads protocols 2 to 3, "
            "and the app writes protocol 1."
        )
        assert ref(UPD, C) not in w.fake.images
        assert not any(c.bare == "/containers/create" for c in w.fake.calls)
        assert load(w.volume, req["id"]) is None
        assert assert_exactly_one_current(w).version == A


def test_an_outdated_updater_still_updates_itself_with_only_the_handover_calls(tmp_path):
    with World(tmp_path, fleet=True) as w:
        w.fake.version_doc = engine_fixture("docker-future")
        boot(w)
        assert w.fleet[w.updater_id].kit.client.state == "outdated"
        refused = send(w, w.prepare_request(B), 6)
        assert refused["state"] == "refused" and refused["code"] == "outdated"
        start = len(w.fake.calls)
        req, record = update_updater(w, C)
        assert record["state"] == "succeeded", record
        made = w.fake.calls[start:]
        names = set()
        for call in made:
            endpoint = eng.allowed(call.method, call.path.split("?")[0])
            assert endpoint is not None
            names.add(endpoint.name)
        assert names <= eng.OUTDATED_ALLOWED, names - eng.OUTDATED_ALLOWED
        assert {c.version for c in made if c.version} == {"1.53"}
        assert named(w, CANONICAL)["Id"] == w.successor_id()
        w.run_for(STANDBY_SECONDS + 30)
        assert assert_exactly_one_current(w).version == C


# --------------------------------------------------------------------------- #
# Small parts
# --------------------------------------------------------------------------- #


def test_who_is_current_follows_u1s_journal():
    pred = Owner(digest(UPD, A), A, CANONICAL).to_dict()
    succ = Owner(digest(UPD, B), B, NEXT).to_dict()
    doc = {"predecessor": pred, "successor": succ, "outcome": None}
    theirs = {"role": "current", "image_digest": digest(UPD, B)}
    mine = {"role": "current", "image_digest": digest(UPD, A)}
    assert current_side(doc, False, theirs) == "predecessor"
    assert current_side(doc, True, mine) == "predecessor"
    assert current_side(doc, True, theirs) == "successor"
    assert current_side({**doc, "outcome": "taken_back"}, True, theirs) == "predecessor"
    assert current_side({**doc, "outcome": "done"}, False, None) == "successor"


def test_the_handover_files_stay_protocol_1_and_carry_no_code(world):
    req, _ = update_updater(world, B)
    for part in ("request", "ready", "go"):
        doc = json.loads(world.volume.handover(req["id"], part).read_text())
        assert doc["protocol"] == contract.FROZEN_PROTOCOL and doc["id"] == req["id"]
    assert not list((world.volume.root / "handover").glob("*.check-*"))


# --------------------------------------------------------------------------- #
# An image with two digests: the index's and the platform manifest's (#287)
# --------------------------------------------------------------------------- #

TWO_ORDERS = pytest.mark.parametrize("order", ["index_first", "platform_first"])


def two_digests(version: str, order: str) -> tuple[str, str]:
    pair = (digest(UPD, version), platform_digest(UPD, version))
    return pair if order == "index_first" else pair[::-1]


@TWO_ORDERS
def test_an_updater_is_every_digest_its_image_carries_not_the_last_one(order):
    """0.9.2 took the last `RepoDigests` entry for its own, and on Podman that
    is the platform manifest's: the successor refused itself, and no updater
    could hand over. Either digest of the image is the same updater."""
    digests = two_digests(B, order)
    me = Owner(digests[0], B, CANONICAL, digests=digests)
    index, platform = Owner(digest(UPD, B), B, ""), Owner(platform_digest(UPD, B), B, "")
    rebuilt, newer = Owner(digest(UPD, C), B, ""), Owner(digest(UPD, C), C, "")
    assert is_me(me, index) and is_me(me, platform)
    assert not is_me(me, rebuilt) and not is_me(me, newer)
    # 2a: never first to a copy of itself, whichever digest the target names.
    assert not goes_first(me, index, (1, 1), 1) and not goes_first(me, platform, (1, 1), 1)
    assert goes_first(me, newer, (1, 1), 1)
    # An Owner read back from a file has the one digest it was written with.
    assert me.is_(Owner.from_dict(platform.to_dict())) and me.is_(Owner.from_dict(index.to_dict()))
    assert not me.is_(Owner.from_dict(newer.to_dict()))
    assert me.preferring(digest(UPD, B)).image_digest == digest(UPD, B)
    assert me.preferring(digest(UPD, C)).image_digest == digests[0]  # not its image: unchanged
    # The file form is unchanged: one digest, the one it writes.
    assert me.preferring(digest(UPD, B)).to_dict() == {
        "image_digest": digest(UPD, B), "version": B, "container": CANONICAL,
    }  # fmt: skip


@TWO_ORDERS
def test_identity_of_reads_every_updater_digest_and_names_itself_as_its_container_was_created(order):
    from updater.handover import identity_of, own_digests

    digests = two_digests(B, order)
    image = {
        "RepoDigests": [
            f"{APP}@{digest(APP, B)}",
            *(f"{UPD}@{d}" for d in digests),
            f"{UPD}@{digests[0]}",
            "localhost/other@sha256:" + "9" * 64,
        ]
    }
    assert own_digests(image) == digests
    # The bundle's compose file names the index digest: that is the one it writes.
    by_digest = {"Config": {"Image": f"{UPD}:{B}@{digest(UPD, B)}"}}
    me = identity_of(image, by_digest, B, CANONICAL)
    assert me.image_digest == digest(UPD, B) and me.ids == {digest(UPD, B), platform_digest(UPD, B)}
    # Started by tag: the engine's first, and still both are its identity.
    by_tag = {"Config": {"Image": f"{UPD}:{B}"}}
    me = identity_of(image, by_tag, B, CANONICAL)
    assert me.image_digest == digests[0] and me.ids == set(digests)
    # Created from another updater's digest (compose with an older pin): not taken.
    other = {"Config": {"Image": f"{UPD}:{A}@{digest(UPD, A)}"}}
    assert identity_of(image, other, B, CANONICAL).image_digest == digests[0]
    assert identity_of({}, by_tag, B, CANONICAL).ids == frozenset()


@TWO_ORDERS
def test_known_as_prefers_the_verified_digest_then_the_pin(tmp_path, order):
    from updater.handover import known_as

    vol = volume.Volume(tmp_path / "update")
    vol.init()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SPENDTRACKER_VERSION=0.8.0\n")
    digests = two_digests(B, order)
    me = Owner(digests[0], B, NEXT, digests=digests)
    # No handover, no pin: as it came.
    assert known_as(me, vol, project, None).image_digest == digests[0]
    # The pin names the platform digest (written by 0.9.2 on Podman): the pin's.
    pin.write_updater(project, f"{UPD}:{B}@{platform_digest(UPD, B)}")
    assert known_as(me, vol, project, None).image_digest == platform_digest(UPD, B)
    # A pin naming another updater is not this one's name.
    pin.write_updater(project, f"{UPD}:{A}@{digest(UPD, A)}")
    assert known_as(me, vol, project, None).image_digest == digests[0]
    # A successor names itself by the digest its handover verified, above the pin.
    pin.write_updater(project, f"{UPD}:{B}@{platform_digest(UPD, B)}")
    rid = "0b7a3c9e-5d2f-4c1a-9e8b-1f2d3c4b5a69"
    succ = Owner(digest(UPD, B), B, NEXT).to_dict()
    journal.write_handover(vol, rid, "request", {"successor": succ}, time.time())
    known = known_as(me, vol, project, rid)
    assert known.image_digest == digest(UPD, B) and known.ids == set(digests)


@pytest.mark.parametrize("multiarch", ["index_first", "platform_first"])
def test_identify_reads_both_digests_from_the_engine(tmp_path, multiarch):
    from updater.__main__ import identify

    with World(tmp_path, multiarch=multiarch) as w:
        w.kit()
        assert w.client is not None
        me, update_volume = identify(w.client, mountinfo="", hostname=w.updater_id[:12])
        assert me.container == CANONICAL and me.version == A and update_volume == f"{PROJECT}_update"
        assert me.ids == {digest(UPD, A), platform_digest(UPD, A)}
        # Its container was created by the index digest, as the bundle names it.
        assert me.image_digest == digest(UPD, A)
        listed = w.fake.images[ref(UPD, A)]["RepoDigests"]
        assert listed[-1 if multiarch == "index_first" else 0] == f"{UPD}@{platform_digest(UPD, A)}"


@pytest.fixture(params=["index_first", "platform_first"])
def podman_world(tmp_path, request):
    with World(tmp_path, fleet=True, multiarch=request.param) as w:
        boot(w)
        yield w


def test_a_successor_whose_image_also_carries_the_platform_digest_passes_its_own_check(podman_world):
    w = podman_world
    req, record = update_updater(w, C)
    assert record["state"] == "succeeded", record
    ready = journal.read_handover(w.volume, req["id"], "ready")
    assert ready["ok"] is True and ready["problem"] is None
    assert ready["image_digest"] == digest(UPD, C)
    assert ready["image_digests"] == sorted([digest(UPD, C), platform_digest(UPD, C)])
    u2 = w.fleet[w.successor_id()]
    assert u2.me.image_digest == digest(UPD, C) and u2.kit.site.me.image_digest == digest(UPD, C)
    # The heartbeat, the lock and the pin name the index: what the release published.
    assert beat(w)["image_digest"] == digest(UPD, C) and beat(w)["role"] == "current"
    assert lock_holder(w.volume).image_digest == digest(UPD, C)
    assert pin.read(w.project_dir)[pin.UPDATER_KEY] == f"{UPD}:{C}@{digest(UPD, C)}"
    w.run_for(STANDBY_SECONDS + 30)
    assert load(w.volume, req["id"])["outcome"] == "done"
    assert assert_exactly_one_current(w).image_digest == digest(UPD, C)


def test_the_successors_self_check_refuses_an_image_carrying_neither_digest(podman_world):
    w = podman_world
    w.lying_digest = platform_digest(UPD, A)
    req, record = update_updater(w, C)
    assert record["state"] == "not_started"
    assert f"it runs {platform_digest(UPD, A)[:19]}, not {digest(UPD, C)[:19]}" in record["sentence"]
    assert named(w, NEXT) is None and assert_exactly_one_current(w).version == A
    assert pin.UPDATER_KEY not in pin.read(w.project_dir)


def as_092(monkeypatch, version: str = A) -> None:
    """Updaters of `version` know themselves as 0.9.2 did: by the last entry alone."""
    import tests.updater_world as world_module
    from updater.handover import identity_of

    def last_entry(image, seen, v, container):
        me = identity_of(image, seen, v, container)
        if v != version:
            return me
        listed = [r.split("@", 1)[1] for r in image.get("RepoDigests") or [] if r.startswith(UPD + "@")]
        return Owner(listed[-1], v, container)

    monkeypatch.setattr(world_module, "identity_of", last_entry)


def test_a_092_updater_hands_over_to_a_fixed_successor_first(podman_world, monkeypatch):
    """The release that fixes #287 goes first from a 0.9.2 updater on Podman.

    U1 knows itself by one digest -- the platform's, where the engine lists it
    last -- so it is never the target's index digest: 2a goes first. The fixed
    successor checks membership and answers with the verified index digest,
    which is exactly what U1 compares, by its word and the engine's.
    """
    w = podman_world
    w.fleet.clear()
    as_092(monkeypatch)
    boot(w)
    u1 = w.fleet[w.updater_id]
    expected_own = digest(UPD, A) if w.multiarch == "platform_first" else platform_digest(UPD, A)
    assert u1.me.ids == {expected_own}
    req, record = apply(w)
    assert record["state"] == "succeeded" and w.ledger.stamp == B
    j = w.journal(req["id"])
    assert [(o["owner"]["version"], o["from_step"]) for o in j.owners] == [(A, "0"), (B, "2a")]
    assert j.owners[-1]["owner"]["image_digest"] == digest(UPD, B)
    assert pin.read(w.project_dir)[pin.UPDATER_KEY] == f"{UPD}:{B}@{digest(UPD, B)}"
    assert beat(w)["image_digest"] == digest(UPD, B)
    w.run_for(STANDBY_SECONDS + 30)
    assert load(w.volume, req["id"])["outcome"] == "done"
    assert assert_exactly_one_current(w).version == B


# --------------------------------------------------------------------------- #
# A gap during the standby (#288)
# --------------------------------------------------------------------------- #


def standby_clock(w: World) -> list[float]:
    """The monotonic time of U1's H6 write -- the standby's start -- once it happens."""
    started: list[float] = []
    real = w._wrote

    def wrote(step, cid):
        if step == "H6" and w.who(cid) == "U1" and not started:
            started.append(w.time.t)
        real(step, cid)

    w._wrote = wrote  # type: ignore[method-assign]
    return started


def until_settled(w: World, rid: str, limit: float) -> float:
    """Run until U1's handover has an outcome; the monotonic time it took."""
    t0 = w.time.t
    while (load(w.volume, rid) or {}).get("outcome") is None and w.time.t - t0 < limit:
        w.run_for(2)
    return w.time.t


def test_a_shutdown_during_the_standby_does_not_take_back_on_boot(world):
    """Lab VM 314: shut down four minutes into the standby, booted an hour later.
    The standby restarted into a successor heartbeat an hour old, took back,
    stopped the healthy successor and pinned the older updater."""
    started = standby_clock(world)
    req, record = update_updater(world, C)
    assert record["state"] == "succeeded" and started
    u2 = world.successor_id()
    pinned = pin.read(world.project_dir)
    world.run_for(120)
    # Off: every process dies, for an hour. On boot the engine starts U1 at
    # once; U2's container starts 5 s later.
    world.engine_restart()
    world.pending.remove(u2)
    world.fake.set_state(world.fake.containers[u2], "exited", 0)
    world.time.doze(3600)
    world.run_for(5)
    assert world.fleet[world.updater_id].mode == "standby"
    world.start_container(u2)
    world.run_for(60)

    doc = load(world.volume, req["id"])
    assert doc["outcome"] is None and doc["step"] == "H6"
    assert world.fleet[world.updater_id].mode == "standby" and world.is_running(world.updater_id)
    assert named(world, CANONICAL)["Id"] == u2 and world.is_running(u2)
    assert pin.read(world.project_dir) == pinned
    assert lock_holder(world.volume).version == C
    # H7 after ten minutes of running standby -- the hour off not among them
    # (what it ran is saved every 30 s, so up to that is run again).
    done = until_settled(world, req["id"], STANDBY_SECONDS)
    ran = done - started[0]
    assert STANDBY_SECONDS <= ran <= STANDBY_SECONDS + 30 + 10, ran
    doc = load(world.volume, req["id"])
    assert doc["outcome"] == "done" and not world.is_running(world.updater_id)
    assert pin.read(world.project_dir) == pinned
    assert world.history(req["id"])["state"] == "succeeded"
    assert assert_exactly_one_current(world).version == C


def test_a_sleep_during_the_standby_does_not_take_back_on_wake(world):
    """The laptop case: both processes live through an hour asleep, and the
    successor's first heartbeat after it comes 5 s after the wake."""
    started = standby_clock(world)
    req, record = update_updater(world, C)
    assert record["state"] == "succeeded" and started
    u2 = world.fleet[world.successor_id()]
    pinned = pin.read(world.project_dir)
    world.run_for(120)
    real = u2.beat.tick
    wake = world.time.t + 5
    u2.beat.tick = lambda now: real(now) if world.time.t >= wake else None  # type: ignore[method-assign]
    world.time.doze(3600)
    world.run_for(90)

    assert load(world.volume, req["id"])["outcome"] is None
    assert world.fleet[world.updater_id].mode == "standby"
    assert named(world, CANONICAL)["Id"] == u2.cid and world.is_running(u2.cid)
    assert pin.read(world.project_dir) == pinned
    done = until_settled(world, req["id"], STANDBY_SECONDS)
    assert STANDBY_SECONDS <= done - started[0] <= STANDBY_SECONDS + 10
    assert load(world.volume, req["id"])["outcome"] == "done"
    assert pin.read(world.project_dir) == pinned
    assert assert_exactly_one_current(world).version == C


def test_after_a_shutdown_a_successor_that_never_comes_back_is_still_taken_back_from(world):
    """The grace after a return is the stale window, not a pass: U2 down for good is taken back."""
    req, record = update_updater(world, C)
    assert record["state"] == "succeeded"
    u2 = world.successor_id()
    world.run_for(120)
    world.engine_restart()
    world.pending.remove(u2)
    world.stay_down.add(u2)
    world.fake.set_state(world.fake.containers[u2], "exited", 0)
    world.time.doze(3600)
    world.run_for(60)
    assert load(world.volume, req["id"])["outcome"] is None
    world.run_for(60 + GONE_GRACE_SECONDS + 10)
    assert load(world.volume, req["id"])["outcome"] == "taken_back"
    assert named(world, CANONICAL)["Id"] == world.updater_id
    assert pin.read(world.project_dir)[pin.UPDATER_KEY].endswith(digest(UPD, A))
    assert assert_exactly_one_current(world).version == A
