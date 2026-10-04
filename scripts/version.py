"""The version, in one place, with every copy kept honest.

`pyproject.toml`, `client/package.json` and `app/__init__.py` each carried
`0.1.0` independently, and the one `/api/health` reports -- `app/__init__.py` --
is the copy nobody thinks to edit. An operator curling `/api/health` after an
upgrade to confirm what is running is relying on the field least likely to be
right.

`app/__init__.py` is the **source of truth**, because it is the one the running
process can read without a build step. `tests/test_version.py` fails when the
three disagree, which is this project's way of enforcing a rule: a test, not a
convention.

## The three classes

The review item asks for major / minor / technical. That is SemVer with the
third field renamed, and the value is not the scheme -- it is that the class is
declared per release and checkable.

    major      a code overhaul, a UI overhaul, a backend restructure, or any
               change a downgrade cannot undo without losing rows
    minor      a new feature, or a functional change to one that exists
    technical  a dependency bump, a security fix, patching, a bug fix

Run it:

    python -m scripts.version                 # what is set now
    python -m scripts.version minor           # 0.1.0 -> 0.2.0, everywhere
    python -m scripts.version technical       # 0.1.0 -> 0.1.1
    python -m scripts.version --set 1.0.0     # exactly this

It writes the three files and stops. It does not commit, does not tag and does
not touch the CHANGELOG: a release is a decision somebody makes, and a script
that makes three of them at once is a script whose mistakes are three deep.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

INIT = ROOT / "app" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE_JSON = ROOT / "client" / "package.json"
#: npm copies the version into its lockfile twice -- the root and the `""`
#: package -- and `make version` used to leave both behind, so a bump left a
#: fourth copy saying the old number. Read and written as JSON: npm writes it as
#: `json.dumps(..., indent=2)` plus a newline, so the round trip is byte-exact.
PACKAGE_LOCK = ROOT / "client" / "package-lock.json"

#: Which SemVer field each of the review's three classes moves.
CLASSES = {"major": 0, "minor": 1, "technical": 2}

_INIT_RE = re.compile(r'^__version__ = "(?P<version>[^"]+)"$', re.M)
_PYPROJECT_RE = re.compile(r'^version = "(?P<version>[^"]+)"$', re.M)
_PACKAGE_RE = re.compile(r'^(?P<lead>  "version": ")(?P<version>[^"]+)(?P<tail>",)$', re.M)

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def current() -> str:
    """What `app/__init__.py` says. The source of truth."""
    found = _INIT_RE.search(INIT.read_text())
    if found is None:  # pragma: no cover - would mean the file was restructured
        raise SystemExit(f"no __version__ found in {INIT}")
    return found["version"]


def declared() -> dict[str, str]:
    """What each copy says -- three files and the lockfile's two -- for the test that they agree."""
    package = _PACKAGE_RE.search(PACKAGE_JSON.read_text())
    project = _PYPROJECT_RE.search(PYPROJECT.read_text())
    lock = json.loads(PACKAGE_LOCK.read_text())
    return {
        "app/__init__.py": current(),
        "pyproject.toml": project["version"] if project else "",
        "client/package.json": package["version"] if package else "",
        "client/package-lock.json": lock.get("version", ""),
        'client/package-lock.json packages[""]': lock.get("packages", {}).get("", {}).get("version", ""),
    }


def bumped(version: str, kind: str) -> str:
    if not _SEMVER.match(version):
        raise SystemExit(f"{version!r} is not MAJOR.MINOR.PATCH, so it cannot be bumped")
    parts = [int(piece) for piece in version.split(".")]
    index = CLASSES[kind]
    parts[index] += 1
    # A minor release resets the patch, a major resets both. Without this,
    # 0.1.7 -> 0.2.7 reads as though seven technical releases happened on a
    # branch that has only just been created.
    for later in range(index + 1, 3):
        parts[later] = 0
    return ".".join(str(piece) for piece in parts)


def write(version: str) -> None:
    if not _SEMVER.match(version):
        raise SystemExit(f"{version!r} is not MAJOR.MINOR.PATCH")

    INIT.write_text(_INIT_RE.sub(f'__version__ = "{version}"', INIT.read_text(), count=1))
    PYPROJECT.write_text(
        _PYPROJECT_RE.sub(f'version = "{version}"', PYPROJECT.read_text(), count=1)
    )
    PACKAGE_JSON.write_text(
        _PACKAGE_RE.sub(rf'\g<lead>{version}\g<tail>', PACKAGE_JSON.read_text(), count=1)
    )
    lock = json.loads(PACKAGE_LOCK.read_text())
    lock["version"] = version
    lock["packages"][""]["version"] = version
    PACKAGE_LOCK.write_text(json.dumps(lock, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "kind", nargs="?", choices=sorted(CLASSES), help="which field to move"
    )
    parser.add_argument("--set", dest="exact", help="set this exact version instead")
    args = parser.parse_args(argv)

    if args.exact and args.kind:
        parser.error("give a class or --set, not both")

    if not args.exact and not args.kind:
        for where, version in declared().items():
            print(f"{version:<12} {where}")
        return 0

    was = current()
    now = args.exact or bumped(was, args.kind)
    write(now)
    print(f"{was} -> {now}")
    print("Now: add a CHANGELOG.md entry with its `Reversible:` field, then commit and tag.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
