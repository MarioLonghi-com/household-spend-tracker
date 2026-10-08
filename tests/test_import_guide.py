"""The "How import works" page keeps up with the importer (issue #73).

A page that explains behaviour goes stale the first time the behaviour
changes. This reads the page's source and fails when something the importer
does has no explanation on it: an outcome, a state word, a format, an
identifier kind.
"""

from __future__ import annotations

import pathlib

import pytest

from app.models import IdentifierKind, ImportOutcome
from statements import sniffing

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

PAGE = pathlib.Path(__file__).resolve().parent.parent / "client/src/screens/ImportGuide.tsx"
#: Where the Import screen's outcome words live since #52.
LABELS = PAGE.parent.parent / "lib/labels.ts"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.mark.parametrize("outcome", list(ImportOutcome), ids=str)
def test_every_outcome_is_explained(page, outcome):
    assert f'code: "{outcome.value}"' in page, f"{outcome.value} is not explained"


def test_the_page_uses_the_words_the_preview_shows(page):
    """The label on the guide is the label on the Import screen, exactly."""
    import re

    labels = LABELS.read_text(encoding="utf-8")
    shown = dict(
        re.findall(
            r"^\s+get (\w+)\(\) \{ return t`([^`]+)`; \},$",
            labels.split("IMPORT_OUTCOME_WORDS")[1].split("};")[0],
            re.M,
        )
    )
    explained = dict(re.findall(r'code: "(\w+)",\s+label: "([^"]+)"', page))
    assert shown == explained


@pytest.mark.parametrize(
    "word", sorted(sniffing._DID_NOT_HAPPEN | sniffing._NOT_YET), ids=str
)
def test_every_state_word_is_named(page, word):
    assert word.upper() in page


@pytest.mark.parametrize("kind", list(IdentifierKind), ids=str)
def test_every_identifier_kind_is_explained(page, kind):
    assert f"<code>{kind.value}</code>" in page


@pytest.mark.parametrize(
    "fmt", ["CSV", "OFX", ".xls", "PDF", ".xlsx", "camt.053", "MT940"], ids=str
)
def test_every_format_read_or_refused_is_named(page, fmt):
    assert fmt in page


def test_it_is_information_only(page):
    """No toggles, no actions (the issue's own words)."""
    for control in ("<button", "<input", "<select", "useMutation", "api."):
        assert control not in page, f"{control} on an information-only page"


def test_it_is_every_members_page():
    app = (PAGE.parent.parent / "App.tsx").read_text(encoding="utf-8")
    line = next(line for line in app.splitlines() if '"import-guide", label' in line)
    assert "ownerOnly" not in line


# --------------------------------------------------------------------------- #
# The One-time Import section (#183)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def one_time(page) -> str:
    return page.split("One-time Import: bringing")[1]


def test_every_one_time_workflow_is_named(one_time):
    from app.services.one_time_import import WORKFLOWS, engine

    for key in WORKFLOWS:
        assert engine.WORKFLOW_NAMES[key] in one_time, f"the {key} workflow is not explained"


def test_the_duplicate_window_is_the_engines(one_time):
    from app.services.one_time_import import engine

    assert f"dated up to {engine.DUPLICATE_WINDOW_DAYS} days apart" in one_time


def test_the_history_headline_is_the_one_history_shows(one_time):
    from app.services.describing import ONE_TIME_HEADLINES

    csv, api = ONE_TIME_HEADLINES["one-time-import:ynab-csv"], ONE_TIME_HEADLINES["one-time-import:ynab-api"]
    assert csv in one_time
    assert api.removeprefix(csv.removesuffix("(CSV)")) in one_time


def test_the_files_it_reads_and_writes_are_named(one_time):
    from app.services.one_time_import import ynab_source

    assert "<code>Register.csv</code>" in one_time and "<code>Plan.csv</code>" in one_time
    assert "Register.csv" in ynab_source.PLAN_NOT_REGISTER
    results = (PAGE.parent / "ynab" / "results.tsx").read_text(encoding="utf-8")
    assert ".txt`" in results, "the report is no longer a .txt download"
    assert "<code>.txt</code>" in one_time


def test_it_says_the_token_is_never_stored_and_points_at_github(one_time):
    assert "never stored" in one_time
    assert "href={NEW_ISSUE_URL}" in one_time
