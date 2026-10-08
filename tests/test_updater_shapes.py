"""The containers the updater creates (design notes 4.2, 6.2, 6.4, 8.4, C10).

U11  the copy of the app, against every recorded inspect fixture (Docker
     Engine loopback and sidecar, Docker Desktop, Podman rootful and rootless
     under podman-compose, and `podman machine` under both composes): every
     port binding, mount, network endpoint and alias, limit, restart policy,
     user, healthcheck and compose label is carried, and -- created on an
     engine -- the new container differs from the old one only in the image
     and SPENDTRACKER_AUTO_MIGRATE.

Plus the one-off shapes and the engine client's guard agreeing on them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.self_update.compare import ALLOWED, differences
from tests.updater_fake_engine import FakeEngine, Running, engine_fixture
from updater import engine as eng
from updater import shapes, survey
from updater.engine import EngineClient, NotAllowed, Scope

INSPECT = Path(__file__).parent / "fixtures" / "updater" / "inspect"
NEW = eng.REPOSITORIES[0] + "@sha256:" + "b" * 64
OLD = eng.REPOSITORIES[0] + "@sha256:" + "a" * 64

#: fixture -> (engine fixture, scheme, in a pod, refused by the guard)
FIXTURES = {
    "docker-engine-rootful/loopback-app.json": ("docker-engine-rootful", "docker", False, False),
    "docker-engine-rootful/sidecar.json": ("docker-engine-rootful", "docker", False, False),
    # The spike bound ./pin into the app to test the pin; a host bind on the
    # app is something the updater will not copy.
    "docker-desktop/loopback-app.json": ("docker-desktop", "docker", False, True),
    "podman-rootful-fedora/loopback-app.json": ("podman-rootful-fedora", "podman", True, False),
    "podman-rootless-fedora/loopback-app.json": ("podman-rootless-fedora", "podman", True, False),
    "podman-machine/loopback-app-podman-compose.json": ("podman-machine", "podman", True, True),
    "podman-machine/loopback-app-compose-provider.json": ("podman-machine", "docker", False, True),
}


def load(name: str) -> list[dict]:
    return json.loads((INSPECT / name).read_text())


def app_of(name: str) -> dict:
    return next(d for d in load(name) if survey.service_of(d["Config"]["Labels"]) == "app")


def project_of(inspect: dict) -> str:
    return next(iter(shapes.project_labels(inspect).values()))


def image_part(inspect: dict) -> dict:
    """What the old image put into the container: its OCI and Chainguard labels."""
    labels = inspect["Config"]["Labels"]
    return {
        "Labels": {
            k: v for k, v in labels.items() if k.startswith(("org.opencontainers.", "dev.chainguard."))
        }
    }


def sidecar_id(name: str) -> str | None:
    mode = app_of(name)["HostConfig"]["NetworkMode"]
    return mode.split(":", 1)[1] if mode.startswith("container:") else None


# --------------------------------------------------------------------------- #
# U11: field by field
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_the_copy_carries_every_binding_mount_network_limit_and_label(name):
    prev = app_of(name)
    body = shapes.copy_app(prev, NEW, sidecar_id=sidecar_id(name))
    host, old = body["HostConfig"], prev["HostConfig"]

    assert body["Image"] == NEW != prev["Config"]["Image"]
    assert host["Binds"] == old["Binds"] and host["Binds"]
    for key in ("RestartPolicy", "Memory", "ReadonlyRootfs", "Tmpfs", "SecurityOpt", "CapDrop"):
        assert host[key] == old[key], key
    assert host["LogConfig"]["Type"] == old["LogConfig"]["Type"]
    assert body["User"] == prev["Config"]["User"] == "65532:65532"
    assert body["Healthcheck"] == prev["Config"]["Healthcheck"]

    if old["NetworkMode"].startswith("container:"):
        assert host["NetworkMode"] == old["NetworkMode"]
        assert "NetworkingConfig" not in body and "Hostname" not in body
    else:
        assert host["PortBindings"] == old["PortBindings"] and old["PortBindings"]["8848/tcp"]
        endpoints = body["NetworkingConfig"]["EndpointsConfig"]
        assert set(endpoints) == set(prev["NetworkSettings"]["Networks"])
        for net, ep in prev["NetworkSettings"]["Networks"].items():
            kept = [a for a in ep["Aliases"] if a != prev["Id"][:12]]
            assert endpoints[net]["Aliases"] == kept and "app" in kept
        # The old container's own short id is no name for the new one.
        assert all(prev["Id"][:12] not in (ep.get("Aliases") or []) for ep in endpoints.values())
        assert "Hostname" not in body

    compose = {
        k: v
        for k, v in prev["Config"]["Labels"].items()
        if k.startswith(("com.docker.compose.", "io.podman."))
    }
    assert compose and all(body["Labels"][k] == v for k, v in compose.items())
    assert not any(k.startswith("org.opencontainers.") for k in body["Labels"])

    env = dict(e.split("=", 1) for e in body["Env"])
    was = dict(e.split("=", 1) for e in prev["Config"]["Env"])
    assert was["SPENDTRACKER_AUTO_MIGRATE"] in ("0", "1") and env["SPENDTRACKER_AUTO_MIGRATE"] == "0"
    assert "HOSTNAME" not in env and "container" not in env
    for key, value in was.items():
        if key not in ("SPENDTRACKER_AUTO_MIGRATE", "HOSTNAME", "container", "HOME"):
            assert env[key] == value, key


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_on_an_engine_the_copy_differs_only_in_the_image_and_auto_migrate(name):
    """Created through the client on a fake engine and inspected back (E1's assertion, C10)."""
    fixture, _scheme, _pod, refused = FIXTURES[name]
    prev = app_of(name)
    old_cfg = image_part(prev)
    new_cfg = copy.deepcopy(old_cfg)
    new_cfg["Labels"]["org.opencontainers.image.version"] = "9.9.9"
    fake = FakeEngine(engine_fixture(fixture), engine_fixture(fixture, "info"))
    fake.images[NEW] = {"Id": "sha256:" + "c" * 64, "RepoDigests": [NEW], "Config": new_cfg}
    project = project_of(prev)
    with Running(fake) as running:
        client = EngineClient(running.socket_path, Scope(project=project))
        client.negotiate()
        body = shapes.copy_app(prev, NEW, image_config=old_cfg, sidecar_id=sidecar_id(name))
        if refused:
            with pytest.raises(NotAllowed, match="host bind"):
                client.create(shapes.name_of(prev), body)
            assert fake.calls[-1].bare != "/containers/create"
            return
        cid = client.create(shapes.name_of(prev), body)
        made = client.inspect(cid)
    diff = differences(prev, made, old_cfg, new_cfg)
    allowed = set(ALLOWED)
    if prev["HostConfig"]["NetworkMode"] in ("bridge", "default"):
        # Podman reports `bridge` for a container on the project's network;
        # the copy names the network, which both engines accept.
        allowed.add("HostConfig.NetworkMode")
        assert made["HostConfig"]["NetworkMode"] in prev["NetworkSettings"]["Networks"]
    assert set(diff) <= allowed, {k: v for k, v in diff.items() if k not in allowed}
    assert diff["Config.Image"][1] == NEW
    env = dict(e.split("=", 1) for e in made["Config"]["Env"])
    assert env[shapes.AUTO_MIGRATE] == "0"
    # The recorded sidecar layout already ran with 0; the others ran with 1.
    was = dict(e.split("=", 1) for e in prev["Config"]["Env"])[shapes.AUTO_MIGRATE]
    assert (f"Config.Env[{shapes.AUTO_MIGRATE}]" in diff) is (was == "1")


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_an_app_in_a_podman_pod_is_named_as_such(name):
    assert shapes.in_pod(app_of(name)) is FIXTURES[name][2]


def test_the_sidecar_layout_joins_the_sidecar_as_it_is_now_not_as_it_was():
    prev = app_of("docker-engine-rootful/sidecar.json")
    recreated = "d" * 64
    body = shapes.copy_app(prev, NEW, sidecar_id=recreated)
    assert body["HostConfig"]["NetworkMode"] == f"container:{recreated}"
    assert prev["HostConfig"]["NetworkMode"] != body["HostConfig"]["NetworkMode"]
    with pytest.raises(ValueError):
        shapes.copy_app(prev, NEW, sidecar_id=None)


def test_what_came_from_the_old_image_is_left_to_the_new_one():
    prev = app_of("docker-engine-rootful/loopback-app.json")
    image = {
        "Labels": {
            "org.opencontainers.image.version": prev["Config"]["Labels"][
                "org.opencontainers.image.version"
            ],
            "dev.chainguard.image.title": prev["Config"]["Labels"]["dev.chainguard.image.title"],
        },
        "Env": [e for e in prev["Config"]["Env"] if e.startswith(("PATH=", "SSL_CERT_FILE="))],
        "Entrypoint": prev["Config"]["Entrypoint"],
        "WorkingDir": "/app",
    }
    body = shapes.copy_app(prev, NEW, image_config=image)
    assert "org.opencontainers.image.version" not in body["Labels"]
    assert not any(e.startswith(("PATH=", "SSL_CERT_FILE=")) for e in body["Env"])
    assert "Entrypoint" not in body and "WorkingDir" not in body
    # Where compose had set something of its own, it is carried.
    image["Entrypoint"] = ["python", "-m", "something-else"]
    assert shapes.copy_app(prev, NEW, image_config=image)["Entrypoint"] == prev["Config"]["Entrypoint"]


def test_a_compose_hostname_is_carried_and_an_engine_made_one_is_not():
    prev = app_of("docker-engine-rootful/loopback-app.json")
    assert "Hostname" not in shapes.copy_app(prev, NEW)
    named = copy.deepcopy(prev)
    named["Config"]["Hostname"] = "spend"
    assert shapes.copy_app(named, NEW)["Hostname"] == "spend"


# --------------------------------------------------------------------------- #
# The one-offs, the maintenance page and the probe, against the guard
# --------------------------------------------------------------------------- #

APP = app_of("docker-engine-rootful/loopback-app.json")
RID = "5d3c0b8e-7f0a-4b8e-9f43-0f6f1a2b9c11"


def scope() -> Scope:
    return Scope(project=project_of(APP), bind_sources=("/var/run/docker.sock", "/srv/spend-tracker"))


@pytest.mark.parametrize("role", ["check", "drill", "restore", "measure", "find-backup", "prune"])
def test_every_ledger_one_off_has_no_network_and_passes_the_guard(role):
    body = shapes.oneoff(
        APP,
        NEW,
        role,
        RID,
        ["python", "-c", "1"],
        ledger_volume="spike157_ledger",
        update_volume="spike157_update",
    )
    eng.guard_create(body, scope())
    host = body["HostConfig"]
    assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
    assert body["User"] == "65532:65532" and host["Memory"] == APP["HostConfig"]["Memory"]
    assert host["Binds"] == [
        "spike157_ledger:/var/lib/spend-tracker:rw",
        "spike157_update:/var/lib/spend-tracker-update:rw",
    ]
    assert body["Labels"][eng.ONEOFF_LABEL] == "True" and body["Labels"][eng.ROLE_LABEL] == role
    assert dict(e.split("=", 1) for e in body["Env"])[shapes.AUTO_MIGRATE] == "0"
    # The same body on the bridge, or with a host path, is refused.
    for bad in ({"NetworkMode": "bridge"}, {"Binds": [*host["Binds"], "/srv/spend-tracker:/x"]}):
        with pytest.raises(NotAllowed):
            eng.guard_create({**body, "HostConfig": {**host, **bad}}, scope())


def test_the_maintenance_page_is_the_old_image_where_the_app_listened_with_the_ledger_read_only():
    body = shapes.placard(APP, OLD, RID, ledger_volume="l", update_volume="u", sidecar_id=None)
    eng.guard_create(body, scope())
    host = body["HostConfig"]
    assert body["Image"] == OLD and body["Cmd"] == ["-m", "scripts.placard"]
    assert host["PortBindings"] == APP["HostConfig"]["PortBindings"]
    assert host["NetworkMode"] == APP["HostConfig"]["NetworkMode"]
    assert host["Binds"] == ["u:/var/lib/spend-tracker-update:rw", "l:/var/lib/spend-tracker:ro"]
    assert body["Labels"]["com.docker.compose.oneoff"] == "True"
    sidecar = app_of("docker-engine-rootful/sidecar.json")
    joined = shapes.placard(
        sidecar, OLD, RID, ledger_volume="l", update_volume="u", sidecar_id="e" * 64, recovery=True
    )
    assert joined["HostConfig"]["NetworkMode"] == "container:" + "e" * 64
    assert joined["Cmd"] == ["-m", "scripts.placard", "--recovery"]


def test_the_port_probe_runs_the_app_image_on_the_bridge_and_mounts_nothing():
    body = shapes.port_probe(APP, NEW, RID, "host.docker.internal", 8848)
    eng.guard_create(body, scope())
    assert body["HostConfig"]["NetworkMode"] == "bridge" and "Binds" not in body["HostConfig"]
    assert body["Cmd"][-2:] == ["http://host.docker.internal:8848/api/health", "localhost:8848"]
    for bad in (
        {**body, "HostConfig": {**body["HostConfig"], "Binds": ["l:/var/lib/spend-tracker"]}},
        {**body, "HostConfig": {**body["HostConfig"], "NetworkMode": "none"}},
        {**body, "Image": eng.REPOSITORIES[1] + "@sha256:" + "b" * 64},
    ):
        with pytest.raises(NotAllowed):
            eng.guard_create(bad, scope())


def test_a_one_off_without_a_role_or_a_request_and_a_role_on_a_service_are_refused():
    body = shapes.oneoff(APP, NEW, "drill", RID, ["python"], ledger_volume="l")
    for labels in (
        {k: v for k, v in body["Labels"].items() if k != eng.ROLE_LABEL},
        {k: v for k, v in body["Labels"].items() if k != eng.REQUEST_LABEL},
        {**body["Labels"], eng.ROLE_LABEL: "anything"},
        {k: v for k, v in body["Labels"].items() if k != eng.ONEOFF_LABEL},
    ):
        with pytest.raises(NotAllowed):
            eng.guard_create({**body, "Labels": labels}, scope())


def test_the_published_port_is_read_from_the_bindings():
    assert shapes.published_port(APP) == 18848
    assert shapes.published_port(app_of("docker-engine-rootful/sidecar.json")) is None
