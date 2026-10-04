"""The version, and the promise `/api/health` makes about it.

`pyproject.toml`, `client/package.json` and `app/__init__.py` each carried the
version independently, and the copy `/api/health` reports -- `app/__init__.py`
-- is the one nobody thinks to edit. An operator curling `/api/health` after an
upgrade to confirm which code is running is therefore relying on the field
least likely to be right.

In this codebase the fix for that is a test rather than a process.
"""

from __future__ import annotations

import re

import pytest

from app import __version__
from scripts.version import CLASSES, bumped, declared

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_the_three_files_agree():
    said = declared()
    assert len(set(said.values())) == 1, (
        "the version is written in several places and they have drifted:\n"
        + "\n".join(f"  {where}: {version}" for where, version in sorted(said.items()))
        + "\n`make version BUMP=...` moves them all together."
    )


def test_the_version_is_semver_because_the_release_classes_map_onto_it():
    assert SEMVER.match(__version__), f"{__version__!r} is not MAJOR.MINOR.PATCH"


def test_health_reports_the_version_the_files_declare(client):
    """The whole reason the three have to agree.

    `/api/health` is step 7 of the upgrade drill: one curl proving the running
    code is the version that was deployed. It only proves it if this holds.
    """
    from tests.conftest import HEADERS

    body = client.get("/api/health", headers=HEADERS).json()
    assert body["version"] == declared()["app/__init__.py"]
    assert body["status"] == "ok"


@pytest.mark.parametrize(
    ("kind", "was", "expected"),
    [
        # The review's three classes, mapped onto the three SemVer fields.
        ("major", "0.4.7", "1.0.0"),
        ("minor", "0.4.7", "0.5.0"),
        ("technical", "0.4.7", "0.4.8"),
    ],
)
def test_a_bump_resets_the_fields_below_it(kind, was, expected):
    """0.1.7 -> 0.2.7 would read as seven technical releases on a new branch."""
    assert bumped(was, kind) == expected


def test_every_release_class_the_review_named_has_a_field_to_move():
    """`major` / `minor` / `technical`, and nothing silently unmapped.

    An enum value with no designed behaviour is a bug with a menu item, and the
    same goes for a release class with nowhere to go.
    """
    assert set(CLASSES) == {"major", "minor", "technical"}
    assert sorted(CLASSES.values()) == [0, 1, 2]
