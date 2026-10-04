"""The upgrade rehearsal's "before", on a commit that has no parent.

`tests.yml` works out which commit to build the "before" database from. On a
push it took `git rev-parse HEAD^ || git rev-parse HEAD` -- but on a root commit
the first half fails *and still prints* `HEAD^`, so the step wrote two lines to
`$GITHUB_OUTPUT` and the job died on "Invalid format". A repository whose
history starts at one commit hits that on its first push, and since `main` stays
a root commit until the next release, no rerun could ever turn it green.

These run the step's own shell, lifted out of the workflow, against two
repositories: one with a parent and one without.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
STEP = 'work out what "before" is'


def _step_script() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"- name: {STEP}")
    block = text[start:]
    block = block[: block.index("\n      - ", 1)]  # up to the next step
    body = block.split("run: |\n", 1)[1]
    return textwrap.dedent(body)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path, commits: int) -> Path:
    repo = tmp_path / f"repo-{commits}"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for n in range(commits):
        (repo / "f.txt").write_text(f"{n}\n", encoding="utf-8")
        _git(repo, "add", "f.txt")
        _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", f"c{n}")
    return repo


def _run_step(repo: Path, tmp_path: Path) -> list[str]:
    output = tmp_path / f"output-{repo.name}"
    output.write_text("", encoding="utf-8")
    subprocess.run(
        ["bash", "-e", "-c", _step_script()],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "GITHUB_OUTPUT": str(output),
            "BASE_REF": "",
        },
    )
    return output.read_text(encoding="utf-8").splitlines()


def test_a_root_commit_is_its_own_before(tmp_path):
    repo = _repo(tmp_path, commits=1)
    lines = _run_step(repo, tmp_path)
    assert lines == [f"sha={_git(repo, 'rev-parse', 'HEAD')}"]


def test_a_commit_with_a_parent_rehearses_from_the_parent(tmp_path):
    repo = _repo(tmp_path, commits=2)
    lines = _run_step(repo, tmp_path)
    assert lines == [f"sha={_git(repo, 'rev-parse', 'HEAD^')}"]
    assert lines != [f"sha={_git(repo, 'rev-parse', 'HEAD')}"]
