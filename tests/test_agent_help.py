"""The help articles through the agent API (#460 §9).

What is asserted is the text an agent receives: the article as written in the
language asked for, the same files the client's Help panel is built from.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app import capabilities
from app.api.routers import agent as agent_router
from app.services import help as help_service
from tests.test_agent_manifest import keyed  # noqa: F401 - a fixture

HELP = f"/api/agent/v{agent_router.API_VERSION}/help"
SOURCE = pathlib.Path(__file__).resolve().parent.parent / "client/src/help"


def _get(client, token: str, path: str = HELP, **headers):
    return client.get(path, headers={"authorization": f"Bearer {token}", **headers})


def _english(article_id: str) -> help_service.Article:
    return help_service.parse((SOURCE / "en" / f"{article_id}.md").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def from_source(monkeypatch, tmp_path):
    """Read the Markdown, whatever a local build left in app/static/dist."""
    monkeypatch.setenv("SPENDTRACKER_CLIENT_DIST", str(tmp_path / "no-build"))


def test_every_english_article_is_listed_with_its_body_as_written(client, keyed):  # noqa: F811
    body = _get(client, keyed["token"]).json()

    assert body["locale"] == "en"
    assert set(body["available_locales"]) == {"en", "pt-BR", "es-ES", "sv-SE"}
    on_disk = {path.stem for path in (SOURCE / "en").glob("*.md")}
    assert {one["id"] for one in body["articles"]} == on_disk
    split = next(one for one in body["articles"] if one["id"] == "split-transaction")
    written = _english("split-transaction")
    assert split["title"] == written.title
    assert split["markdown"] == written.markdown
    assert "---" not in split["markdown"].splitlines()[0], "the front matter is not body"
    assert split["screens"] == ["register"]


def test_lang_picks_the_language_and_says_so(client, keyed):  # noqa: F811
    body = _get(client, keyed["token"], f"{HELP}?lang=pt-BR").json()
    assert body["locale"] == "pt-BR"
    portuguese = help_service.parse((SOURCE / "pt-BR/split-transaction.md").read_text(encoding="utf-8"))
    split = next(one for one in body["articles"] if one["id"] == "split-transaction")
    assert split["title"] == portuguese.title != _english("split-transaction").title


def test_accept_language_decides_when_lang_is_not_given(client, keyed):  # noqa: F811
    body = _get(client, keyed["token"], HELP, **{"accept-language": "fr;q=0.9, es;q=0.8"}).json()
    # No French; Spanish by its language alone, as the client negotiates.
    assert body["locale"] == "es-ES"


def test_a_language_with_no_articles_gets_english(client, keyed):  # noqa: F811
    assert _get(client, keyed["token"], f"{HELP}?lang=fr-FR").json()["locale"] == "en"


def test_one_article_by_id(client, keyed):  # noqa: F811
    answer = _get(client, keyed["token"], f"{HELP}/reimbursements?lang=sv-SE")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    swedish = help_service.parse((SOURCE / "sv-SE/reimbursements.md").read_text(encoding="utf-8"))
    assert (body["locale"], body["id"], body["title"], body["markdown"]) == (
        "sv-SE", "reimbursements", swedish.title, swedish.markdown,
    )


def test_an_unknown_id_is_a_404_that_says_where_the_list_is(client, keyed):  # noqa: F811
    answer = _get(client, keyed["token"], f"{HELP}/no-such-thing")
    assert answer.status_code == 404
    # An agent is answered without the code, as every agent refusal is (main.py).
    assert answer.json() == {"detail": f"no help article has the id no-such-thing. GET {HELP} lists them."}


def test_an_article_about_a_feature_not_offered_is_left_out(client, keyed, monkeypatch):  # noqa: F811
    assert "snap-receipt" in {one["id"] for one in _get(client, keyed["token"]).json()["articles"]}
    real = capabilities.for_settings

    def without_snap(settings):
        offered = real(settings)
        return capabilities.Capabilities(**{**offered.as_dict(), "snap": False})

    monkeypatch.setattr(capabilities, "for_settings", without_snap)
    assert "snap-receipt" not in {one["id"] for one in _get(client, keyed["token"]).json()["articles"]}
    assert _get(client, keyed["token"], f"{HELP}/snap-receipt").status_code == 404


def test_the_manifest_lists_both(client, keyed):  # noqa: F811
    endpoints = _get(client, keyed["token"], f"/api/agent/v{agent_router.API_VERSION}/manifest").json()["endpoints"]
    paths = {(one["method"], one["path"], one["scope"]) for one in endpoints}
    assert ("GET", HELP, "read") in paths
    assert ("GET", f"{HELP}/{{article_id}}", "read") in paths


def test_no_key_no_articles(client, keyed):  # noqa: F811
    assert client.get(HELP).status_code == 401


# --------------------------------------------------------------------------- #
# Where the articles are read from
# --------------------------------------------------------------------------- #


def test_the_build_copy_is_read_when_there_is_one(tmp_path):
    built = tmp_path / "help"
    built.mkdir()
    (built / "en.json").write_text(
        json.dumps({
            "locale": "en",
            "articles": [
                {"id": "one", "title": "One", "screens": ["register"], "also": [], "markdown": "Built text."},
                {"id": "two", "title": "Two", "screens": ["accounts"], "also": ["x"], "markdown": "More."},
            ],
        }),
        encoding="utf-8",
    )
    found = help_service.articles("en", built=built, source=SOURCE)
    assert [(one.id, one.markdown) for one in found] == [("one", "Built text."), ("two", "More.")]
    assert help_service.locales(built=built, source=SOURCE) == ["en"]


def test_without_a_build_the_markdown_is_read(tmp_path):
    found = help_service.articles("en", built=tmp_path / "nothing", source=SOURCE)
    assert {one.id for one in found} == {path.stem for path in (SOURCE / "en").glob("*.md")}


def test_front_matter_is_split_as_the_client_splits_it():
    article = help_service.parse(
        "---\nid: two-things\ntitle: Two things\nscreens: [register, accounts]\n"
        "also:\n  - first way\n  - second, with a comma\nrequires: snap\n---\n\nBody **here**.\n"
    )
    assert article.screens == ("register", "accounts")
    assert article.also == ("first way", "second, with a comma")
    assert (article.requires, article.markdown) == ("snap", "Body **here**.")


def test_a_file_without_front_matter_is_refused():
    with pytest.raises(help_service.HelpFormatError):
        help_service.parse("# Just a heading\n")


@pytest.mark.parametrize(
    ("header", "wanted"),
    [("pt-PT", "pt-BR"), ("sv", "sv-SE"), ("de, en;q=0.5", "en"), ("", "en"), ("es-ES;q=0, sv;q=0.4", "sv-SE")],
)
def test_negotiation(header, wanted):
    available = ["en", "es-ES", "pt-BR", "sv-SE"]
    assert help_service.negotiate(help_service.accepted(header), available) == wanted
