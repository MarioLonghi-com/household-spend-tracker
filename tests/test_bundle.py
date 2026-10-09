"""The release zip for a personal computer (#167, design notes Part 13, C13-C16).

Built by `scripts/bundle.py` from `deploy/bundle/`, two releases with two sets
of digests each time, and read back the way a person's computer reads it:

- the compose file, parsed: both images by this release's digest, no
  `build:` (C15), `mem_limit: 768m` (C14), the updater, `x-podman`, the
  pinned project name, and the repository's own compose.yaml's security
  settings unchanged;
- `.env`: `SPENDTRACKER_PUBLIC_URL=http://localhost:8848` (C13), nothing secret;
- the launchers: executable in the zip (C16), naming the same two images,
  `cd` first (S1), placards removed before `up` (R30), localhost opened,
  never `sudo` on the host; the `.bat` in CRLF and saying what is untested (D2);
- `--check` catching each way a zip goes wrong;
- shellcheck, where it is installed (CI's runners have it).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts import bundle

pytestmark = pytest.mark.repo_wide

ROOT = Path(__file__).resolve().parent.parent
RELEASES = [("0.9.0", "a", "b"), ("1.12.3", "c", "d")]


def _digest(fill: str) -> str:
    return "sha256:" + fill * 64


def _build(tmp_path: Path, version: str, app_fill: str, upd_fill: str) -> Path:
    app = bundle.image(bundle.APP_REPO, version, _digest(app_fill))
    upd = bundle.image(bundle.UPDATER_REPO, version, _digest(upd_fill))
    return bundle.build(version, app, upd, tmp_path, epoch=1_791_500_000)


def _read(z: Path, name: str) -> str:
    with zipfile.ZipFile(z) as f:
        top = f.namelist()[0].split("/", 1)[0]
        return f.read(f"{top}/{name}").decode()


# --------------------------------------------------------------------------- #
# compose.yaml
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("version", "a", "u"), RELEASES)
def test_the_compose_file_names_both_images_by_this_release_s_digest(tmp_path, version, a, u):
    doc = yaml.safe_load(_read(_build(tmp_path, version, a, u), "compose.yaml"))
    services = doc["services"]
    assert services["app"]["image"] == (
        f"${{SPENDTRACKER_IMAGE:-{bundle.APP_REPO}:{version}@{_digest(a)}}}"
    )
    assert services["updater"]["image"] == (
        f"${{SPENDTRACKER_UPDATER_IMAGE:-{bundle.UPDATER_REPO}:{version}@{_digest(u)}}}"
    )


@pytest.mark.parametrize(("version", "a", "u"), RELEASES)
def test_the_compose_file_has_no_build_a_memory_ceiling_and_no_pod(tmp_path, version, a, u):
    doc = yaml.safe_load(_read(_build(tmp_path, version, a, u), "compose.yaml"))
    assert [name for name, s in doc["services"].items() if "build" in s] == []
    assert doc["services"]["app"]["mem_limit"] == "768m"
    assert doc["x-podman"] == {"in_pod": False}
    assert doc["name"] == "spend-tracker"
    assert set(doc["services"]) == {"app", "updater"}
    assert doc["services"]["app"]["environment"]["SPENDTRACKER_PUBLIC_URL"] == (
        "${SPENDTRACKER_PUBLIC_URL:-http://localhost:8848}"
    )


#: What the bundle's file must keep exactly as the repository's compose.yaml has it.
SAME = {
    "app": ("ports", "volumes", "read_only", "tmpfs", "security_opt", "cap_drop", "restart"),
    "updater": ("user", "group_add", "volumes", "read_only", "tmpfs", "security_opt", "cap_drop", "mem_limit", "restart"),
}  # fmt: skip


@pytest.mark.parametrize("service", sorted(SAME))
def test_the_bundle_s_services_keep_the_repository_s_settings(tmp_path, service):
    mine = yaml.safe_load(_read(_build(tmp_path, *RELEASES[0]), "compose.yaml"))["services"][service]
    theirs = yaml.safe_load((ROOT / "compose.yaml").read_text())["services"][service]
    assert {k: mine.get(k) for k in SAME[service]} == {k: theirs.get(k) for k in SAME[service]}
    if service == "app":
        assert set(mine["environment"]) == set(theirs["environment"])
        assert {k: v for k, v in mine["environment"].items() if k != "SPENDTRACKER_PUBLIC_URL"} == {
            k: v for k, v in theirs["environment"].items() if k != "SPENDTRACKER_PUBLIC_URL"
        }


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("version", "a", "u"), RELEASES)
def test_the_env_sets_the_public_url_and_nothing_else(tmp_path, version, a, u):
    env = _read(_build(tmp_path, version, a, u), ".env")
    settings = [line for line in env.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert settings == ["SPENDTRACKER_PUBLIC_URL=http://localhost:8848"]
    assert not re.search(r"AUTHKEY|PASSWORD|SECRET|TOKEN", env.upper().replace("NOTHING SECRET", ""))


# --------------------------------------------------------------------------- #
# The zip and the launchers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("version", "a", "u"), RELEASES)
def test_the_zip_is_one_folder_and_the_launchers_stay_executable(tmp_path, version, a, u):
    z = _build(tmp_path, version, a, u)
    assert z.name == f"spend-tracker-{version}-compose.zip"
    assert bundle.problems(z) == []
    out = tmp_path / "unzipped"
    with zipfile.ZipFile(z) as f:
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in f.infolist()}
        f.extractall(out)
    top = f"spend-tracker-{version}"
    assert modes[f"{top}/Start Spend Tracker.command"] == 0o755
    assert modes[f"{top}/start-spend-tracker.sh"] == 0o755
    assert modes[f"{top}/Start Spend Tracker.bat"] == 0o644
    assert modes[f"{top}/pin/"] == 0o775
    assert sorted(p.name for p in (out / top).iterdir()) == sorted(
        [".env", "README.txt", "Start Spend Tracker.bat", "Start Spend Tracker.command", "compose.yaml", "pin", "start-spend-tracker.sh"]
    )  # fmt: skip
    assert list((out / top / "pin").iterdir()) == []


@pytest.mark.parametrize(("version", "a", "u"), RELEASES)
def test_every_launcher_names_the_compose_file_s_two_images(tmp_path, version, a, u):
    z = _build(tmp_path, version, a, u)
    app = f"{bundle.APP_REPO}:{version}@{_digest(a)}"
    upd = f"{bundle.UPDATER_REPO}:{version}@{_digest(u)}"
    sh = _read(z, "Start Spend Tracker.command")
    assert sh == _read(z, "start-spend-tracker.sh")
    assert f"APP_IMAGE='{app}'" in sh and f"UPDATER_IMAGE='{upd}'" in sh
    bat = _read(z, "Start Spend Tracker.bat")
    assert f'set "APP_IMAGE={app}"' in bat and f'set "UPDATER_IMAGE={upd}"' in bat


def test_the_windows_launcher_is_crlf_and_says_what_is_untested(tmp_path):
    with zipfile.ZipFile(_build(tmp_path, *RELEASES[0])) as f:
        raw = f.read("spend-tracker-0.9.0/Start Spend Tracker.bat")
    assert raw.count(b"\r\n") == raw.count(b"\n") > 50
    assert b"UNTESTED" in raw[:400]
    readme = _read(_build(tmp_path / "2", *RELEASES[1]), "README.txt")
    assert "With Podman on Windows it has not been tried yet" in readme
    # The first-run warning is explained where the owner double-clicks (#170).
    step3 = readme[readme.index("3. Double-click"):readme.index("4. The wizard")]
    assert "Open Anyway" in step3 and "Run anyway" in step3


LAUNCHER = (ROOT / "deploy" / "bundle" / "start-spend-tracker.sh").read_text()
BAT = (ROOT / "deploy" / "bundle" / "Start Spend Tracker.bat").read_text()


def _first(text: str, needle: str) -> int:
    at = text.find(needle)
    assert at >= 0, needle
    return at


@pytest.mark.parametrize(
    ("text", "cd", "placards", "up"),
    [
        (LAUNCHER, 'cd "$(dirname "$0")"', 'rm -f "$id"', '"$ENGINE" compose --env-file .env up -d'),
        (BAT, 'cd /d "%~dp0"', "rm -f %%i", "%ENGINE% compose --env-file .env up -d"),
    ],
    ids=["sh", "bat"],
)
def test_each_launcher_cds_first_and_clears_placards_before_up(text, cd, placards, up):
    body = text[text.find("\n\n") :]  # after the header
    assert _first(body, cd) < _first(body, "compose version")
    assert _first(body, cd) < _first(body, "updater.launch")
    assert _first(body, "updater.launch") < _first(body, placards) < _first(body, up)
    assert "com.docker.compose.oneoff=True" in text
    assert "PLACARD" in text


def test_under_podman_the_launcher_removes_the_running_app_and_updater_before_up():
    """podman-compose's `up` cannot replace a running container and exits 0
    anyway (#169, E15 on rootless Podman): the launcher takes them away first,
    and never the parked ones."""
    body = LAUNCHER[LAUNCHER.find("\n\n") :]
    block = body[_first(body, 'if [ "$ENGINE" = podman ]; then\n  for svc in updater app') :]
    assert _first(body, 'rm -f "$id"') < _first(body, "for svc in updater app")
    assert _first(block, '"$ENGINE" stop "$id"') < _first(block, '"$ENGINE" rm "$id"')
    assert _first(block, '"$ENGINE" rm "$id"') < _first(block, '"$ENGINE" compose --env-file .env up -d')
    assert "*-previous|*-next) continue" in block[: _first(block, "up -d")]
    assert 'rm -f "$id"' not in block[: _first(block, "up -d")]  # stopped first, never killed


def test_the_launcher_stops_a_standby_updater_before_it_replaces_the_updater():
    """H6's standby took back over while compose replaced the updater (#169, E15)."""
    body = LAUNCHER[LAUNCHER.find("\n\n") :]
    standby = _first(body, '*-previous) "$ENGINE" stop "$id"')
    assert _first(body, "label=com.docker.compose.service=updater") < standby
    assert standby < _first(body, "for svc in updater app") < _first(body, '"$ENGINE" compose --env-file .env up -d')


@pytest.mark.parametrize("text", [LAUNCHER, BAT], ids=["sh", "bat"])
def test_each_launcher_opens_localhost_and_probes_health(text):
    assert re.search(r"URL=.?http://localhost:8848", text)
    assert "/api/health" in text
    assert '127.0.0.1:8848"' not in text.replace("PROBE='http://127.0.0.1:8848/api/health'", "")


def test_the_launcher_never_runs_sudo_on_the_host():
    """It says which command needs sudo and stops (#167). Inside a podman machine, sudo is the VM's."""
    for line in LAUNCHER.splitlines():
        if "sudo" in line and not line.lstrip().startswith("#"):
            assert (
                "stop " in line or "podman machine ssh" in line or line.strip().startswith('|| stop "')
            ), line


# --------------------------------------------------------------------------- #
# #247: the compose that created the project, run headless against stubs
# --------------------------------------------------------------------------- #

ENGINE_STUB = """#!/bin/sh
# A stand-in for `docker` or `podman`: logs what it was asked, answers enough.
printf '%s|%s\\n' "$(basename "$0") $*" "${PODMAN_COMPOSE_PROVIDER-}" >> "$STUB_LOG"
# What this engine is (#264, #300, `launch.ENGINE_STATES`): it answers, unless
# told otherwise; and, for an install, <ENGINE>_DIR (the folder its containers
# were started from), _IMAGE, _STARTED and _PORT (it publishes the port).
me="$(basename "$0")"
case "$me" in
  docker) state="${DOCKER_STATE-answers}" dir="${DOCKER_DIR-}" image="${DOCKER_IMAGE-}" started="${DOCKER_STARTED-}" port="${DOCKER_PORT-}" ;;
  podman) state="${PODMAN_STATE-answers}" dir="${PODMAN_DIR-}" image="${PODMAN_IMAGE-}" started="${PODMAN_STARTED-}" port="${PODMAN_PORT-}" ;;
  *) state=answers dir="" image="" started="" port="" ;;
esac
[ "$state" = none ] && exit 1
if [ "$1" = info ]; then
  case "$state" in
    installed) echo "Cannot connect to the engine. Is it running?" >&2; exit 1 ;;
    denied) echo "permission denied while trying to connect to the socket" >&2; exit 1 ;;
  esac
fi
P="--filter label=com.docker.compose.project=spend-tracker"
case "$state" in project|running)
  case "$*" in
    "ps -aq $P") echo "${me}-app" ;;
    "ps -aq $P --filter label=com.docker.compose.service=app") echo "${me}-app" ;;
    "ps -q $P --filter label=com.docker.compose.service=app") [ "$state" = running ] && echo "${me}-app" ;;
    "ps $P --format {{.Ports}}") [ -n "$port" ] && echo "127.0.0.1:${port}->8848/tcp" ;;
    *working_dir*) echo "$dir" ;;
    "inspect --format {{.Name}} ${me}-app") echo "/spend-tracker-app-1" ;;
    "inspect --format {{.Config.Image}} ${me}-app") echo "$image" ;;
    "inspect --format {{.State.StartedAt}} ${me}-app") echo "$started" ;;
  esac ;;
esac
case "$1 $2" in
  "compose version") exit 0 ;;
  "info --format") echo "Docker Desktop" ;;
  "compose --env-file") exit "${STUB_UP_STATUS-0}" ;;
esac
case "$1" in
  run) cat "$STUB_ANSWER"; exit "${STUB_STATUS-0}" ;;
esac
exit 0
"""
#: curl: the health probe answers; the port check (any other URL) finds the
#: port free, exit 7, unless PORT_TAKEN is set.
CURL_STUB = """#!/bin/sh
printf '%s|\\n' "curl $*" >> "$STUB_LOG"
case "$*" in *"/api/health"*) exit 0 ;; esac
[ -n "${PORT_TAKEN-}" ] && exit 0
# Taken only once `compose up` has been asked: by then the cause is known.
if [ -n "${PORT_TAKEN_AFTER_UP-}" ]; then
  case "$(cat "$STUB_LOG")" in *"compose --env-file .env up -d"*) exit 0 ;; esac
fi
exit 7
"""
OK_STUB = "#!/bin/sh\nexit 0\n"


def _launch_headless(
    tmp_path: Path,
    installed: tuple[str, ...],
    made_by: str,
    kind: str = "podman-machine",
    states: dict[str, str] | None = None,
    answer_text: str | None = None,
    answer_status: int = 0,
    extra_env: dict[str, str] | None = None,
):
    """The shell launcher, run as a person runs it, against stubs of what is `installed`.

    Returns (exit status, output, the engine calls `compose ... up` made, each
    with the PODMAN_COMPOSE_PROVIDER it ran under)."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for tool in ("dirname", "date", "basename", "cat", "id"):
        found = shutil.which(tool)
        assert found, tool
        (stubs / tool).symlink_to(found)
    for name in installed:
        (stubs / name).write_text(ENGINE_STUB if name in ("docker", "podman") else OK_STUB)
    # The Linux path, wherever the test runs: Darwin's adds Docker Desktop's CLIs to PATH.
    for name, text in (("uname", "#!/bin/sh\necho Linux\n"), ("curl", CURL_STUB), ("xdg-open", OK_STUB)):
        (stubs / name).write_text(text)
    for f in stubs.iterdir():
        if not f.is_symlink():
            f.chmod(0o755)
    folder = tmp_path / "Spend Tracker"
    folder.mkdir()
    shutil.copy(ROOT / "deploy" / "bundle" / "start-spend-tracker.sh", folder / "start-spend-tracker.sh")
    (folder / ".env").write_text("SPENDTRACKER_PUBLIC_URL=http://localhost:8848\n")
    answer = tmp_path / "answer.txt"
    answer.write_text(
        f"ENGINE={kind}\nPODMAN_RESTART=\nLINGER=0\nCHGRP=\nAPP=app@sha256:{'a' * 64}\n"
        f"UPDATER=upd@sha256:{'b' * 64}\nPLACARD=role=placard\nCOMPOSE={made_by}\n"
    )
    if answer_text is not None:
        answer.write_text(answer_text)
    log = tmp_path / "calls.log"
    log.touch()
    env = {"PATH": str(stubs), "HOME": str(tmp_path), "STUB_LOG": str(log), "STUB_ANSWER": str(answer)}
    env.update({f"{name.upper()}_STATE": state for name, state in (states or {}).items()})
    env["STUB_STATUS"] = str(answer_status)
    env.update(extra_env or {})
    bash = shutil.which("bash") or "/bin/bash"
    done = subprocess.run(
        [bash, str(folder / "start-spend-tracker.sh")],
        cwd=folder, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    ups = [line for line in log.read_text().splitlines() if " compose " in line and "up -d" in line]
    return done.returncode, done.stdout + done.stderr, ups, stubs, log.read_text()


@pytest.mark.parametrize("made_by", ["podman-compose", "docker-compose"])
def test_with_both_composes_installed_the_launcher_runs_the_one_that_made_the_project(tmp_path, made_by):
    """`podman compose` would hand a podman-compose stack to docker-compose,
    which refuses it: the launcher names the provider (#247)."""
    status, out, ups, stubs, _ = _launch_headless(tmp_path, ("podman", "podman-compose", "docker-compose"), made_by)
    assert status == 0, out
    assert ups == [f"podman compose --env-file .env up -d|{stubs / made_by}"], ups
    assert f"Using {made_by}, which created this Spend Tracker." in out


def test_a_first_install_leaves_podman_s_own_choice(tmp_path):
    status, out, ups, _, _ = _launch_headless(tmp_path, ("podman", "podman-compose", "docker-compose"), "")
    assert status == 0, out
    assert ups == ["podman compose --env-file .env up -d|"], ups


def test_a_project_podman_compose_made_without_podman_compose_stops_before_up(tmp_path):
    status, out, ups, _, _ = _launch_headless(tmp_path, ("podman", "docker-compose"), "podman-compose")
    assert status == 1 and ups == [], (out, ups)
    assert "created with podman-compose, which was not found" in out


def test_under_docker_its_own_compose_runs_the_project_docker_compose_made(tmp_path):
    status, out, ups, _, _ = _launch_headless(
        tmp_path, ("docker", "podman", "podman-compose", "docker-compose"), "docker-compose", kind="docker-desktop"
    )
    assert status == 0, out
    assert ups == ["docker compose --env-file .env up -d|"], ups


# --------------------------------------------------------------------------- #
# --check
# --------------------------------------------------------------------------- #


def _rewrite(src: Path, dst: Path, change) -> Path:
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(dst, "w") as b:
        for info in a.infolist():
            data = a.read(info)
            info, data = change(info, data)
            b.writestr(info, data)
    return dst


def test_check_catches_a_lost_executable_bit(tmp_path):
    good = _build(tmp_path, *RELEASES[0])

    def strip(info, data):
        if info.filename.endswith(".command"):
            info.external_attr = 0o100644 << 16
        return info, data

    (tmp_path / "x").mkdir()
    bad = _rewrite(good, tmp_path / "x" / good.name, strip)
    assert bundle.problems(bad) == ["Start Spend Tracker.command is mode 0o644, not 0o755"]


def test_check_catches_a_build_section_and_a_placeholder(tmp_path):
    good = _build(tmp_path, *RELEASES[1])

    def spoil(info, data):
        if info.filename.endswith("compose.yaml"):
            data = data.replace(
                b"    restart: unless-stopped\n", b"    build: .\n    restart: unless-stopped\n", 1
            )
        if info.filename.endswith(".env"):
            data += b"X=@VERSION@\n"
        return info, data

    (tmp_path / "x").mkdir()
    bad = _rewrite(good, tmp_path / "x" / good.name, spoil)
    assert bundle.problems(bad) == [".env still holds a placeholder", "compose.yaml has a build: section"]


def test_check_refuses_images_that_are_not_this_release_by_digest(tmp_path):
    z = bundle.build(
        "0.9.0", "spend-tracker-e2e-app:0.9.0", f"{bundle.UPDATER_REPO}:0.8.0@{_digest('b')}", tmp_path
    )
    found = bundle.problems(z)
    assert found == [
        "spend-tracker-e2e-app:0.9.0 is not this release by digest",
        f"{bundle.UPDATER_REPO}:0.8.0@{_digest('b')} is not this release by digest",
    ]
    assert bundle.problems(z, local=True) == []


@pytest.mark.parametrize("digest", ["sha256:abc", "md5:" + "a" * 64])
def test_a_digest_that_is_not_one_is_refused(digest):
    with pytest.raises(SystemExit):
        bundle.image(bundle.APP_REPO, "0.9.0", digest)


# --------------------------------------------------------------------------- #
# shellcheck
# --------------------------------------------------------------------------- #


def test_shellcheck_passes_the_launcher():
    if shutil.which("shellcheck") is None:
        if os.environ.get("CI"):
            pytest.fail("shellcheck is not installed on this runner")
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        ["shellcheck", "-s", "bash", str(ROOT / "deploy" / "bundle" / "start-spend-tracker.sh")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout
