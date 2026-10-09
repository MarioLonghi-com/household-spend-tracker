"""Engine detection, the socket refusals, the not-root rule and the heartbeat (design notes 5.3, 8.1-8.3).

U8  every recorded and hand-written `/version` + `/info` answer gives the right
    `engine`, `rootless`, `selinux` and socket state; Windows containers,
    Podman 4.3, an API below the floor and an unknown engine are refused with
    their sentences;
C3  `outdated` from `docker-future`, end to end through the client;
5.3 the heartbeat carries every key, `api_version`, `engine_api`, `protocols`
    and the updater's real container name, found under Docker Compose's and
    podman-compose's naming alike.

Fixtures are in `tests/fixtures/updater/engines/`; its README says which are
recorded and which are written by hand.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import uuid
from pathlib import Path

import pytest

from tests.updater_fake_engine import ENGINES, FakeEngine, Running, engine_fixture
from updater import contract, detect, engine, heartbeat
from updater.contract import Context, RunningApp
from updater.engine import EngineClient, Scope
from updater.volume import Volume

PROJECT = "spend-tracker"
NOW = 1_791_500_000.0
APP_REPO = "ghcr.io/mariolonghi-com/household-spend-tracker"
UPDATER_REPO = "ghcr.io/mariolonghi-com/household-spend-tracker-updater"
APP_DIGEST = "sha256:" + "a" * 64
UPDATER_DIGEST = "sha256:" + "b" * 64


def info_of(name: str) -> dict:
    path = ENGINES / name / "info.json"
    return json.loads(path.read_text()) if path.exists() else {"OSType": "linux"}


# --------------------------------------------------------------------------- #
# U8: every fixture
# --------------------------------------------------------------------------- #

#: folder: (engine, rootless, selinux, socket, engine_version, api_version, engine_api)
U8 = {
    "docker-28": ("docker-engine", False, False, "ok", "28.5.1", "1.51", "1.24-1.51"),
    "docker-29.0": ("docker-engine", False, False, "ok", "29.0.0", "1.52", "1.44-1.52"),
    "docker-desktop": ("docker-desktop", False, False, "ok", "4.93.0", "1.52", "1.40-1.56"),
    "docker-desktop-windows": ("docker-desktop", False, False, "windows_containers", "4.93.0", "1.52", "1.24-1.56"),
    "docker-engine-rootful": ("docker-engine", False, False, "ok", "29.8.2", "1.52", "1.40-1.56"),
    "docker-engine-rootless": ("docker-engine", True, False, "ok", "29.8.2", "1.52", "1.40-1.56"),
    "docker-future": ("docker-engine", False, False, "outdated", "31.0.0", "1.53", "1.53-1.60"),
    "podman-4.3": ("podman", True, False, "too_old", "4.3.1", "1.41", "1.24-1.41"),
    "podman-4.9": ("podman", False, False, "ok", "4.9.4", "1.41", "1.24-1.41"),
    "podman-machine": ("podman-machine", True, True, "ok", "6.1.3", "1.44", "1.24-1.44"),
    "podman-rootful-fedora": ("podman", False, True, "ok", "5.8.7", "1.44", "1.24-1.44"),
    "podman-rootless-fedora": ("podman", True, True, "ok", "5.8.7", "1.44", "1.24-1.44"),
}  # fmt: skip


def test_every_fixture_folder_is_in_the_u8_table():
    folders = {p.name for p in ENGINES.iterdir() if p.is_dir()}
    assert folders == set(U8)


@pytest.mark.parametrize("name", sorted(U8))
def test_u8_each_engine_is_identified(name):
    found = detect.identify(engine_fixture(name), info_of(name))
    engine_, rootless, selinux, state, version, api, window = U8[name]
    assert (found.engine, found.rootless, found.selinux, found.socket) == (engine_, rootless, selinux, state)
    assert (found.engine_version, found.api_version, found.engine_api) == (version, api, window)
    assert (found.sentence is None) == (state == "ok")


@pytest.mark.parametrize("name", sorted(U8))
def test_u8_each_engine_has_a_not_root_rule_with_the_volume_group(name):
    engine_, rootless, *_ = U8[name]
    rule = detect.not_root(engine_, rootless)
    assert rule.group_add[-1] == "65532"
    assert rule.label_disable
    # Rootless on Linux is in-container uid 0; everywhere else 65532.
    assert rule.user == ("0:0" if rootless and engine_ in ("docker-engine", "podman") else "65532:65532")


def test_the_hand_written_fixtures_say_so_and_the_recorded_ones_do_not():
    recorded = {"docker-desktop", "podman-machine", "docker-engine-rootful", "podman-rootful-fedora", "podman-rootless-fedora"}
    readme = (ENGINES / "README.md").read_text()
    for folder in U8:
        assert f"`{folder}`" in readme
        if (ENGINES / folder / "info.json").exists():
            marked = {f.name for f in (ENGINES / folder).glob("*.json") if json.loads(f.read_text()).get("_hand_written")}
            assert marked == (set() if folder in recorded else {"version.json", "info.json"}), folder


def test_detection_ignores_the_hand_written_marker():
    doc = engine_fixture("docker-engine-rootless")
    bare = {k: v for k, v in doc.items() if k != "_hand_written"}
    assert detect.identify(doc, info_of("docker-engine-rootless")) == detect.identify(
        bare, info_of("docker-engine-rootless")
    )


# --------------------------------------------------------------------------- #
# podman machine against Podman on Linux
# --------------------------------------------------------------------------- #


def test_the_recorded_machine_is_a_machine_by_its_storage_root_and_fedora_is_not():
    machine = detect.identify(engine_fixture("podman-machine"), info_of("podman-machine"))
    fedora = detect.identify(engine_fixture("podman-rootless-fedora"), info_of("podman-rootless-fedora"))
    # Both say "fedora", both are rootless under SELinux: only the storage root differs.
    assert info_of("podman-machine")["OperatingSystem"] == info_of("podman-rootless-fedora")["OperatingSystem"]
    assert (machine.engine, machine.vm) == ("podman-machine", True)
    assert (fedora.engine, fedora.vm) == ("podman", False)


@pytest.mark.parametrize(
    "working_dir, expected",
    [
        ("/Users/Shared/spend-tracker", "podman-machine"),
        ("C:\\Users\\someone\\spend-tracker", "podman-machine"),
        ("/srv/spend-tracker", "podman"),
        ("/home/someone/spend-tracker", "podman"),
        (None, "podman"),
    ],
)
def test_a_rootful_machine_is_told_apart_by_the_projects_host_path(working_dir, expected):
    # A rootful machine's storage is /var/lib/containers, like any rootful host's.
    info = {**info_of("podman-machine"), "DockerRootDir": "/var/lib/containers/storage", "Rootless": False}
    info["SecurityOptions"] = ["name=seccomp,profile=default", "name=selinux"]
    found = detect.identify(engine_fixture("podman-machine"), info, working_dir)
    assert (found.engine, found.rootless) == (expected, False)


def test_a_mac_path_does_not_make_docker_a_machine():
    found = detect.identify(engine_fixture("docker-engine-rootful"), info_of("docker-engine-rootful"), "/Users/Shared/st")
    desktop = detect.identify(engine_fixture("docker-desktop"), info_of("docker-desktop"), "/srv/st")
    assert (found.engine, desktop.engine) == ("docker-engine", "docker-desktop")


def test_docker_desktop_is_recognised_from_info_alone_too():
    version = {**engine_fixture("docker-desktop"), "Platform": {"Name": ""}}
    by_info = detect.identify(version, info_of("docker-desktop"))
    by_neither = detect.identify(version, {**info_of("docker-desktop"), "OperatingSystem": "Ubuntu 24.04"})
    assert (by_info.engine, by_neither.engine) == ("docker-desktop", "docker-engine")


# --------------------------------------------------------------------------- #
# Each refusal: its token and its sentence
# --------------------------------------------------------------------------- #


def test_windows_containers_is_refused_and_linux_containers_on_the_same_desktop_is_not():
    windows = detect.identify(engine_fixture("docker-desktop-windows"), info_of("docker-desktop-windows"))
    linux = detect.identify(engine_fixture("docker-desktop"), info_of("docker-desktop"))
    assert windows.socket == "windows_containers"
    assert windows.sentence == "Docker Desktop is in Windows containers mode. Switch to Linux containers."
    assert (linux.socket, linux.sentence) == ("ok", None)


def test_podman_older_than_4_4_is_refused_by_its_own_version_not_its_api():
    old = detect.identify(engine_fixture("podman-4.3"), info_of("podman-4.3"))
    newer = detect.identify(engine_fixture("podman-4.9"))
    # Both speak compat API 1.41: the floor is read from Components (S11, R6).
    assert engine_fixture("podman-4.3")["ApiVersion"] == engine_fixture("podman-4.9")["ApiVersion"]
    assert old.socket == "too_old"
    assert old.sentence == "Podman 4.3 is too old for self-update; 4.4 or newer is needed."
    assert (newer.socket, newer.sentence) == ("ok", None)


def test_an_api_below_the_tested_floor_is_too_old():
    doc = {**engine_fixture("docker-28"), "ApiVersion": "1.40", "MinAPIVersion": "1.12"}
    old = detect.identify(doc)
    ok = detect.identify({**doc, "ApiVersion": "1.41"})
    assert old.socket == "too_old"
    assert old.sentence == "Docker Engine API 1.40 is too old for self-update; 1.41 or newer is needed."
    assert ok.socket == "ok"


@pytest.mark.parametrize(
    "doc, name",
    [
        ({"ApiVersion": "1.45", "Components": [{"Name": "Moby Fork", "Version": "1.0"}]}, "Moby Fork"),
        ({"Platform": {"Name": "Some Runtime 2"}, "Components": [{"Name": "runtime", "Version": "2.0"}],
          "ApiVersion": "1.45"}, "runtime"),
        ({"Platform": {"Name": "Some Runtime 2"}}, "Some Runtime 2"),
    ],
)  # fmt: skip
def test_an_unknown_engine_is_refused_with_its_name(doc, name):
    found = detect.identify(doc)
    assert (found.socket, found.engine) == ("unknown_engine", None)
    assert found.sentence == (
        f"The updater does not recognise the container engine {name}, so it does nothing with it."
    )


@pytest.mark.parametrize(
    "target, refused",
    [
        ("tcp://192.0.2.10:2375", True),
        ("https://engine.example:2376", True),
        ("ssh://user@host", True),
        ("engine.local:2375", True),
        ("unix:///var/run/docker.sock", False),
        ("/run/user/1000/podman/podman.sock", False),
    ],
)
def test_a_tcp_socket_is_refused_and_a_unix_one_is_not(target, refused):
    found = detect.check_socket_target(target)
    if refused:
        assert found is not None and found.socket == "tcp_socket"
        assert found.sentence == "The updater talks to the container engine over a unix socket only, not over TCP."
    else:
        assert found is None


def test_a_socket_the_updater_may_not_open_is_permission_denied():
    if os.geteuid() == 0:
        pytest.skip("root opens any socket")
    d = tempfile.mkdtemp(prefix="pd-", dir="/tmp")
    path = os.path.join(d, "engine.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    os.chmod(path, 0)
    try:
        denied = detect.detect(EngineClient(path, Scope(project=PROJECT)))
        missing = detect.detect(EngineClient(os.path.join(d, "absent.sock"), Scope(project=PROJECT)))
    finally:
        server.close()
        os.unlink(path)
        os.rmdir(d)
    assert (denied.socket, denied.engine) == ("permission_denied", None)
    assert denied.sentence == "The updater cannot use the container engine: permission denied on its socket."
    assert (missing.socket, missing.sentence) == ("unreachable", "The container engine's socket does not answer.")


class _EciEngine(FakeEngine):
    def handle(self, method, raw_path, body):
        status, answer = super().handle(method, raw_path, body)
        if raw_path.endswith("/info"):
            return 403, {"message": "enhanced container isolation: docker socket access denied for this image"}
        return status, answer


def test_eci_blocking_the_socket_names_the_allowlist_setting():
    blocked = _EciEngine(engine_fixture("docker-desktop"), info_of("docker-desktop"))
    with Running(blocked) as running:
        found = detect.detect(EngineClient(running.socket_path, Scope(project=PROJECT)))
    other = detect.from_error(engine.EngineError(403, "forbidden by an authorization plugin"))
    assert found.socket == "eci_blocked"
    assert "enhancedContainerIsolation.dockerSocketMount.imageList.images" in found.sentence
    assert "Docker Business only" in found.sentence
    assert other.socket == "unreachable"


def test_outdated_from_docker_future_end_to_end():
    future = FakeEngine(engine_fixture("docker-future"), {"OSType": "linux", "SecurityOptions": []})
    current = FakeEngine(engine_fixture("docker-29.0"), {"OSType": "linux", "SecurityOptions": []})
    results = {}
    for name, fake in (("future", future), ("current", current)):
        with Running(fake) as running:
            client = EngineClient(running.socket_path, Scope(project=PROJECT))
            results[name] = detect.detect(client)
            results[name + "-info"] = [c.version for c in fake.calls if c.bare == "/info"]
    assert results["future"].socket == "outdated"
    assert results["future"].sentence.startswith("The updater is too old for this container engine.")
    # Outdated still detects: /info at the engine's floor, not below it.
    assert results["future-info"] == ["1.53"] and results["current-info"] == ["1.52"]
    assert results["current"].socket == "ok"


def test_a_refused_socket_refuses_a_request_with_detections_sentence():
    found = detect.identify(engine_fixture("docker-desktop-windows"), info_of("docker-desktop-windows"))
    app = RunningApp(version="0.8.0", revision="0123456789ab", published=True)
    ctx = Context(app=app, updater_version="0.8.0", socket=found.socket, socket_sentence=found.sentence)
    bare = Context(app=app, updater_version="0.8.0", socket="tcp_socket")
    request = {
        "protocol": 1, "id": str(uuid.uuid4()), "kind": "prepare", "created_at": contract.iso(NOW),
        "requested_by": "owner", "from_version": "0.8.0", "to_version": "0.9.0",
    }  # fmt: skip
    with pytest.raises(contract.Refusal) as with_sentence:
        contract.validate(request, ctx, NOW)
    with pytest.raises(contract.Refusal) as without:
        contract.validate({**request, "id": str(uuid.uuid4())}, bare, NOW)
    assert with_sentence.value.sentence == "Docker Desktop is in Windows containers mode. Switch to Linux containers."
    assert without.value.sentence == "The updater cannot use the container engine (tcp_socket)."


# --------------------------------------------------------------------------- #
# Not root, per engine
# --------------------------------------------------------------------------- #


def test_not_root_rules_follow_the_spikes():
    desktop = detect.not_root("docker-desktop", False)
    machine = detect.not_root("podman-machine", True)
    rootless_docker = detect.not_root("docker-engine", True)
    rootful_docker = detect.not_root("docker-engine", False)
    assert (desktop.user, desktop.group_add, desktop.confirmed) == ("65532:65532", ("0", "65532"), True)
    assert (machine.user, machine.podman_restart, machine.linger) == ("65532:65532", "user", False)
    # S21: a rootful machine, Podman Desktop's default, observed -- the
    # system podman-restart unit, not the user one, and the default socket.
    rootful_machine = detect.not_root("podman-machine", False)
    assert (rootful_machine.user, rootful_machine.group_add, rootful_machine.podman_restart) == (
        "65532:65532", ("0", "65532"), "system",
    )  # fmt: skip
    assert (rootful_machine.socket, rootful_machine.linger, rootful_machine.confirmed) == (
        "/var/run/docker.sock", False, True,
    )  # fmt: skip
    assert all(rule.confirmed for rule in detect._RULES.values())
    # B11: rootless Docker, observed: only in-container uid 0 reaches the socket.
    assert (rootless_docker.user, rootless_docker.linger, rootless_docker.confirmed) == ("0:0", True, True)
    # Rootful Docker: the host's docker group, never 0 (observed: gid 0 is refused).
    assert rootful_docker.group_add == ("${SPENDTRACKER_SOCKET_GID}", "65532")
    assert rootful_docker.podman_restart is None and rootful_docker.confirmed


def test_linux_podman_rootless_is_uid_0_with_podman_restart_and_lingering_and_rootful_is_not():
    rootless = detect.not_root("podman", True)
    rootful = detect.not_root("podman", False)
    assert (rootless.user, rootless.group_add, rootless.podman_restart, rootless.linger) == ("0:0", ("65532",), "user", True)
    assert (rootful.user, rootful.group_add, rootful.podman_restart, rootful.linger) == (
        "65532:65532", ("0", "65532"), "system", False,
    )  # fmt: skip
    assert rootless.label_disable and rootful.label_disable
    with pytest.raises(KeyError):
        detect.not_root("lxc", False)


# --------------------------------------------------------------------------- #
# podman-restart, inferred (S2)
# --------------------------------------------------------------------------- #


def _own(created: float, started: float) -> dict:
    return {"Created": contract.iso(created), "State": {"StartedAt": contract.iso(started)}}


def test_podman_restart_is_enabled_only_when_a_boot_started_an_older_container():
    found = detect.identify(engine_fixture("podman-machine"), {**info_of("podman-machine"), "Uptime": "0h 20m 0.00s"})
    boot = NOW - 20 * 60
    restarted = detect.podman_restart(found, _own(boot - 86400, boot + 30), NOW)
    created_now = detect.podman_restart(found, _own(boot + 60, boot + 61), NOW)
    started_late = detect.podman_restart(found, _own(boot - 86400, boot + 15 * 60), NOW)
    docker = detect.podman_restart(detect.identify(engine_fixture("docker-desktop"), info_of("docker-desktop")),
                                   _own(boot - 86400, boot + 30), NOW)  # fmt: skip
    assert (restarted, created_now, started_late, docker) == ("enabled", "unknown", "unknown", "not_applicable")


def test_engine_times_and_uptime_parse_in_both_engines_formats():
    docker = detect.parse_engine_time("2026-10-07T22:15:28.839801804Z")
    podman = detect.parse_engine_time("2026-10-08T00:15:28.839801804+02:00")
    assert docker == podman and int(docker) == int(contract.parse_iso("2026-10-07T22:15:28Z"))
    assert detect.parse_uptime("0h 25m 19.00s") == 1519.0
    assert detect.parse_uptime("71h 3m 12.00s") == 71 * 3600 + 3 * 60 + 12
    assert detect.parse_uptime("") is None and detect.parse_engine_time("yesterday") is None


# --------------------------------------------------------------------------- #
# A local build (A6), and the layout
# --------------------------------------------------------------------------- #

RELEASE_LABELS = {"org.opencontainers.image.version": "v0.7.1", "org.opencontainers.image.revision": "987afef" * 5 + "abcde"}


@pytest.mark.parametrize(
    "image, labels, repo_digests, local",
    [
        (f"{APP_REPO}@{APP_DIGEST}", RELEASE_LABELS, [], False),
        (f"{APP_REPO}:0.7.1", RELEASE_LABELS, [f"{APP_REPO}@{APP_DIGEST}"], False),
        (f"{APP_REPO}:0.7.1", RELEASE_LABELS, [], True),
        ("household-spend-tracker-app:latest", {}, [], True),
        (f"{APP_REPO}@{APP_DIGEST}", {"org.opencontainers.image.version": "v0.7.1"}, [], True),
        (f"{UPDATER_REPO}@{UPDATER_DIGEST}", RELEASE_LABELS, [], True),
        ("ghcr.io/someone-else/household-spend-tracker:0.7.1", RELEASE_LABELS,
         ["ghcr.io/someone-else/household-spend-tracker@" + APP_DIGEST], True),
    ],
)  # fmt: skip
def test_a_local_build_is_recognised_from_the_app_containers_labels_and_digest(image, labels, repo_digests, local):
    app = detect.running_app({"Config": {"Image": image, "Labels": labels}}, repo_digests)
    assert app.local_build is local
    if not local:
        assert (app.version, app.revision) == ("0.7.1", RELEASE_LABELS["org.opencontainers.image.revision"])


def test_the_app_images_protocol_label_is_read():
    labelled = detect.running_app({"Config": {"Labels": {detect.PROTOCOL_LABEL: "2"}}})
    unlabelled = detect.running_app({"Config": {"Labels": {}}})
    assert (labelled.protocol, unlabelled.protocol) == (2, 1)


def test_the_layout_is_the_apps_network_mode():
    sidecar = detect.layout_of({"HostConfig": {"NetworkMode": "container:" + "c" * 64}})
    loopback = detect.layout_of({"HostConfig": {"NetworkMode": "spend-tracker_default"}})
    assert (sidecar, loopback) == ("sidecar", "loopback")


# --------------------------------------------------------------------------- #
# The updater's own container
# --------------------------------------------------------------------------- #


def _mountinfo(cid: str, podman: bool) -> str:
    root = f"/containers/overlay-containers/{cid}/userdata" if podman else f"/docker/containers/{cid}"
    lines = [
        "700 600 0:50 / / ro,relatime - overlay overlay rw",
        f"801 700 254:1 {root}/resolv.conf /etc/resolv.conf rw,relatime - ext4 /dev/vda1 rw",
        f"802 700 254:1 {root}/hostname /etc/hostname rw,relatime - ext4 /dev/vda1 rw",
        f"803 700 254:1 {root}/hosts /etc/hosts rw,relatime - ext4 /dev/vda1 rw",
        # A volume path that also looks like an id must not be taken for ours.
        f"804 700 254:1 /volumes/containers/{'d' * 64}/x /update rw,relatime - ext4 /dev/vda1 rw",
    ]
    return "\n".join(lines) + "\n"


def test_own_ids_reads_docker_and_podman_mountinfo():
    a, b = "1" * 64, "2" * 64
    assert detect.own_ids(_mountinfo(a, podman=False), "updater") == ([a], None)
    assert detect.own_ids(_mountinfo(b, podman=True), b[:12]) == ([b], b[:12])


# Both naming schemes of S5: Docker Compose (and `podman compose`, which
# delegates to it) and podman-compose.
NAMING = {
    "docker-compose": ("com.docker.compose.project", "com.docker.compose.service", "-", "docker-desktop"),
    "podman-compose": ("io.podman.compose.project", "io.podman.compose.service", "_", "podman-machine"),
}


def _world(scheme: str) -> FakeEngine:
    project_label, service_label, sep, fixture = NAMING[scheme]
    fake = FakeEngine(engine_fixture(fixture), info_of(fixture))
    for service in ("app", "updater"):
        fake.add_container(
            sep.join((PROJECT, service, "1")), PROJECT, label=project_label,
            labels={service_label: service, detect.WORKING_DIR_LABEL: "/Users/Shared/spend-tracker"},
            HostConfig={"NetworkMode": "spend-tracker_default"},
        )  # fmt: skip
    # Another project's updater, on the same engine, with the same service name.
    fake.add_container("other-updater-1", "other", labels={"com.docker.compose.service": "updater"})
    return fake


@pytest.mark.parametrize("scheme", sorted(NAMING))
@pytest.mark.parametrize("via", ["mountinfo", "hostname"])
def test_the_updater_finds_its_real_name_under_both_compose_naming_schemes(scheme, via):
    fake = _world(scheme)
    sep = NAMING[scheme][2]
    me = fake.by_name(sep.join((PROJECT, "updater", "1")))["Id"]
    mountinfo = _mountinfo(me, podman=scheme == "podman-compose") if via == "mountinfo" else ""
    hostname = "updater-host" if via == "mountinfo" else me[:12]
    with Running(fake) as running:
        client = EngineClient(running.socket_path, Scope(project=PROJECT))
        client.negotiate()
        own = detect.find_own_container(client.containers(), mountinfo, hostname)
    assert own is not None and own["Id"] == me
    assert detect.container_name(own) == f"{PROJECT}{sep}updater{sep}1"


def test_no_name_is_guessed_when_nothing_identifies_the_container():
    fake = _world("docker-compose")
    with Running(fake) as running:
        client = EngineClient(running.socket_path, Scope(project=PROJECT))
        client.negotiate()
        listing = client.containers()
    other = next(c for c in fake.containers.values() if c["Names"] == ["/other-updater-1"])
    # A custom hostname, no mountinfo; and another project's id, which the listing never holds.
    assert detect.find_own_container(listing, "", "my-updater") is None
    assert detect.find_own_container(listing, _mountinfo(other["Id"], podman=False), other["Id"][:12]) is None


# --------------------------------------------------------------------------- #
# The heartbeat (5.3)
# --------------------------------------------------------------------------- #


def _beat(fake: FakeEngine, running: Running, tmp_path: Path, scheme: str) -> heartbeat.Beat:
    sep = NAMING[scheme][2]
    me = fake.by_name(sep.join((PROJECT, "updater", "1")))["Id"]
    client = EngineClient(running.socket_path, Scope(project=PROJECT))
    return heartbeat.Beat(
        client, Volume(tmp_path), heartbeat.Identity("0.8.0", UPDATER_DIGEST),
        mountinfo=_mountinfo(me, podman=scheme == "podman-compose"), hostname="x",
    )  # fmt: skip


@pytest.mark.parametrize("scheme", sorted(NAMING))
def test_the_heartbeat_file_carries_every_key_and_the_real_name(scheme, tmp_path):
    fake = _world(scheme)
    with Running(fake) as running:
        beat = _beat(fake, running, tmp_path, scheme)
        beat.tick(NOW)
    written = json.loads((tmp_path / "updater.json").read_text())
    sep = NAMING[scheme][2]
    assert set(written) == {
        "protocol", "updater_version", "image_digest", "seen_at", "engine", "engine_version",
        "rootless", "layout", "socket", "hook", "busy", "role", "protocols", "api_version",
        "engine_api", "container", "socket_sentence", "podman_restart", "problem",
    }  # fmt: skip
    assert written["problem"] is None
    assert written["container"] == f"{PROJECT}{sep}updater{sep}1"
    assert (written["protocols"], written["protocol"], written["seen_at"]) == ("1-1", 1, contract.iso(NOW))
    if scheme == "docker-compose":
        assert (written["engine"], written["api_version"], written["engine_api"]) == ("docker-desktop", "1.52", "1.40-1.56")
        assert written["podman_restart"] == "not_applicable"
    else:
        assert (written["engine"], written["api_version"], written["engine_api"]) == ("podman-machine", "1.44", "1.24-1.44")
        assert (written["rootless"], written["podman_restart"]) == (True, "unknown")
    assert (written["socket"], written["socket_sentence"], written["layout"]) == ("ok", None, "loopback")


def test_the_projects_mac_path_makes_a_rootful_podman_a_machine_in_the_heartbeat(tmp_path):
    fake = _world("podman-compose")
    fake.info_doc = {**fake.info_doc, "DockerRootDir": "/var/lib/containers/storage", "Rootless": False,
                     "SecurityOptions": ["name=selinux"]}  # fmt: skip
    with Running(fake) as running:
        beat = _beat(fake, running, tmp_path, "podman-compose")
        first = beat.tick(NOW)
    linux = _world("podman-compose")
    linux.info_doc = fake.info_doc
    for c in linux.containers.values():
        c["Labels"][detect.WORKING_DIR_LABEL] = "/srv/spend-tracker"
    with Running(linux) as running:
        second = _beat(linux, running, tmp_path, "podman-compose").tick(NOW)
    assert (first.engine, first.rootless) == ("podman-machine", False)
    assert second.engine == "podman"


def test_a_beat_between_detections_makes_no_engine_call_and_the_fifth_minute_does(tmp_path):
    fake = _world("docker-compose")
    with Running(fake) as running:
        beat = _beat(fake, running, tmp_path, "docker-compose")
        beat.tick(NOW)
        after_first = len(fake.calls)
        second = beat.tick(NOW + heartbeat.HEARTBEAT_EVERY_SECONDS)
        after_second = len(fake.calls)
        beat.tick(NOW + detect.DETECT_EVERY_SECONDS)
        after_fifth_minute = len(fake.calls)
    assert after_first > 0 and after_second == after_first
    assert after_fifth_minute > after_second
    assert second.seen_at == contract.iso(NOW + 30)


def test_a_refused_socket_is_in_the_heartbeat_and_is_retried_at_every_beat(tmp_path):
    fake = FakeEngine(engine_fixture("docker-desktop-windows"), info_of("docker-desktop-windows"))
    with Running(fake) as running:
        client = EngineClient(running.socket_path, Scope(project=PROJECT))
        beat = heartbeat.Beat(client, Volume(tmp_path), heartbeat.Identity("0.8.0", UPDATER_DIGEST), mountinfo="", hostname="x")
        first = beat.tick(NOW)
        calls = len(fake.calls)
        fake.version_doc, fake.info_doc = engine_fixture("docker-desktop"), info_of("docker-desktop")
        second = beat.tick(NOW + 30)
    assert (first.socket, first.engine) == ("windows_containers", "docker-desktop")
    assert first.socket_sentence == "Docker Desktop is in Windows containers mode. Switch to Linux containers."
    assert len(fake.calls) > calls
    assert (second.socket, second.socket_sentence) == ("ok", None)


def test_an_unreachable_engine_writes_an_unknown_engine_heartbeat(tmp_path):
    client = EngineClient("/tmp/no-such-engine-for-the-heartbeat.sock", Scope(project=PROJECT))
    beat = heartbeat.Beat(client, Volume(tmp_path), heartbeat.Identity("0.8.0", UPDATER_DIGEST), mountinfo="", hostname="x")
    written = beat.tick(NOW).to_dict()
    on_disk = json.loads((tmp_path / "updater.json").read_text())
    assert (written["engine"], written["socket"], written["container"]) == ("unknown", "unreachable", "")
    assert on_disk == written
