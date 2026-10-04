"""Which commit is running: what the Application screen and `/api/health` say.

Three things are asserted. The answer is the real commit when this is a working
copy; a git repository that merely *contains* the installation is not asked;
and a build with no `.git` -- the tarball, the image -- answers from the stamp
that `scripts.build_stamp` writes, and says "unknown" rather than guessing when
there is neither.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from app import build
from scripts import build_stamp
from tests.conftest import HEADERS, _setup_owner


def _head() -> str:
    return subprocess.run(
        ["git", "-C", str(build.ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def a_stamp(tmp_path, monkeypatch):
    """STAMP pointed somewhere a test can write, never at the real app/."""
    stamp = tmp_path / "build.json"
    monkeypatch.setattr(build, "STAMP", stamp)
    return stamp


def test_a_working_copy_answers_with_its_own_head():
    found = build.from_git()
    assert found is not None
    assert found.commit == _head()
    assert found.source == "git"
    assert found.committed_at and found.committed_at[:4].isdigit()
    assert isinstance(found.dirty, bool)


def test_a_repository_that_only_contains_the_installation_is_not_asked(tmp_path, monkeypatch):
    """A tarball unpacked inside somebody's dotfiles repo is inside git too."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    inside = tmp_path / "spend-tracker-0.1.0"
    inside.mkdir()
    monkeypatch.setattr(build, "ROOT", inside)
    assert build.from_git() is None


def test_no_git_at_all_is_none_rather_than_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "ROOT", tmp_path)
    assert build.from_git() is None


def test_the_stamp_is_read_when_there_is_one(a_stamp):
    a_stamp.write_text(
        json.dumps({"commit": "a" * 40, "branch": None, "committed_at": "2026-09-28T22:19:17+02:00", "dirty": False})
    )
    found = build.from_stamp()
    assert found == build.Build(
        commit="a" * 40, branch=None, committed_at="2026-09-28T22:19:17+02:00", dirty=False, source="stamp"
    )


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"commit": ""}', '{"branch": "dev"}'])
def test_a_stamp_that_says_nothing_is_ignored(a_stamp, text):
    a_stamp.write_text(text)
    assert build.from_stamp() is None


def test_nothing_to_ask_is_unknown_not_a_guess(tmp_path, monkeypatch, a_stamp):
    monkeypatch.setattr(build, "ROOT", tmp_path)
    build.current.cache_clear()
    try:
        assert build.current() == build.Build(source="unknown")
        assert build.short(build.current()) is None
    finally:
        build.current.cache_clear()


def test_git_wins_over_a_stale_stamp(a_stamp):
    """A stamp left behind in a working copy must not outrank the checkout."""
    a_stamp.write_text(json.dumps({"commit": "b" * 40}))
    build.current.cache_clear()
    try:
        assert build.current().commit == _head()
    finally:
        build.current.cache_clear()


def test_the_stamp_script_writes_what_git_says(a_stamp):
    assert build_stamp.main() == 0
    written = json.loads(a_stamp.read_text())
    assert written["commit"] == _head()
    assert "source" not in written
    assert build.from_stamp().commit == _head()


def test_the_stamp_script_refuses_without_git(tmp_path, monkeypatch, a_stamp):
    monkeypatch.setattr(build, "ROOT", tmp_path)
    assert build_stamp.main() == 1
    assert not a_stamp.exists()


def test_health_reports_the_short_commit(client):
    body = client.get("/api/health").json()
    assert body["commit"] == build.short(build.current())
    assert "branch" not in body and "dirty" not in body


def test_the_application_screen_carries_the_whole_build(client):
    _setup_owner(client)
    answer = client.get("/api/admin/application", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    said = answer.json()["build"]
    running = build.current()
    assert said["commit"] == running.commit
    assert said["branch"] == running.branch
    assert said["dirty"] == running.dirty
    assert said["source"] == running.source
