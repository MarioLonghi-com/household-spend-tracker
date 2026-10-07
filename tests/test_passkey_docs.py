"""The operator docs and the boot check say the same thing about passkeys (#123).

If the doc and the code disagree, both are suspect (CLAUDE.md). These read
the README, DOCKER.md and UPGRADING.md and hold each sentence that states a
rule to the behaviour of `config._rp_id`, so changing one without the other
fails here rather than in somebody's terminal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import _rp_id

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_readme_states_the_default_and_the_code_has_it():
    assert "the host of `SPENDTRACKER_PUBLIC_URL`" in README
    assert _rp_id(None, "https://spend.example.ts.net", "production") == "spend.example.ts.net"


def test_the_readme_says_neither_set_means_none_and_the_code_agrees():
    assert "**Neither set:** there are no passkeys" in README
    assert _rp_id(None, "", "production") == ""


def test_the_readme_says_a_wider_name_is_refused_and_the_code_refuses_it():
    assert "the app refuses to start unless the value is that same" in README
    assert "`<tailnet>.ts.net`" in README
    with pytest.raises(ValueError, match="SPENDTRACKER_RP_ID must be the host"):
        _rp_id("example.ts.net", "https://spend.example.ts.net", "production")


def test_localhost_is_accepted_in_development_only_as_the_readme_says():
    assert "`localhost` in development" in README
    assert _rp_id("localhost", "", "development") == "localhost"
    with pytest.raises(ValueError):
        _rp_id("localhost", "", "production")
    # The other way the README offers: a localhost public URL, in any mode.
    assert "`SPENDTRACKER_PUBLIC_URL=http://localhost:8848`" in README
    assert _rp_id(None, "http://localhost:8848", "production") == "localhost"


@pytest.mark.parametrize("doc", ["README.md", "deploy/DOCKER.md", "deploy/UPGRADING.md"])
def test_each_operator_doc_says_to_choose_the_name_first(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert "SPENDTRACKER_RP_ID" in text
    assert "register" in text and "password and code" in text
