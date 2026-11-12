"""How import works keeps up with the importer (issues #73, #460).

A page that explains behaviour goes stale the first time the behaviour
changes. This reads the help articles the guide became in #460 -- How import
reads a statement, How imported transfers are matched, the identifiers and
the One-time Import -- and fails when something the importer does has no
explanation in them: an outcome, a state word, a format, an identifier kind.

The English articles are read; each other language is the same article,
written whole, and `client/src/help/articles.test.ts` holds every language to
having it.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.models import IdentifierKind, ImportOutcome
from app.services import help as help_service
from statements import sniffing

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

CLIENT = pathlib.Path(__file__).resolve().parent.parent / "client/src"
HELP = CLIENT / "help/en"
#: Where the Import screen's outcome words live since #52.
LABELS = CLIENT / "lib/labels.ts"


def _article(article_id: str) -> str:
    return help_service.parse((HELP / f"{article_id}.md").read_text(encoding="utf-8")).markdown


@pytest.fixture(scope="module")
def page() -> str:
    """Sections 1-4 and 7 of the old guide."""
    return _article("import-how-it-works")


@pytest.fixture(scope="module")
def identifiers() -> str:
    """Section 6."""
    return _article("account-identifiers")


#: Each outcome is one list item: ``- **<label>** (`<code>`): <meaning>``.
OUTCOME_LINE = re.compile(r"^- \*\*([^*]+)\*\* \(`(\w+)`\):", re.M)


@pytest.mark.parametrize("outcome", list(ImportOutcome), ids=str)
def test_every_outcome_is_explained(page, outcome):
    explained = {code for _label, code in OUTCOME_LINE.findall(page)}
    assert outcome.value in explained, f"{outcome.value} is not explained"


def test_the_page_uses_the_words_the_preview_shows(page):
    """The label on the guide is the label on the Import screen, exactly."""
    labels = LABELS.read_text(encoding="utf-8")
    # Each getter returns `t({ message: "…", … })`: the message carries a
    # translator note since #228, and "New" a context of its own.
    shown = dict(
        re.findall(
            r'get (\w+)\(\)\s*\{\s*return t\(\{\s*message: "([^"]+)"',
            labels.split("IMPORT_OUTCOME_WORDS")[1].split("\n};")[0],
        )
    )
    explained = {code: label for label, code in OUTCOME_LINE.findall(page)}
    assert shown == explained


@pytest.mark.parametrize(
    "word", sorted(sniffing._DID_NOT_HAPPEN | sniffing._NOT_YET), ids=str
)
def test_every_state_word_is_named(page, word):
    assert f"`{word.upper()}`" in page


@pytest.mark.parametrize("kind", list(IdentifierKind), ids=str)
def test_every_identifier_kind_is_explained(identifiers, kind):
    assert f"`{kind.value}`" in identifiers


@pytest.mark.parametrize(
    "fmt", ["CSV", "OFX", ".xls", "PDF", ".xlsx", "camt.053", "MT940"], ids=str
)
def test_every_format_read_or_refused_is_named(page, fmt):
    assert fmt in page


def test_it_is_every_members_article():
    """Nothing about importing is the owner's alone, or a capability's."""
    for article_id in ("import-statement", "import-how-it-works", "import-transfers", "account-identifiers"):
        source = help_service.parse((HELP / f"{article_id}.md").read_text(encoding="utf-8"))
        assert source.requires is None, article_id


def test_the_old_route_opens_the_article():
    """Links to How import works keep working (#460 §12): the route opens the article."""
    app = (CLIENT / "App.tsx").read_text(encoding="utf-8")
    assert 'const IMPORT_GUIDE_ARTICLE = "import-how-it-works";' in app
    assert 'if (to === "import-guide")' in app
    assert (HELP / "import-how-it-works.md").is_file()


# --------------------------------------------------------------------------- #
# The One-time Import section (#183)
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def one_time() -> str:
    return _article("one-time-import")


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

    assert "`Register.csv`" in one_time and "`Plan.csv`" in one_time
    assert "Register.csv" in ynab_source.PLAN_NOT_REGISTER
    results = (CLIENT / "screens/ynab/results.tsx").read_text(encoding="utf-8")
    assert ".txt`" in results, "the report is no longer a .txt download"
    assert "`.txt`" in one_time


def test_it_says_the_token_is_never_stored_and_points_at_github(one_time):
    assert "never stored" in one_time
    screen = (CLIENT / "screens/OneTimeImport.tsx").read_text(encoding="utf-8")
    url = re.search(r'export const NEW_ISSUE_URL = "([^"]+)"', screen).group(1)
    assert f"]({url})" in one_time
