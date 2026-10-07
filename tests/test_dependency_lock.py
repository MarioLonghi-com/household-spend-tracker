"""The Python dependencies are locked, and the lock is what gets compared (#46).

`requirements*.in` hold the floors a person edits; `requirements*.txt` are
what `make lock` compiles from them, every package pinned exactly and hashed,
and what every install reads with `--require-hashes`. CI's `lock` job proves
the two are in step. These say the locks are the shape the installs rely on,
and that `scripts/lock_matches.py` -- what tests.yml's `image` job uses to
compare the image with the lock -- reports what it should.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.lock_matches import differences, installed, locked

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

ROOT = Path(__file__).resolve().parent.parent
LINUX = {
    "sys_platform": "linux",
    "platform_system": "Linux",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
}
WINDOWS = {**LINUX, "sys_platform": "win32", "platform_system": "Windows"}


def _packages(lock: str) -> list[list[str]]:
    """Each package entry of a compiled lock, as its lines."""
    entries: list[list[str]] = []
    for line in lock.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            entries.append([line])
        else:
            entries[-1].append(line)
    return entries


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
def test_every_package_in_a_lock_is_pinned_exactly_and_hashed(name):
    entries = _packages((ROOT / name).read_text(encoding="utf-8"))
    assert len(entries) > 30, f"{name} has {len(entries)} packages; is it a lock at all?"
    loose = [e[0] for e in entries if not re.match(r"^[A-Za-z0-9._-]+==\S+", e[0])]
    unhashed = [e[0] for e in entries if not any("--hash=sha256:" in line for line in e)]
    assert loose == [], f"not pinned with == in {name}: {loose}"
    assert unhashed == [], f"no hash in {name}: {unhashed}"


def test_the_runtime_lock_is_what_the_dev_lock_installs_too():
    runtime = locked((ROOT / "requirements.txt").read_text(encoding="utf-8"), LINUX)
    dev = locked((ROOT / "requirements-dev.txt").read_text(encoding="utf-8"), LINUX)
    drifted = {n: (v, dev.get(n)) for n, v in runtime.items() if dev.get(n) != v}
    assert drifted == {}, f"the suite would test other versions than a deployment runs: {drifted}"


def test_what_the_app_imports_directly_is_declared_directly():
    floors = (ROOT / "requirements.in").read_text(encoding="utf-8")
    declared = {re.split(r"[<>=\[ ]", line, maxsplit=1)[0].lower() for line in floors.splitlines()
                if line and not line.startswith("#")}
    assert {"starlette", "certifi"} <= declared


LOCK = """\
alpha==1.0 \\
    --hash=sha256:aa
    # via -r requirements.in
Beta_Two==2.0 \\
    --hash=sha256:bb
colorama==0.4.6 ; sys_platform == 'win32' \\
    --hash=sha256:cc
pip==26.0 \\
    --hash=sha256:dd
"""


def test_a_marker_decides_whether_a_pin_applies_here():
    assert locked(LOCK, LINUX) == {"alpha": "1.0", "beta-two": "2.0", "pip": "26.0"}
    assert locked(LOCK, WINDOWS)["colorama"] == "0.4.6"


def test_an_environment_that_matches_the_lock_has_no_differences():
    have = installed("alpha==1.0\nbeta-two==2.0\npip==25.1\nsetuptools==80.0\n")
    # The venv's own pip differs from the lock's and is not a difference.
    assert differences(locked(LOCK, LINUX), have) == []


def test_every_way_an_environment_can_differ_is_named():
    have = installed("alpha==1.1\nextra==3.0\n")
    assert differences(locked(LOCK, LINUX), have) == [
        "alpha: locked at 1.0, 1.1 installed",
        "beta-two: locked at 2.0, not installed",
        "extra: 3.0 installed, not in the lock",
    ]
