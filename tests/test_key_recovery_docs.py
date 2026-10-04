"""What the documents say a lost or replaced `secret.key` costs (#287).

The key going signs nobody out and loses no data. Recovery mode then sends
every member the key cannot open to a recovery code -- at a trusted browser
too -- and a recovery code revokes that member's sessions, trusted browsers
and agent keys. Putting the original key back ends recovery mode and returns
none of those. SECURITY.md once said a lost key cost "nothing else", and the
README that trusted devices were "unaffected": both true before a trusted
browser stopped skipping the code, and wrong after.
`test_key_recovery_mode` proves the behaviour; this holds the pages an
operator reads to it, because only a person notices a sentence that has
stopped being true, and only after it has misled them.
"""

from __future__ import annotations

import pathlib
import re

import pytest

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _between(name: str, start: str, end: str) -> str:
    text = (ROOT / name).read_text(encoding="utf-8")
    assert start in text, f"{name} has no {start!r}: the test needs to follow it"
    tail = text.split(start, 1)[1]
    return re.sub(r"\s+", " ", tail.split(end, 1)[0] if end in tail else tail)


#: Each place that says what losing the key costs, from its heading or first
#: words to where it stops.
PASSAGES = {
    "SECURITY.md": ("Losing `secret.key`", "## "),
    "README.md": ("**Back up `secret.key` with the database.**", "\n\n"),
    "deploy/TROUBLESHOOTING.md": ("### 4. `secret.key` is lost or wrong", "\n### 5."),
}


#: Every page an operator reads about the key.
DOCS = (
    "CHANGELOG.md",
    "README.md",
    "SECURITY.md",
    "deploy/TROUBLESHOOTING.md",
    "deploy/UPGRADING.md",
    "deploy/DOCKER.md",
)

#: "putting the original key back ends it", "if the original key turns up,
#: putting it back and restarting ends recovery mode": a sentence that says
#: the original key, back, ends something.
_ENDS_IT = re.compile(r"original key\b.{0,40}?\bback\b.{0,60}?\bends?\b", re.I)


def _sentences(name: str) -> list[str]:
    flat = re.sub(r"\s+", " ", (ROOT / name).read_text(encoding="utf-8"))
    return re.split(r"(?<=[.;:!?])\s+", flat)


@pytest.mark.parametrize("name", DOCS)
def test_the_original_key_back_ends_recovery_mode_only_for_those_who_have_not_re_enrolled(name):
    """A member who re-enrolled under the replacement key holds a secret only
    the replacement opens; the original key back puts *them* in recovery mode
    (`test_one_recovery_code_re_enrols_one_member_and_the_original_key_ends_it_for_the_other`).
    So every sentence promising the original key ends it says for whom. The
    CHANGELOG once did not, and an operator who trusted it spent a member a
    second recovery code."""
    unlimited = [
        sentence
        for sentence in _sentences(name)
        if _ENDS_IT.search(sentence) and "re-enrol" not in sentence
    ]
    assert not unlimited, f"{name}: {unlimited}"


@pytest.mark.parametrize("name", sorted(PASSAGES))
def test_what_a_lost_key_costs_names_what_the_recovery_code_takes(name):
    passage = _between(name, *PASSAGES[name])
    assert re.search(r"agent keys?", passage), f"{name} does not say agent keys are revoked"
    assert re.search(r"trusted (browser|device)s?", passage, re.I), name
    assert re.search(r"brings? (back )?(nothing|none)|not bring|stays revoked", passage), (
        f"{name} does not say the original key put back returns none of it"
    )
    assert "nothing else" not in passage, f"{name} promises the loss costs nothing else"
    assert not re.search(r"(sessions|trusted devices)[^.]*unaffected", passage, re.I), (
        f"{name} says sessions or trusted devices are unaffected"
    )
