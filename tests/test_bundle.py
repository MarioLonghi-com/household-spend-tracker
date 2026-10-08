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
  never `sudo` on the host; the `.bat` in CRLF and saying it is untested (D2);
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


def test_the_windows_launcher_is_crlf_and_says_it_is_untested(tmp_path):
    with zipfile.ZipFile(_build(tmp_path, *RELEASES[0])) as f:
        raw = f.read("spend-tracker-0.9.0/Start Spend Tracker.bat")
    assert raw.count(b"\r\n") == raw.count(b"\n") > 50
    assert b"UNTESTED" in raw[:400]
    assert "Windows is untested" in _read(_build(tmp_path / "2", *RELEASES[1]), "README.txt")


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
