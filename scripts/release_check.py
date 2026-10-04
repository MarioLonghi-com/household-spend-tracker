"""Is this ready to be a release? Run on every pull request into `main`.

    python -m scripts.release_check --since origin/main     # a PR into main
    python -m scripts.release_check --tagged v0.2.0         # the release job

`main` only moves at a release, so a pull request into it *is* one, and until
this check existed nothing said so. `make version` and `tests/test_version.py`
keep the three version files agreeing with each other; neither notices when
they agree on a number that was never moved. `0.1.0` was never tagged, and
187 commits later `main` and `dev` both still said it.

## What it refuses

- **The version is already released**, or is not newer than the newest tag.
  The number has to move, and forwards.
- **No CHANGELOG section for it**, one still marked *unreleased*, or one
  without a date. The heading is `## 0.2.0 — 2026-09-29`.
- **`## Unreleased` still has something in it.** Every change merged since the
  last release goes under the version, or it goes out unannounced.
- **A migration added since `--since` that the section does not name.** The
  `Reversible:` line is what an operator reads before upgrading, and the one
  migration it forgets is the one they roll back. The 0.2.0 notes, when first
  drafted, named three of the eight.
- **No `**Reversible: none|clean|lossy**` line**, or `none` with migrations.

It reports every problem at once, not the first, so one push fixes them all.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

_HEADING = re.compile(r"^## (?P<title>.+?)\s*$", re.M)
_VERSIONED = re.compile(r"^(?P<version>\d+\.\d+\.\d+)(?:\s+—\s+(?P<when>.+))?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REVERSIBLE = re.compile(r"\*\*Reversible: (?P<verdict>none|clean|lossy)\*\*")
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    body: str


def sections(changelog: str) -> list[Section]:
    """Every `## ` section, in order, with its text up to the next one."""
    found = list(_HEADING.finditer(changelog))
    return [
        Section(
            title=match["title"],
            body=changelog[match.end() : found[i + 1].start() if i + 1 < len(found) else None],
        )
        for i, match in enumerate(found)
    ]


def _numbers(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


def _meaningful(body: str) -> str:
    """The body without the `---` rules that separate sections."""
    return "\n".join(line for line in body.splitlines() if line.strip() not in ("", "---"))


def problems(
    changelog: str,
    *,
    version: str,
    tags: list[str],
    migrations: list[str],
    tagged: str | None = None,
) -> list[str]:
    found: list[str] = []

    released = sorted(_numbers(t[1:]) for t in tags if _TAG.match(t))
    if tagged is not None:
        if tagged != f"v{version}":
            found.append(f"the tag is {tagged} but app/__init__.py says {version}")
    elif f"v{version}" in tags:
        found.append(f"v{version} is already released. Move it: make version BUMP=...")
    elif released and _numbers(version) <= released[-1]:
        newest = ".".join(map(str, released[-1]))
        found.append(f"{version} is not newer than the newest tag, v{newest}")

    by_title = sections(changelog)
    for one in by_title:
        if one.title.lower() == "unreleased" and _meaningful(one.body):
            found.append(
                "## Unreleased still has entries. Move them under "
                f"## {version} — <date>, and leave Unreleased empty"
            )

    ours = [
        s for s in by_title if (m := _VERSIONED.match(s.title)) and m["version"] == version
    ]
    if not ours:
        found.append(f"CHANGELOG.md has no section for {version}: ## {version} — YYYY-MM-DD")
        return found
    section = ours[0]
    when = _VERSIONED.match(section.title)["when"]
    if not when or not _DATE.match(when):
        found.append(
            f"## {section.title} needs a release date: ## {version} — YYYY-MM-DD"
            + (" (it still says unreleased)" if when and "unreleased" in when.lower() else "")
        )

    reversible = _REVERSIBLE.search(section.body)
    if reversible is None:
        found.append(f"## {version} has no **Reversible: none|clean|lossy** line")
    elif reversible["verdict"] == "none" and migrations:
        found.append(f"## {version} says Reversible: none, and {len(migrations)} migration(s) were added")

    unnamed = [revision for revision in migrations if revision not in section.body]
    if unnamed:
        found.append(
            f"## {version} does not name {len(unnamed)} migration(s) added in this release, "
            "each with what rolling it back loses: " + ", ".join(unnamed)
        )
    return found


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    ).stdout


def added_migrations(since: str) -> list[str]:
    """Revision ids of the migration files added between `since` and HEAD."""
    from scripts.upgrade import Migration

    names = _git("diff", "--name-only", "--diff-filter=A", f"{since}...HEAD", "--", "migrations/versions")
    return sorted(
        Migration(ROOT / name).revision for name in names.split() if name.endswith(".py")
    )


def main(argv: list[str] | None = None) -> int:
    from app import __version__

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--since", metavar="REF", help="the last release: origin/main on a PR into it")
    where.add_argument("--tagged", metavar="TAG", help="the tag being released")
    args = parser.parse_args(argv)

    found = problems(
        CHANGELOG.read_text(),
        version=__version__,
        tags=_git("tag", "--list", "v*").split(),
        migrations=added_migrations(args.since) if args.since else [],
        tagged=args.tagged,
    )
    if found:
        print(f"{__version__} is not ready to release:")
        for one in found:
            print(f"  - {one}")
        return 1
    print(f"{__version__} is ready: a dated CHANGELOG section, every new migration named")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
