"""The two dark blocks in `styles.css` are one thing written twice.

`client/src/lib/appearance.ts` explains why there are two: the scheme cannot be
resolved in JavaScript before the first paint, because `main.py` sends
`script-src 'self'` with no `'unsafe-inline'`. So the media query paints the
system's answer and `:root[data-theme="dark"]` overrides it, and both have to
carry the same values.

The failure this prevents is a quiet one. Change `--danger` in the media query
only and the app looks right for everybody following their system and wrong for
everybody who chose dark by hand -- one colour, on one screen, for one group of
people, with nothing to point at.

It lives in pytest rather than vitest for the reason `test_client_agrees.py`
does: reading a client file as text is what these guards do, and the obvious
in-browser route does not work. Vite's CSS plugin answers `?raw` for a
stylesheet with an empty string under vitest, so the test passed while reading
nothing at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

CSS_FILE = Path(__file__).resolve().parent.parent / "client" / "src" / "styles.css"

#: Comments stripped, because the header comment in `styles.css` quotes both
#: selectors while explaining the contract. Searching the raw text found the
#: *comment* and then read the light block after it -- both lookups returned the
#: same declarations and the comparison passed while proving nothing.
CSS = re.sub(r"/\*.*?\*/", "", CSS_FILE.read_text(), flags=re.S)

SYSTEM_DARK = ':root:not([data-theme="light"])'
FORCED_DARK = ':root[data-theme="dark"]'


def declarations(selector: str) -> dict[str, str]:
    """The custom properties inside the first real rule headed by `selector`."""
    match = re.search(re.escape(selector) + r"\s*\{", CSS)
    assert match, f"{selector} does not head a rule in styles.css"

    depth, start = 0, match.end() - 1
    for i in range(start, len(CSS)):
        if CSS[i] == "{":
            depth += 1
        elif CSS[i] == "}":
            depth -= 1
            if depth == 0:
                body = CSS[start + 1 : i]
                break
    else:  # pragma: no cover - only reachable from an unbalanced stylesheet
        pytest.fail(f"the rule headed by {selector} is never closed")

    found = {}
    for part in body.split(";"):
        line = part.strip()
        if not line.startswith("--"):
            continue
        name, _, value = line.partition(":")
        found[name.strip()] = value.strip()
    return found


@pytest.fixture(scope="module")
def blocks() -> tuple[dict[str, str], dict[str, str]]:
    return declarations(SYSTEM_DARK), declarations(FORCED_DARK)


def test_both_dark_blocks_actually_declare_something(blocks):
    """Or every comparison below passes by comparing nothing with nothing."""
    from_query, from_attribute = blocks
    assert len(from_query) > 8
    assert len(from_attribute) > 8


def test_the_two_dark_blocks_declare_the_same_tokens(blocks):
    from_query, from_attribute = blocks
    assert sorted(from_attribute) == sorted(from_query)


def test_the_two_dark_blocks_give_every_token_the_same_value(blocks):
    from_query, from_attribute = blocks
    assert from_attribute == from_query


def test_dark_is_not_just_the_light_values(blocks):
    """A dark block that matched light would mean the feature did nothing."""
    _, from_attribute = blocks
    light = declarations(":root")
    assert from_attribute["--paper"] != light["--paper"]
    assert from_attribute["--ink"] != light["--ink"]


def test_a_forced_light_page_escapes_the_system_dark_rule():
    """Without the `:not()`, choosing light on a dark laptop does nothing."""
    assert "@media (prefers-color-scheme: dark)" in CSS
    assert SYSTEM_DARK in CSS


def test_the_attribute_rule_comes_last_so_it_wins_the_tie():
    """Equal specificity, so source order is the entire mechanism."""
    assert CSS.index(FORCED_DARK) > CSS.index(SYSTEM_DARK)


def test_the_browsers_own_furniture_is_told_which_scheme_it_is_in():
    """Without `color-scheme`, a forced theme leaves white menus on a dark page."""
    assert f"{FORCED_DARK} {{ color-scheme: dark; }}" in CSS
    assert ':root[data-theme="light"] { color-scheme: light; }' in CSS
