"""The help articles, for an agent (#460 §9).

The same articles the app's Help panel shows, so someone who wants an
assistant to answer "how do I…?" in conversation points their own model at
these and the app still knows about no vendor. Read only, scope `read`; the
desktop profile has no agents, so no key reaches it there.

**Which language.** A household has no language of its own -- the language is
each device's choice (#48) -- so the caller says which it wants: ``lang``, or
else its ``Accept-Language`` header, negotiated the way the client
negotiates (exact, then the language alone). English otherwise. The answer
says which language it is in and which others there are.

Articles about a feature this deployment does not offer (`requires:`) are
left out, as the panel leaves them out.

**Endpoints join `agent.ENDPOINTS` at import**, like the other agent modules.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from ... import capabilities, config
from ...errors import NotFound
from ...schemas import ManifestEndpoint
from ...services import help as help_service
from ..deps import CurrentAgent
from . import agent as agent_api

router = APIRouter(prefix=f"/agent/v{agent_api.API_VERSION}", tags=["agent"])

_BASE = f"/api/agent/v{agent_api.API_VERSION}"


class HelpArticleOut(BaseModel):
    id: str
    title: str
    #: The app's screens the article is about, by the name the app uses.
    screens: list[str]
    #: Other ways a person asks the same question. Useful for matching one.
    also: list[str]
    #: The body as written: Markdown, where **bold** quotes a label the app shows.
    markdown: str


class HelpArticlesOut(BaseModel):
    locale: str
    available_locales: list[str]
    articles: list[HelpArticleOut]


class HelpOneOut(HelpArticleOut):
    locale: str


def _language(request: Request, lang: str | None) -> tuple[str, list[str]]:
    available = help_service.locales()
    asked = [lang] if lang else help_service.accepted(request.headers.get("accept-language"))
    return help_service.negotiate(asked, available), available


def _offered(locale: str) -> list[help_service.Article]:
    offered = capabilities.for_settings(config.settings).as_dict()
    return [
        one
        for one in help_service.articles(locale)
        if not one.requires or offered.get(one.requires, False)
    ]


def _out(one: help_service.Article) -> dict:
    return {
        "id": one.id,
        "title": one.title,
        "screens": list(one.screens),
        "also": list(one.also),
        "markdown": one.markdown,
    }


_LANG = Query(
    max_length=35,
    pattern=r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$",
    description="A language tag, e.g. pt-BR. Without it, Accept-Language decides; then English.",
)


@router.get("/help", response_model=HelpArticlesOut)
def help_articles(
    agent: CurrentAgent, request: Request, lang: Annotated[str | None, _LANG] = None
) -> HelpArticlesOut:
    """Every help article, in one language."""
    locale, available = _language(request, lang)
    found = _offered(locale)
    request.state.agent_rows = len(found)
    return HelpArticlesOut(
        locale=locale,
        available_locales=available,
        articles=[HelpArticleOut(**_out(one)) for one in found],
    )


@router.get("/help/{article_id}", response_model=HelpOneOut)
def help_article(
    article_id: str, agent: CurrentAgent, request: Request, lang: Annotated[str | None, _LANG] = None
) -> HelpOneOut:
    """One help article, in one language."""
    locale, _available = _language(request, lang)
    for one in _offered(locale):
        if one.id == article_id:
            request.state.agent_rows = 1
            return HelpOneOut(locale=locale, **_out(one))
    raise NotFound(
        f"no help article has the id {article_id}. GET {_BASE}/help lists them.",
        code="help.article_not_found",
        params={"id": article_id},
    )


ENDPOINTS = [
    ManifestEndpoint(
        method="GET",
        path=f"{_BASE}/help",
        says=(
            "The app's own help articles -- how to do something in it, step by step -- "
            "the ones its Help panel shows. Answer a person's 'how do I…?' from these "
            "rather than guessing at screens. lang=pt-BR (or Accept-Language) picks the "
            "language; English otherwise, and locale says which you got. Each article "
            "has its screens, other phrasings of the question (also) and its body as "
            "Markdown, where **bold** is a label exactly as the app shows it."
        ),
        returns="{locale, available_locales[], articles[]}",
        scope="read",
    ),
    ManifestEndpoint(
        method="GET",
        path=f"{_BASE}/help/{{article_id}}",
        says="One help article by its id, in the language lang asks for.",
        returns="{locale, id, title, screens[], also[], markdown}",
        scope="read",
    ),
]


agent_api.ENDPOINTS.extend(ENDPOINTS)
