"""A shell block in the docs is something people paste, so it has to paste.

zsh, the macOS default, does not treat `#` as a comment at an interactive
prompt unless `interactivecomments` is set, and it is not set by default. So

    make install    # Python venv + client/node_modules

pasted from the README runs `make install '#' Python venv + client/...`, and
make answers `No rule to make target '#'` after doing the real work -- an error
at the end of a successful install, which is what #64 was filed alongside.

The explanation goes under the block instead, where it reads the same and is
never executed.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

ROOT = pathlib.Path(__file__).resolve().parent.parent
FENCE = re.compile(r"^```(bash|sh|shell|zsh)\s*$")
TRAILING = re.compile(r"^\S.*\s#\s")

DOCS = sorted(
    p
    for p in ROOT.rglob("*.md")
    if not {"node_modules", ".venv", "dist"} & set(p.relative_to(ROOT).parts)
    and not p.relative_to(ROOT).parts[0].startswith(".")
)


def _shell_lines(path: pathlib.Path):
    inside = False
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith("```"):
            inside = not inside and bool(FENCE.match(line))
            continue
        if inside:
            yield number, line


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_shell_block_carries_a_comment_after_a_command(path: pathlib.Path):
    offending = [
        f"{path.relative_to(ROOT)}:{number}: {line}"
        for number, line in _shell_lines(path)
        if TRAILING.match(line)
    ]
    assert not offending, (
        "a trailing `# comment` becomes arguments when pasted into zsh. Move it "
        "under the block:\n" + "\n".join(offending)
    )


def test_the_check_finds_one(tmp_path):
    """Planted back, so the rule above is known to fire."""
    planted = tmp_path / "planted.md"
    planted.write_text("```bash\nmake seed       # a demo household\n```\n")
    assert [n for n, line in _shell_lines(planted) if TRAILING.match(line)] == [2]
