"""The release check: what a pull request into `main` has to have.

Each test builds the smallest CHANGELOG that breaks one rule and asserts the
message names it -- and one that breaks nothing and passes, so a check that
refuses everything cannot pass these either.
"""

from __future__ import annotations

import subprocess

from scripts import release_check
from scripts.release_check import problems

READY = """# Changelog

## Unreleased

---

## 0.2.0 — 2026-09-29

**Reversible: lossy** — `a4c7e19d2b86` drops every work-expense flag.
`c3e8a1f05d72` drops two indexes and loses nothing.

### Added

- Something.

---

## 0.1.0 — never tagged
"""

MIGRATIONS = ["a4c7e19d2b86", "c3e8a1f05d72"]


def _check(text=READY, *, version="0.2.0", tags=(), migrations=MIGRATIONS, tagged=None):
    return problems(text, version=version, tags=list(tags), migrations=list(migrations), tagged=tagged)


def test_a_ready_release_passes():
    assert _check() == []
    assert _check(tags=["v0.1.0"]) == []


def test_an_already_released_version_is_refused():
    [said] = _check(tags=["v0.1.0", "v0.2.0"])
    assert "v0.2.0 is already released" in said


def test_a_version_behind_the_newest_tag_is_refused():
    [said] = _check(tags=["v0.3.0"])
    assert "not newer than the newest tag, v0.3.0" in said


def test_entries_left_under_unreleased_are_refused():
    text = READY.replace("## Unreleased\n", "## Unreleased\n\n- Forgotten.\n")
    [said] = _check(text)
    assert "## Unreleased still has entries" in said


def test_no_section_for_the_version_is_refused():
    [said] = _check(version="0.3.0")
    assert "no section for 0.3.0" in said


def test_a_section_still_marked_unreleased_is_refused():
    text = READY.replace("## 0.2.0 — 2026-09-29", "## 0.2.0 — unreleased")
    [said] = _check(text)
    assert "needs a release date" in said and "still says unreleased" in said


def test_a_section_without_a_date_is_refused():
    text = READY.replace("## 0.2.0 — 2026-09-29", "## 0.2.0")
    [said] = _check(text)
    assert "needs a release date" in said


def test_a_migration_the_notes_forget_is_named():
    said = _check(migrations=[*MIGRATIONS, "b7d2e5a91c63"])
    assert said == [
        "## 0.2.0 does not name 1 migration(s) added in this release, each with what "
        "rolling it back loses: b7d2e5a91c63"
    ]


def test_no_reversible_line_is_refused():
    text = READY.replace("**Reversible: lossy**", "Reversible, probably")
    assert "has no **Reversible" in _check(text)[0]


def test_reversible_none_with_migrations_is_refused():
    text = READY.replace("**Reversible: lossy**", "**Reversible: none**")
    [said] = _check(text)
    assert "Reversible: none" in said and "2 migration(s)" in said


def test_every_problem_is_reported_at_once():
    text = READY.replace("## Unreleased\n", "## Unreleased\n\n- Forgotten.\n").replace(
        "2026-09-29", "unreleased"
    )
    assert len(_check(text, tags=["v0.2.0"], migrations=["ffffffffffff"])) == 4


def test_on_the_tag_the_tag_must_match_and_existing_is_expected():
    assert _check(tags=["v0.2.0"], tagged="v0.2.0", migrations=[]) == []
    [said] = _check(tags=["v0.2.1"], tagged="v0.2.1", migrations=[])
    assert "the tag is v0.2.1 but app/__init__.py says 0.2.0" in said


def _commit(repo, message: str) -> str:
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", message)
    return git("rev-parse", "HEAD")


def test_added_migrations_reads_revision_ids_from_the_files(tmp_path, monkeypatch):
    """In a history of its own, not this checkout's.

    It used to walk this repository back to its first commit, which CI's
    shallow checkout does not have: there, HEAD *is* the root, nothing was
    added since, and the test failed on the 0.2.0 release PR having passed on
    every full clone.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    versions = tmp_path / "migrations" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_first.py").write_text('revision: str = "0000000000a1"\n')
    before = _commit(tmp_path, "first")
    (versions / "0002_second.py").write_text('revision: str = "0000000000b2"\n')
    (versions / "0003_third.py").write_text(
        'revision: str | None = "0000000000a3"\ndown_revision = "0000000000b2"\n'
    )
    (versions / "README").write_text("not a migration\n")
    _commit(tmp_path, "second and third")

    monkeypatch.setattr(release_check, "ROOT", tmp_path)
    assert release_check.added_migrations(before) == ["0000000000a3", "0000000000b2"]


def test_this_repository_ready_or_not_is_answered(capsys):
    """The CLI runs end to end against the real CHANGELOG and says one or the other."""
    code = release_check.main(["--tagged", "v0.0.0"])
    out = capsys.readouterr().out
    assert code == 1
    assert "the tag is v0.0.0" in out
