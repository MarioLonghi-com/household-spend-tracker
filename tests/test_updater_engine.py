"""The updater's engine client, against a recording fake engine (design notes 6.2, 15.1).

U2  the client refuses privileged and host-reaching container shapes, even
    when asked, and the engine receives nothing;
U3  containers outside the project are never touched (two projects);
U6  the paths the client can call are exactly the 6.2 table;
U12 API negotiation (C3) against recorded and written-down `/version` windows.

The fixtures under `tests/fixtures/updater/engines/`: `docker-desktop` and
`podman-machine` were recorded from real engines (Docker Desktop 4.93 with
Engine 29.8.1, and Podman 6.1.3 in `podman machine`) and scrubbed of anything
naming the machine; `docker-28`, `docker-29.0`, `podman-4.9` and
`docker-future` (a floor above the tested window) are written by hand from
those engines' published windows.
"""

from __future__ import annotations

import pytest

from tests.updater_fake_engine import FakeEngine, Running, engine_fixture
from updater import engine
from updater.engine import EngineClient, NotAllowed, Scope

PROJECT = "spend-tracker"
OTHER = "someone-else"
SOCKET_HOST_PATH = "/var/run/docker.sock"
PROJECT_DIR = "/srv/spend-tracker"
APP_IMAGE = "ghcr.io/mariolonghi-com/household-spend-tracker@sha256:" + "a" * 64
UPDATER_IMAGE = "ghcr.io/mariolonghi-com/household-spend-tracker-updater@sha256:" + "b" * 64


def scope(**kw) -> Scope:
    return Scope(project=PROJECT, bind_sources=(SOCKET_HOST_PATH, PROJECT_DIR), **kw)


@pytest.fixture
def world():
    """Two projects, two containers each, and a sidecar in ours."""
    fake = FakeEngine(engine_fixture("docker-desktop"), engine_fixture("docker-desktop", "info"))
    fake.add_container("spend-tracker-app-1", PROJECT, labels={"com.docker.compose.service": "app"})
    fake.add_container("spend-tracker-updater-1", PROJECT, labels={"com.docker.compose.service": "updater"})
    fake.add_container("spend-tracker-tailscale-1", PROJECT)
    # podman-compose labels its containers with its own scheme as well (S5).
    fake.add_container("spend-tracker_maintenance_1", PROJECT, label="io.podman.compose.project")
    fake.add_container("other-db-1", OTHER)
    fake.add_container("other-web-1", OTHER)
    with Running(fake) as running:
        client = EngineClient(running.socket_path, scope(sidecar="spend-tracker-tailscale-1"))
        client.negotiate()
        fake.calls.clear()
        yield fake, client


DRILL_LABELS = {
    "com.docker.compose.project": PROJECT,
    "com.docker.compose.oneoff": "True",
    engine.ROLE_LABEL: "drill",
    engine.REQUEST_LABEL: "5d3c0b8e-7f0a-4b8e-9f43-0f6f1a2b9c11",
}


def good_body(**host) -> dict:
    """A drill one-off: a named volume, no network, no host path."""
    return {
        "Image": APP_IMAGE,
        "Labels": dict(DRILL_LABELS),
        "HostConfig": {
            "Binds": ["spend-tracker_data:/data"],
            "NetworkMode": "none",
            **host,
        },
    }


def successor_body(**host) -> dict:
    """Not a one-off: the successor updater, which may bind the socket and the project directory."""
    return {
        "Image": UPDATER_IMAGE,
        "Labels": {"com.docker.compose.project": PROJECT},
        "HostConfig": {
            "Binds": ["spend-tracker_update:/update", f"{SOCKET_HOST_PATH}:/run/engine.sock"],
            **host,
        },
    }


# --------------------------------------------------------------------------- #
# U6: exactly the 6.2 table
# --------------------------------------------------------------------------- #

#: 6.2, spelled out by hand -- plus the two exec calls without which a probe
#: is created and never run. A new entry in `ENDPOINTS` fails this until it is
#: added here too, deliberately.
DESIGN_TABLE = {
    ("GET", "/_ping", False),
    ("GET", "/version", False),
    ("GET", "/info", True),
    ("GET", "/containers/json", True),
    ("GET", "/containers/{id}/json", True),
    ("POST", "/containers/create", True),
    ("POST", "/containers/{id}/start", True),
    ("POST", "/containers/{id}/stop", True),
    ("POST", "/containers/{id}/rename", True),
    # Added in #169: the app's restart policy, parked at `no` while it is
    # `-previous` and put back on a rollback. Nothing else (`guard_update`).
    ("POST", "/containers/{id}/update", True),
    ("DELETE", "/containers/{id}", True),
    ("POST", "/containers/{id}/exec", True),
    ("POST", "/exec/{id}/start", True),
    ("GET", "/exec/{id}/json", True),
    # Added in #161: the check's JSON and the floors' figures arrive on a
    # one-off's standard output, and nothing else's output is read.
    ("GET", "/containers/{id}/logs", True),
    ("POST", "/images/create", True),
    ("GET", "/images/{image}/json", True),
    ("DELETE", "/images/{image}", True),
}


def test_the_callable_endpoints_are_exactly_the_design_table():
    assert set(engine.endpoints_table()) == DESIGN_TABLE
    assert len(engine.ENDPOINTS) == len(DESIGN_TABLE)


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/v1.52/build"),
        ("POST", "/v1.52/volumes/create"),
        ("DELETE", "/v1.52/volumes/spend-tracker_data"),
        ("POST", "/v1.52/networks/create"),
        ("POST", "/v1.52/containers/prune"),
        ("POST", "/v1.52/images/prune"),
        ("POST", "/v1.52/plugins/pull"),
        ("POST", "/v1.52/containers/abc/kill"),
        # Listed, but only with a restart policy for a body (#169): bare, refused.
        ("POST", "/v1.52/containers/abc/update"),
        ("GET", "/v1.52/containers/abc/archive"),
        ("PUT", "/v1.52/containers/abc/archive"),
        ("POST", "/v1.52/swarm/init"),
        ("POST", "/containers/create"),  # unversioned mutating
        ("GET", "/containers/json"),  # unversioned where the table says versioned
        ("GET", "/v1.52/version"),  # versioned where the table says unversioned
    ],
)
def test_the_client_cannot_reach_anything_else(world, method, path):
    fake, client = world
    with pytest.raises(NotAllowed):
        client._send(method, path)
    assert fake.calls == []


def test_every_call_the_client_makes_is_in_the_table_and_versioned_when_it_mutates(world):
    fake, client = world
    client.ping()
    client.info()
    client.containers()
    client.inspect("spend-tracker-app-1")
    cid = client.create("spend-tracker-drill", good_body())
    client.start(cid)
    client.logs("spend-tracker-drill")
    client.probe("spend-tracker-app-1", ["python", "-c", "print(1)"])
    client.probe("spend-tracker-tailscale-1", ["wget", "-q", "-O-", "http://127.0.0.1:8848/api/health"])
    client.stop("spend-tracker-drill", grace=5)
    client.rename("spend-tracker-drill", "spend-tracker-drill-old")
    client.set_restart_policy("spend-tracker-app-1", {"Name": "no"})
    client.remove("spend-tracker-drill-old")
    client.pull(APP_IMAGE)
    client.inspect_image(APP_IMAGE)
    client.remove_image(APP_IMAGE)

    seen = {engine.allowed(c.method, c.path.split("?")[0]) for c in fake.calls}
    assert None not in seen
    assert {e.name for e in seen} == {e.name for e in engine.ENDPOINTS} - {"version"}
    for call in fake.calls:
        if call.method != "GET":
            assert call.version == "1.52", call.path
    # And what they did, on the fake's side: the drill came and went.
    assert all("/spend-tracker-drill" not in c["Names"] for c in fake.containers.values())
    assert APP_IMAGE not in fake.images


# --------------------------------------------------------------------------- #
# U2: the create guard
# --------------------------------------------------------------------------- #

DANGEROUS = {
    "privileged": {"Privileged": True},
    "cap_add": {"CapAdd": ["SYS_ADMIN"]},
    "host_root_bind": {"Binds": ["/:/host"]},
    "host_etc_bind": {"Binds": ["/etc:/etc:ro"]},
    "relative_bind": {"Binds": ["./secrets:/s"]},
    "bind_mount": {"Mounts": [{"Type": "bind", "Source": "/home", "Target": "/h"}]},
    "npipe_mount": {"Mounts": [{"Type": "npipe", "Source": "x", "Target": "/x"}]},
    "pid_host": {"PidMode": "host"},
    "network_host": {"NetworkMode": "host"},
    "ipc_host": {"IpcMode": "host"},
    "userns_host": {"UsernsMode": "host"},
    "devices": {"Devices": [{"PathOnHost": "/dev/sda", "PathInContainer": "/dev/sda"}]},
    "device_requests": {"DeviceRequests": [{"Count": -1}]},
    "volumes_from": {"VolumesFrom": ["spend-tracker-updater-1"]},
    "seccomp_unconfined": {"SecurityOpt": ["seccomp=unconfined"]},
}


@pytest.mark.parametrize("name", sorted(DANGEROUS))
def test_a_host_reaching_shape_is_refused_and_the_engine_hears_nothing(world, name):
    fake, client = world
    before = set(fake.containers)
    with pytest.raises(NotAllowed):
        client.create("spend-tracker-drill", good_body(**DANGEROUS[name]))
    assert fake.calls == []
    assert set(fake.containers) == before


@pytest.mark.parametrize(
    "body",
    [
        {**good_body(), "Image": "docker.io/library/alpine:latest"},
        {**good_body(), "Image": "ghcr.io/mariolonghi-com/household-spend-tracker:0.9.0"},
        {**good_body(), "Labels": {"com.docker.compose.project": OTHER}},
        {**good_body(), "Labels": {}},
    ],
)
def test_a_foreign_image_or_a_container_outside_the_project_is_not_created(world, body):
    fake, client = world
    before = set(fake.containers)
    with pytest.raises(NotAllowed):
        client.create("spend-tracker-drill", body)
    assert fake.calls == []
    assert set(fake.containers) == before


def test_the_allowed_shapes_are_created_with_the_body_unchanged(world):
    fake, client = world
    # The successor updater with the socket and the project directory, and a
    # drill with a named volume and no network.
    successor = successor_body(
        SecurityOpt=["label=disable", "no-new-privileges:true"],
        Mounts=[{"Type": "bind", "Source": PROJECT_DIR, "Target": "/project"}],
    )
    drill = good_body(Mounts=[{"Type": "volume", "Source": "spend-tracker_ledger", "Target": "/l"}])
    first = client.create("spend-tracker-updater-1-next", successor)
    second = client.create("spend-tracker-drill", drill)
    assert fake.containers[first]["Config"] == successor
    assert fake.containers[second]["Config"] == drill
    assert [c.query["name"] for c in fake.calls if c.bare == "/containers/create"] == [
        "spend-tracker-updater-1-next",
        "spend-tracker-drill",
    ]


# --------------------------------------------------------------------------- #
# U3: two projects
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ref", ["other-db-1", "other-web-1"])
@pytest.mark.parametrize("op", ["inspect", "start", "stop", "rename", "remove", "probe"])
def test_a_container_of_another_project_is_never_touched(world, ref, op):
    fake, client = world
    foreign = fake.by_name(ref)
    state = foreign["State"]
    with pytest.raises(NotAllowed):
        if op == "rename":
            client.rename(ref, "taken")
        elif op == "probe":
            client.probe(ref, ["python", "-c", "1"])
        else:
            getattr(client, op)(ref)
    # Only the project-filtered listings went out; nothing named the container.
    assert all(c.bare == "/containers/json" for c in fake.calls)
    assert all(foreign["Id"] not in c.path for c in fake.calls)
    assert foreign["State"] == state and foreign["Names"] == [f"/{ref}"]


def test_the_listing_holds_both_label_schemes_and_neither_foreign_container(world):
    fake, client = world
    names = sorted(c["Names"][0] for c in client.containers())
    assert names == [
        "/spend-tracker-app-1",
        "/spend-tracker-tailscale-1",
        "/spend-tracker-updater-1",
        "/spend-tracker_maintenance_1",
    ]


def test_project_containers_are_acted_on(world):
    fake, client = world
    client.stop("spend-tracker-app-1")
    client.rename("spend-tracker-app-1", "spend-tracker-app-1-previous")
    client.stop("spend-tracker_maintenance_1")
    assert fake.by_name("spend-tracker-app-1-previous")["State"] == "exited"
    assert fake.by_name("spend-tracker_maintenance_1")["State"] == "exited"


@pytest.mark.parametrize("op", ["start", "stop", "remove"])
def test_the_sidecar_is_never_stopped_started_or_removed(world, op):
    fake, client = world
    sidecar = fake.by_name("spend-tracker-tailscale-1")
    with pytest.raises(NotAllowed):
        getattr(client, op)("spend-tracker-tailscale-1")
    with pytest.raises(NotAllowed):
        client.rename("spend-tracker-tailscale-1", "moved")
    with pytest.raises(NotAllowed):
        client.probe("spend-tracker-tailscale-1", ["python", "-c", "1"])
    assert sidecar["State"] == "running" and sidecar["Names"] == ["/spend-tracker-tailscale-1"]
    assert all(c.method == "GET" for c in fake.calls)


@pytest.mark.parametrize(
    "ref",
    [
        "ghcr.io/mariolonghi-com/household-spend-tracker:latest",
        "docker.io/library/python@sha256:" + "c" * 64,
        "ghcr.io/mariolonghi-com/household-spend-tracker-evil@sha256:" + "c" * 64,
    ],
)
def test_only_this_repositorys_images_by_digest(world, ref):
    fake, client = world
    for op in (client.pull, client.inspect_image, client.remove_image):
        with pytest.raises(NotAllowed):
            op(ref)
    assert fake.calls == []
    assert fake.images == {}


# --------------------------------------------------------------------------- #
# U12: API negotiation
# --------------------------------------------------------------------------- #

WINDOWS = {
    # fixture: (engine window, version spoken, state)
    "podman-4.9": ("1.24-1.41", "1.41", "ok"),
    "podman-machine": ("1.24-1.44", "1.44", "ok"),
    "docker-28": ("1.24-1.51", "1.51", "ok"),
    "docker-29.0": ("1.44-1.52", "1.52", "ok"),
    "docker-desktop": ("1.40-1.56", "1.52", "ok"),
    "docker-future": ("1.53-1.60", "1.53", "outdated"),
}


@pytest.mark.parametrize("fixture", sorted(WINDOWS))
def test_the_negotiated_version_for_each_recorded_window(fixture, monkeypatch):
    # Neither variable may reach the client: set both to something absurd.
    monkeypatch.setenv("DOCKER_API_VERSION", "1.12")
    monkeypatch.setenv("DOCKER_HOST", "tcp://198.51.100.7:2375")
    window, spoken, state = WINDOWS[fixture]
    fake = FakeEngine(engine_fixture(fixture))
    fake.add_container("spend-tracker-app-1", PROJECT)
    with Running(fake) as running:
        client = EngineClient(running.socket_path, scope())
        got = client.negotiate()
        assert (got.engine_api, engine.api_text(got.version), got.state) == (window, spoken, state)
        assert fake.calls[0].path == "/version" and fake.calls[0].version is None
        client.inspect("spend-tracker-app-1")
        assert {c.version for c in fake.calls[1:]} == {spoken}
        # The engine accepted every versioned call: no 400 for a version below its floor.
        assert fake.by_name("spend-tracker-app-1")["State"] == "running"


def test_podman_reports_its_own_version_and_negotiates_on_the_compat_window():
    doc = engine_fixture("podman-machine")
    got = engine.negotiate(doc)
    assert got.podman and got.engine_version == "6.1.3"
    # libpod's MinAPIVersion is 4.0.0; negotiating on it would read as API 4.0.
    assert got.engine_min == (1, 24) and got.engine_max == (1, 44)
    older = engine.negotiate(engine_fixture("podman-4.9"))
    assert older.podman and older.engine_version == "4.9.4" and older.version == (1, 41)
    docker = engine.negotiate(engine_fixture("docker-desktop"))
    assert not docker.podman and docker.engine_version == "29.8.1"
    assert docker.platform == "Docker Desktop 4.93.0 (240920)"


def test_an_outdated_updater_still_has_the_handover_calls_and_nothing_else():
    fake = FakeEngine(engine_fixture("docker-future"))
    fake.add_container("spend-tracker-updater-1", PROJECT)
    fake.add_container("spend-tracker-app-1", PROJECT)
    with Running(fake) as running:
        client = EngineClient(running.socket_path, scope())
        assert client.negotiate().state == "outdated"
        fake.calls.clear()
        # H1-H5: inspect, pull, create, start, rename, stop.
        client.inspect("spend-tracker-updater-1")
        client.pull(UPDATER_IMAGE)
        client.inspect_image(UPDATER_IMAGE)
        nid = client.create("spend-tracker-updater-1-next", successor_body())
        client.start(nid)
        client.rename("spend-tracker-updater-1", "spend-tracker-updater-1-previous")
        client.stop("spend-tracker-updater-1-previous")
        assert {c.version for c in fake.calls} == {"1.53"}
        assert fake.containers[nid]["State"] == "running"
        assert fake.by_name("spend-tracker-updater-1-previous")["State"] == "exited"
        calls = len(fake.calls)
        # Everything else is refused before it is sent.
        for refused in (
            lambda: client.remove("spend-tracker-app-1"),
            lambda: client.probe("spend-tracker-app-1", ["python", "-c", "1"]),
            lambda: client.remove_image(UPDATER_IMAGE),
        ):
            with pytest.raises(NotAllowed):
                refused()
        assert all(c.bare == "/containers/json" for c in fake.calls[calls:])
        assert "spend-tracker-app-1" in [c["Names"][0].lstrip("/") for c in fake.containers.values()]


def test_an_engine_older_than_the_floor_gets_detection_only():
    doc = {"ApiVersion": "1.40", "MinAPIVersion": "1.12", "Components": [{"Name": "Engine", "Version": "19.03"}]}
    fake = FakeEngine(doc)
    fake.add_container("spend-tracker-app-1", PROJECT)
    with Running(fake) as running:
        client = EngineClient(running.socket_path, scope())
        assert client.negotiate().state == "too_old"
        assert client.info() == {"OSType": "linux"}
        with pytest.raises(NotAllowed):
            client.containers()
        with pytest.raises(NotAllowed):
            client.pull(APP_IMAGE)
        assert [c.bare for c in fake.calls] == ["/version", "/info"]


def test_nothing_versioned_is_sent_before_negotiation():
    fake = FakeEngine(engine_fixture("docker-28"))
    with Running(fake) as running:
        client = EngineClient(running.socket_path, scope())
        assert client.ping() == "OK"
        with pytest.raises(NotAllowed):
            client.containers()
        with pytest.raises(NotAllowed):
            client.create("x-drill", good_body())
        assert [c.bare for c in fake.calls] == ["/_ping"]


def test_an_engine_error_carries_its_message():
    fake = FakeEngine(engine_fixture("docker-29.0"))
    fake.add_container("spend-tracker-app-1", PROJECT)
    with Running(fake) as running:
        client = EngineClient(running.socket_path, scope())
        client.negotiate()
        with pytest.raises(engine.EngineError) as e:
            client.inspect_image(APP_IMAGE)
        assert e.value.status == 404 and "No such image" in e.value.message


def test_a_missing_socket_is_unreachable():
    client = EngineClient("/tmp/no-such-engine-here.sock", scope())
    with pytest.raises(engine.EngineUnavailable) as e:
        client.negotiate()
    assert e.value.socket_state == "unreachable"
    assert client.negotiated is None


def test_the_module_never_reads_the_docker_environment():
    source = engine.__file__
    with open(source) as fh:
        code = "".join(line for line in fh if not line.lstrip().startswith("#"))
    body = code.split('"""', 2)[2]  # past the module docstring, which names them
    assert "DOCKER_API_VERSION" not in body and "DOCKER_HOST" not in body
    assert "environ" not in body and "getenv" not in body


# --------------------------------------------------------------------------- #
# #169: `update` is a restart policy, on the app, and nothing else
# --------------------------------------------------------------------------- #


def test_the_apps_restart_policy_is_changed_with_a_body_of_exactly_that(world):
    fake, client = world
    app = fake.by_name("spend-tracker-app-1")
    client.set_restart_policy("spend-tracker-app-1", {"Name": "no"})
    client.set_restart_policy(app["Id"], {"Name": "on-failure", "MaximumRetryCount": 3})
    sent = [c for c in fake.calls if c.bare.endswith("/update")]
    assert [c.body for c in sent] == [
        {"RestartPolicy": {"Name": "no"}},
        {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": 3}},
    ]
    assert all(c.bare == f"/containers/{app['Id']}/update" and c.version == "1.52" for c in sent)


@pytest.mark.parametrize(
    "body",
    [
        {"RestartPolicy": {"Name": "no"}, "Memory": 1 << 30},
        {"RestartPolicy": {"Name": "always"}, "CpuShares": 2},
        {"Memory": 1 << 30},
        {"RestartPolicy": {"Name": "always", "Privileged": True}},
        {"RestartPolicy": {"Name": "sometimes"}},
        {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": -1}},
        {"RestartPolicy": {"Name": "on-failure", "MaximumRetryCount": True}},
        {"RestartPolicy": "no"},
        {},
        None,
        ["RestartPolicy"],
    ],
)
def test_an_update_with_anything_but_a_restart_policy_is_refused_and_never_sent(world, body):
    fake, client = world
    app = fake.by_name("spend-tracker-app-1")
    with pytest.raises(NotAllowed):
        client._send("POST", f"/v1.52/containers/{app['Id']}/update", body=body)
    if isinstance(body, dict) and set(body) == {"RestartPolicy"} and isinstance(body["RestartPolicy"], dict):
        with pytest.raises(NotAllowed):
            client.set_restart_policy("spend-tracker-app-1", body["RestartPolicy"])
    assert not any(c.bare.endswith("/update") for c in fake.calls)


@pytest.mark.parametrize(
    "ref", ["spend-tracker-updater-1", "spend-tracker-tailscale-1", "other-db-1", "spend-tracker_maintenance_1"]
)
def test_only_the_apps_own_container_has_its_restart_policy_changed(world, ref):
    fake, client = world
    with pytest.raises(NotAllowed):
        client.set_restart_policy(ref, {"Name": "no"})
    assert not any(c.bare.endswith("/update") for c in fake.calls)


def test_a_one_off_of_the_app_service_is_not_updated(world):
    fake, client = world
    fake.add_container(
        "spend-tracker-app-run-1",
        PROJECT,
        labels={"com.docker.compose.service": "app", "com.docker.compose.oneoff": "True"},
    )
    with pytest.raises(NotAllowed):
        client.set_restart_policy("spend-tracker-app-run-1", {"Name": "no"})
    assert not any(c.bare.endswith("/update") for c in fake.calls)
