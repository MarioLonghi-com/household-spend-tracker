"""What the release zip's launchers decide before `compose up` (#167, design notes 9.2 C5, Part 13).

C5   the pin rule: no pin is the bundle's; a pinned app is kept whatever the
     bundle ships; the updater is the newer of the two, never a downgrade;
9.1  where the pin is found: the folder the project was started from first,
     `.env` before `pin/release.env`;
8.2  the per-engine settings, from the recorded detection fixtures:
     SPENDTRACKER_SOCKET_GID (S12), SPENDTRACKER_UPDATER_USER (S18),
     podman-restart's scope (S2, S20, S21), lingering (S20), and the `chgrp`
     the project directory needs under a rootful Linux engine (R34);
     and end to end against a fake engine on a real socket: what `.env`
     holds afterwards, what the launcher is told, and that a refusal writes
     nothing.

Two of everything: two releases on each side of every comparison, two engines
per row of the table.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.updater_fake_engine import ENGINES, FakeEngine, Running, engine_fixture
from updater import detect, engine, launch, pin

APP = "ghcr.io/mariolonghi-com/household-spend-tracker"
UPD = "ghcr.io/mariolonghi-com/household-spend-tracker-updater"


def ref(repo: str, version: str, fill: str) -> str:
    return f"{repo}:{version}@sha256:{fill * 64}"


def info_of(name: str) -> dict:
    path = ENGINES / name / "info.json"
    return json.loads(path.read_text()) if path.exists() else {"OSType": "linux"}


def detection(fixture: str, working_dir: str = "/home/user/spend-tracker") -> detect.Detection:
    return detect.identify(engine_fixture(fixture), info_of(fixture), working_dir)


# --------------------------------------------------------------------------- #
# C5: the rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("version", ["0.8.0", "0.9.1"])
def test_with_no_pin_the_bundle_s_images_start(version):
    bundle_app, bundle_upd = ref(APP, version, "a"), ref(UPD, version, "b")
    choice = launch.choose({}, bundle_app, bundle_upd)
    assert (choice.app, choice.updater, choice.pinned) == (None, None, False)
    assert choice.sentences == ()


@pytest.mark.parametrize(("pinned", "bundled"), [("0.8.0", "0.9.0"), ("0.9.2", "0.10.0")])
def test_a_pinned_older_app_is_kept_and_the_bundle_s_newer_one_is_not_started(pinned, bundled):
    found = {pin.APP_KEY: ref(APP, pinned, "1"), pin.UPDATER_KEY: ref(UPD, pinned, "2")}
    choice = launch.choose(found, ref(APP, bundled, "a"), ref(UPD, bundled, "b"))
    assert choice.app == ref(APP, pinned, "1")
    assert f"Your ledger is at {pinned}" in choice.sentences[0]
    assert bundled in choice.sentences[0]


@pytest.mark.parametrize(("pinned", "bundled"), [("0.9.0", "0.8.0"), ("1.0.0", "0.10.3")])
def test_a_pinned_newer_app_is_kept_over_an_older_download(pinned, bundled):
    found = {pin.APP_KEY: ref(APP, pinned, "1"), pin.UPDATER_KEY: ref(UPD, pinned, "2")}
    choice = launch.choose(found, ref(APP, bundled, "a"), ref(UPD, bundled, "b"))
    assert choice.app == ref(APP, pinned, "1")


@pytest.mark.parametrize(("pinned", "bundled"), [("0.8.0", "0.9.0"), ("0.9.9", "0.10.0")])
def test_the_bundle_s_newer_updater_replaces_the_pin_s(pinned, bundled):
    found = {pin.APP_KEY: ref(APP, pinned, "1"), pin.UPDATER_KEY: ref(UPD, pinned, "2")}
    choice = launch.choose(found, ref(APP, bundled, "a"), ref(UPD, bundled, "b"))
    assert choice.updater == ref(UPD, bundled, "b")
    assert choice.app == ref(APP, pinned, "1")
    assert f"The updater is replaced: {pinned} by {bundled}" in choice.sentences[-1]


@pytest.mark.parametrize(("pinned", "bundled"), [("0.9.0", "0.8.0"), ("0.10.0", "0.9.9")])
def test_the_pin_s_newer_updater_is_never_downgraded(pinned, bundled):
    found = {pin.APP_KEY: ref(APP, pinned, "1"), pin.UPDATER_KEY: ref(UPD, pinned, "2")}
    choice = launch.choose(found, ref(APP, bundled, "a"), ref(UPD, bundled, "b"))
    assert choice.updater == ref(UPD, pinned, "2")
    assert not any("updater is replaced" in s for s in choice.sentences)


@pytest.mark.parametrize(
    "pinned_updater",
    [ref(UPD, "0.8.0", "2"), f"{UPD}@sha256:{'2' * 64}"],
    ids=["same-version-other-digest", "no-version"],
)
def test_an_equal_or_unreadable_updater_keeps_the_pin_s(pinned_updater):
    found = {pin.APP_KEY: ref(APP, "0.8.0", "1"), pin.UPDATER_KEY: pinned_updater}
    choice = launch.choose(found, ref(APP, "0.8.0", "a"), ref(UPD, "0.8.0", "b"))
    assert choice.updater == pinned_updater


@pytest.mark.parametrize("bundled", ["0.8.0", "0.9.0"])
def test_a_pin_with_no_updater_runs_the_bundle_s(bundled):
    """A pin written before the updater knew its own digest names the app alone."""
    found = {pin.APP_KEY: ref(APP, "0.7.0", "1")}
    choice = launch.choose(found, ref(APP, bundled, "a"), ref(UPD, bundled, "b"))
    assert (choice.app, choice.updater) == (ref(APP, "0.7.0", "1"), ref(UPD, bundled, "b"))


@pytest.mark.parametrize(
    ("text", "version"),
    [
        (ref(APP, "0.8.0", "a"), (0, 8, 0)),
        (ref(UPD, "v0.10.2", "b"), (0, 10, 2)),
        ("localhost:5000/spend-tracker-updater:1.2.3", (1, 2, 3)),
        (f"{UPD}@sha256:{'c' * 64}", None),
        (f"{UPD}:latest", None),
    ],
)
def test_the_release_a_reference_names(text, version):
    assert launch.version_of(text) == version


# --------------------------------------------------------------------------- #
# 9.1: where the pin is found
# --------------------------------------------------------------------------- #


def _folder(root: Path, name: str, env: str | None = None, record: str | None = None) -> Path:
    d = root / name
    (d / "pin").mkdir(parents=True)
    if env is not None:
        (d / ".env").write_text(env)
    if record is not None:
        (d / "pin" / "release.env").write_text(record)
    return d


def test_the_pin_of_the_folder_the_project_was_started_from_wins(tmp_path):
    old = _folder(tmp_path, "old", env=f"X=1\n{pin.APP_KEY}={ref(APP, '0.9.0', '1')}\n")
    new = _folder(tmp_path, "new", env=f"{pin.APP_KEY}={ref(APP, '0.8.0', '2')}\n")
    assert launch.find_pin(new, old) == {pin.APP_KEY: ref(APP, "0.9.0", "1")}
    assert launch.find_pin(new) == {pin.APP_KEY: ref(APP, "0.8.0", "2")}


def test_the_record_stands_in_when_an_unzip_replaced_the_env(tmp_path):
    record = f"{pin.RECORD_HEADER}{pin.APP_KEY}={ref(APP, '0.9.0', '1')}\n{pin.UPDATER_KEY}={ref(UPD, '0.9.0', '2')}\n"
    here = _folder(tmp_path, "here", env="SPENDTRACKER_PUBLIC_URL=http://localhost:8848\n", record=record)
    assert launch.find_pin(here) == {
        pin.APP_KEY: ref(APP, "0.9.0", "1"),
        pin.UPDATER_KEY: ref(UPD, "0.9.0", "2"),
    }


def test_no_pin_anywhere_is_a_first_install(tmp_path):
    a = _folder(
        tmp_path, "a", env="SPENDTRACKER_PUBLIC_URL=http://localhost:8848\n# SPENDTRACKER_IMAGE=x\n"
    )
    b = _folder(tmp_path, "b", env=f"{pin.UPDATER_KEY}={ref(UPD, '0.9.0', '2')}\n")
    assert launch.find_pin(a) == {}
    assert launch.find_pin(a, b) == {}


# --------------------------------------------------------------------------- #
# 8.2: the per-engine settings, from the recorded fixtures
# --------------------------------------------------------------------------- #

#: fixture, working dir, socket gid -> engine, user, SOCKET_GID, podman-restart, linger, chgrp
SETTINGS = [
    ("docker-desktop", "/Users/Shared/spend-tracker", None, "docker-desktop", "65532:65532", "0", None, False, None),
    ("docker-engine-rootful", "/home/user/st", "989", "docker-engine", "65532:65532", "989", None, False, "989"),
    ("docker-engine-rootful", "/srv/st", "998", "docker-engine", "65532:65532", "998", None, False, "998"),
    ("docker-engine-rootless", "/home/user/st", "1000", "docker-engine", "0:0", "0", None, True, None),
    ("podman-rootful-fedora", "/root/st", "0", "podman", "65532:65532", "0", "system", False, "0"),
    ("podman-rootless-fedora", "/home/user/st", "1000", "podman", "0:0", "0", "user", True, None),
    ("podman-machine", "/Users/Shared/spend-tracker", None, "podman-machine", "65532:65532", "0", "user", False, None),
    # Podman Desktop's default machine is rootful (S21): its storage is a
    # rootful host's, and only the macOS path says it is a machine.
    ("podman-rootful-fedora", "/Users/Shared/spend-tracker", None, "podman-machine", "65532:65532", "0", "system", False, None),
]  # fmt: skip


@pytest.mark.parametrize(
    ("fixture", "wd", "gid", "engine", "user", "env_gid", "restart", "linger", "chgrp"), SETTINGS
)
def test_the_settings_for_each_recorded_engine(
    fixture, wd, gid, engine, user, env_gid, restart, linger, chgrp
):
    s = launch.settings_for(detection(fixture, wd), "/run/some.sock", gid)
    assert s.engine == engine
    assert s.env == {launch.SOCKET_KEY: "/run/some.sock", launch.GID_KEY: env_gid, launch.USER_KEY: user}
    assert (s.podman_restart, s.linger, s.chgrp) == (restart, linger, chgrp)


@pytest.mark.parametrize("gid", [None, "docker"])
def test_rootful_docker_engine_without_the_socket_s_numeric_group_is_refused(gid):
    with pytest.raises(ValueError, match="socket's group"):
        launch.settings_for(detection("docker-engine-rootful"), "/var/run/docker.sock", gid)


# --------------------------------------------------------------------------- #
# End to end, through the socket
# --------------------------------------------------------------------------- #

BUNDLE_ENV = "# The bundle's.\nSPENDTRACKER_PUBLIC_URL=http://localhost:8848\n"


def _args(
    project: Path,
    socket: str,
    previous: Path | None = None,
    gid: str | None = None,
    host: str = "/Users/Shared/st",
):
    return launch.parser().parse_args(
        [
            "--bundle-app", ref(APP, "0.9.0", "a"),
            "--bundle-updater", ref(UPD, "0.9.0", "b"),
            "--host-dir", host,
            "--engine-socket", "/var/run/docker.sock",
            "--socket", socket,
            "--project", str(project),
            *(["--previous", str(previous)] if previous else []),
            *(["--socket-gid", gid] if gid else []),
        ]
    )  # fmt: skip


def _answer(out: str) -> list[tuple[str, str]]:
    return [tuple(line.split("=", 1)) for line in out.splitlines()]  # type: ignore[misc]


def test_a_first_install_on_docker_desktop_writes_the_settings_and_no_pin(tmp_path):
    project = _folder(tmp_path, "st", env=BUNDLE_ENV)
    fake = FakeEngine(engine_fixture("docker-desktop"), info_of("docker-desktop"))
    with Running(fake) as running:
        status, out = launch.run(_args(project, running.socket_path))
    assert status == 0
    assert (project / ".env").read_text() == (
        BUNDLE_ENV
        + "SPENDTRACKER_ENGINE_SOCKET=/var/run/docker.sock\n"
        + "SPENDTRACKER_SOCKET_GID=0\n"
        + "SPENDTRACKER_UPDATER_USER=65532:65532\n"
    )
    assert _answer(out) == [
        ("ENGINE", "docker-desktop"),
        ("PODMAN_RESTART", ""),
        ("LINGER", "0"),
        ("CHGRP", ""),
        ("APP", ref(APP, "0.9.0", "a")),
        ("UPDATER", ref(UPD, "0.9.0", "b")),
        ("PLACARD", f"{engine.ROLE_LABEL}=placard"),
        # A first install: nothing made the project yet, the engine's compose stands.
        ("COMPOSE", ""),
    ]


def test_a_repair_from_a_new_folder_carries_the_old_pin_and_takes_the_newer_updater(tmp_path):
    old = _folder(
        tmp_path, "old",
        env=BUNDLE_ENV + f"{pin.APP_KEY}={ref(APP, '0.8.0', '1')}\n{pin.UPDATER_KEY}={ref(UPD, '0.8.0', '2')}\n",
    )  # fmt: skip
    new = _folder(tmp_path, "new", env=BUNDLE_ENV)
    os.chmod(new / ".env", 0o640)
    fake = FakeEngine(engine_fixture("podman-machine"), info_of("podman-machine"))
    with Running(fake) as running:
        status, out = launch.run(_args(new, running.socket_path, previous=old))
    assert status == 0
    env = (new / ".env").read_text()
    assert f"{pin.APP_KEY}={ref(APP, '0.8.0', '1')}\n" in env
    assert f"{pin.UPDATER_KEY}={ref(UPD, '0.9.0', 'b')}\n" in env
    assert env.startswith(BUNDLE_ENV)
    assert oct(os.stat(new / ".env").st_mode & 0o777) == "0o640"
    said = dict(_answer(out))
    assert said["APP"] == ref(APP, "0.8.0", "1")
    assert said["UPDATER"] == ref(UPD, "0.9.0", "b")
    assert said["PODMAN_RESTART"] == "user"
    # The old folder is read, never written.
    assert (old / ".env").read_text().endswith(f"{pin.UPDATER_KEY}={ref(UPD, '0.8.0', '2')}\n")


def test_a_second_run_changes_nothing(tmp_path):
    project = _folder(tmp_path, "st", env=BUNDLE_ENV + f"{pin.APP_KEY}={ref(APP, '0.9.5', '1')}\n")
    fake = FakeEngine(engine_fixture("docker-engine-rootful"), info_of("docker-engine-rootful"))
    with Running(fake) as running:
        first = launch.run(_args(project, running.socket_path, gid="989", host="/home/user/st"))
        after_first = (project / ".env").read_text()
        second = launch.run(_args(project, running.socket_path, gid="989", host="/home/user/st"))
    assert first == second
    assert (project / ".env").read_text() == after_first
    assert "SPENDTRACKER_SOCKET_GID=989\n" in after_first
    assert dict(_answer(first[1]))["CHGRP"] == "989"


@pytest.mark.parametrize("fixture", ["docker-desktop-windows", "podman-4.3"])
def test_a_refused_engine_stops_the_launcher_and_writes_nothing(tmp_path, fixture):
    project = _folder(tmp_path, "st", env=BUNDLE_ENV)
    fake = FakeEngine(engine_fixture(fixture), info_of(fixture))
    with Running(fake) as running:
        status, out = launch.run(_args(project, running.socket_path, host="/home/user/st"))
    if fixture == "podman-4.3":
        # Too old for self-update is said, and the app still starts.
        assert status == 0
        assert ("SAY", detect.sentence("too_old", name="Podman", version="4.3", floor="4.4")) in _answer(
            out
        )
        assert "SPENDTRACKER_UPDATER_USER=0:0\n" in (project / ".env").read_text()
    else:
        assert status == 2
        assert _answer(out) == [("SAY", detect.sentence("windows_containers"))]
        assert (project / ".env").read_text() == BUNDLE_ENV


@pytest.mark.parametrize("target", ["tcp://192.0.2.10:2375", "ssh://user@host"])
def test_a_tcp_engine_is_refused_before_anything_is_asked(tmp_path, target):
    project = _folder(tmp_path, "st", env=BUNDLE_ENV)
    args = _args(project, "/nonexistent.sock")
    args.engine_socket = target
    status, out = launch.run(args)
    assert (status, _answer(out)) == (2, [("SAY", detect.sentence("tcp_socket"))])
    assert (project / ".env").read_text() == BUNDLE_ENV


@pytest.mark.parametrize("value", ["a\nSAY=injected", "b\rc"])
def test_an_answer_never_carries_a_second_line(value):
    with pytest.raises(ValueError):
        launch.answer([("APP", value)])


# --------------------------------------------------------------------------- #
# #247: the compose that created the project
# --------------------------------------------------------------------------- #

#: Labels as each implementation writes them on a service container.
#: podman-compose writes compose's own as well as its own.
DOCKER_COMPOSE_LABELS = {
    "com.docker.compose.project": "spend-tracker",
    "com.docker.compose.config-hash": "f" * 64,
    "com.docker.compose.project.working_dir": "/home/user/st",
}
PODMAN_COMPOSE_LABELS = {
    "io.podman.compose.project": "spend-tracker",
    "io.podman.compose.config-hash": "e" * 64,
    "io.podman.compose.version": "1.5.0",
    "com.docker.compose.project": "spend-tracker",
    "com.docker.compose.project.working_dir": "/home/user/st",
}


def _listed(labels: dict, service: str, **more) -> dict:
    key = "io.podman.compose.service" if "io.podman.compose.project" in labels else "com.docker.compose.service"
    return {"Id": os.urandom(32).hex(), "Labels": {**labels, key: service, "com.docker.compose.service": service, **more}}


@pytest.mark.parametrize(
    ("labels", "made_by"),
    [(PODMAN_COMPOSE_LABELS, "podman-compose"), (DOCKER_COMPOSE_LABELS, "docker-compose")],
)
def test_the_compose_that_made_the_project_is_read_from_its_labels(labels, made_by):
    listing = [_listed(labels, "app"), _listed(labels, "updater")]
    assert launch.compose_of(listing) == made_by


def test_no_project_yet_leaves_the_engine_s_compose():
    assert launch.compose_of([]) is None
    # The updater's own one-offs (a maintenance page left over) are not compose's.
    oneoff = _listed(DOCKER_COMPOSE_LABELS, "app", **{"com.docker.compose.oneoff": "True", engine.ROLE_LABEL: "placard"})
    assert launch.compose_of([oneoff]) is None


def test_the_app_s_container_decides_over_any_other():
    """A second stack of the same project name, made by the other compose, is not
    what the launcher is starting: the app's own labels decide, then the updater's."""
    app = _listed(PODMAN_COMPOSE_LABELS, "app")
    stray = _listed(DOCKER_COMPOSE_LABELS, "tailscale")
    updater = _listed(DOCKER_COMPOSE_LABELS, "updater")
    assert launch.compose_of([stray, updater, app]) == "podman-compose"
    assert launch.compose_of([stray, updater]) == "docker-compose"


@pytest.mark.parametrize(
    ("fixture", "labels", "made_by"),
    [
        ("podman-machine", PODMAN_COMPOSE_LABELS, "podman-compose"),
        ("podman-machine", DOCKER_COMPOSE_LABELS, "docker-compose"),
        ("docker-desktop", DOCKER_COMPOSE_LABELS, "docker-compose"),
    ],
)
def test_the_launcher_is_told_which_compose_made_the_project(tmp_path, fixture, labels, made_by):
    """Through the socket: the project's containers as each compose labels them,
    the second with a running updater and a parked `-previous` beside the app."""
    project = _folder(tmp_path, "st", env=BUNDLE_ENV)
    fake = FakeEngine(engine_fixture(fixture), info_of(fixture))
    service_key = "io.podman.compose.service" if "io.podman.compose.project" in labels else "com.docker.compose.service"
    for name, service in (
        ("spend-tracker-app-1", "app"),
        ("spend-tracker-app-1-previous", "app"),
        ("spend-tracker-updater-1", "updater"),
    ):
        fake.add_container(
            name, "spend-tracker", labels={**labels, service_key: service, "com.docker.compose.service": service}
        )
    with Running(fake) as running:
        status, out = launch.run(_args(project, running.socket_path))
    assert status == 0
    assert dict(_answer(out))["COMPOSE"] == made_by
