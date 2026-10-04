"""Which commit this process is running, read once, when it starts.

`__version__` does not answer that. It is written by hand and moves at a
release, so `main` and a `dev` 187 commits ahead of it both said 0.1.0 -- and
"is the server on 8848 running what I just merged" ended in a terminal.

Two places it can come from, in this order:

1. **git**, when the code is a working copy. Only when the repository's top
   level *is* this installation: a tarball unpacked inside somebody's dotfiles
   repository is inside a git repository too, and that one's HEAD says nothing
   about this code.
2. **`app/build.json`**, the stamp `python -m scripts.build_stamp` writes where
   there is no `.git` to ask -- the release tarball and the container image,
   both of which are built from a checkout and shipped without it. Gitignored,
   so a stamp can never be committed and then describe the wrong commit.

Read once and kept, rather than asked on every request. The shared checkout
moves under a running server -- other sessions commit, switch branches, merge
-- and the server keeps executing the code it imported at boot. Asking git now
would report what is on disk, which is exactly the answer this exists to stop
people trusting.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path

#: The installation: the directory holding `app/`, `migrations/`, `Makefile`.
ROOT = Path(__file__).resolve().parent.parent
#: Where a build without `.git` carries its commit.
STAMP = Path(__file__).resolve().parent / "build.json"


@dataclass(frozen=True, slots=True)
class Build:
    #: The full SHA, or None when nothing could say.
    commit: str | None = None
    #: None when detached, which is how CI and a tagged release check out.
    branch: str | None = None
    #: ISO 8601 with offset, from the commit itself -- not when it was built.
    committed_at: str | None = None
    #: Tracked files differed from the commit when the process started, so the
    #: SHA names the nearest commit rather than the code. None when unknowable.
    dirty: bool | None = None
    #: "git", "stamp" or "unknown": how much to trust the rest.
    source: str = "unknown"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
        # `git status` refreshes the index and takes its lock to do it. This
        # checkout is shared with sessions that are committing; a read must
        # never be the reason one of their commits fails on index.lock.
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    ).stdout.strip()


def from_git() -> Build | None:
    """What git says about ROOT, or None when git cannot speak for it."""
    try:
        if Path(_git("rev-parse", "--show-toplevel")).resolve() != ROOT:
            return None
        commit = _git("rev-parse", "HEAD")
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        committed_at = _git("log", "-1", "--format=%cI")
        # Untracked files are left out on purpose: `data/`, a stray scratch
        # file and the stamp itself are not changes to the code that runs.
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError):
        return None
    return Build(
        commit=commit,
        branch=None if branch == "HEAD" else branch,
        committed_at=committed_at,
        dirty=dirty,
        source="git",
    )


def from_stamp() -> Build | None:
    try:
        said = json.loads(STAMP.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(said, dict) or not said.get("commit"):
        return None
    return Build(
        commit=str(said["commit"]),
        branch=said.get("branch") or None,
        committed_at=said.get("committed_at") or None,
        dirty=said.get("dirty"),
        source="stamp",
    )


@cache
def current() -> Build:
    """The build this process started with. The first call fixes it."""
    return from_git() or from_stamp() or Build()


def short(build: Build) -> str | None:
    return build.commit[:7] if build.commit else None
