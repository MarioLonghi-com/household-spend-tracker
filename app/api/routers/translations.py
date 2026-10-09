"""Suggested wordings for the draft languages: review mode's routes (#272).

Owner only, and a household's own. A member of the household, and an owner
who is not in it, get the 404 a household that does not exist gets: these
routes are not something they should learn about by being refused.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ...audit.batch import batch
from ...models import BatchKind, Household, Role, SuggestionStatus
from ...schemas import (
    DraftLocale,
    TranslationSuggestionCreate,
    TranslationSuggestionOut,
    TranslationSuggestionsMark,
)
from ...services import households as household_service
from ...services import translation_suggestions as suggestion_service
from ..deps import CurrentHousehold, CurrentUser, SessionDep

router = APIRouter(tags=["translations"])


def reviewing_household(household: CurrentHousehold, user: CurrentUser, session: SessionDep) -> Household:
    """The household, for an owner who is in it; 404 for everybody else.

    `current_household` has already answered a non-member. A member who is
    not an owner is answered by the same check, asked again with
    `owner_only`, so the 404 is word for word the one a stranger gets.
    """
    if user.role is Role.owner:
        return household
    return household_service.get_for(session, household.id, user, owner_only=True)


ReviewHousehold = Annotated[Household, Depends(reviewing_household)]

BASE = "/households/{household_id}/translation-suggestions"


@router.get(BASE, response_model=list[TranslationSuggestionOut])
def list_suggestions(
    household: ReviewHousehold,
    session: SessionDep,
    status: SuggestionStatus | None = None,
) -> list[TranslationSuggestionOut]:
    rows = suggestion_service.listed(session, household.id, status=status)
    return [TranslationSuggestionOut.model_validate(row) for row in rows]


@router.post(BASE, response_model=TranslationSuggestionOut, status_code=201)
def suggest(
    body: TranslationSuggestionCreate,
    household: ReviewHousehold,
    session: SessionDep,
    user: CurrentUser,
) -> TranslationSuggestionOut:
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        row = suggestion_service.suggest(
            session,
            household.id,
            locale=body.locale,
            message=body.message,
            context=body.context,
            suggested=body.suggested,
            note=body.note,
        )
    return TranslationSuggestionOut.model_validate(row)


@router.post(f"{BASE}/status", response_model=list[TranslationSuggestionOut])
def mark_suggestions(
    body: TranslationSuggestionsMark,
    household: ReviewHousehold,
    session: SessionDep,
    user: CurrentUser,
) -> list[TranslationSuggestionOut]:
    """Mark suggestions applied once the catalogs carry them, or open again."""
    rows = [suggestion_service.get(session, household.id, one) for one in dict.fromkeys(body.ids)]
    with batch(session, kind=BatchKind.bulk_update, actor_id=user.id, household_id=household.id):
        suggestion_service.mark(rows, body.status)
    return [TranslationSuggestionOut.model_validate(row) for row in rows]


@router.delete(f"{BASE}/{{suggestion_id}}", status_code=204)
def delete_suggestion(
    suggestion_id: str, household: ReviewHousehold, session: SessionDep, user: CurrentUser
) -> None:
    row = suggestion_service.get(session, household.id, suggestion_id)
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        suggestion_service.remove(session, row)


@router.get(f"{BASE}/export.json")
def export_json(
    household: ReviewHousehold,
    session: SessionDep,
    status: SuggestionStatus = SuggestionStatus.open,
) -> Response:
    """The open suggestions in every language, as a download the apply script reads."""
    rows = suggestion_service.listed(session, household.id, status=status)
    return Response(
        content=suggestion_service.as_json(session, rows),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="suggestions.json"'},
    )


@router.get(f"{BASE}/export.po")
def export_po(
    household: ReviewHousehold,
    session: SessionDep,
    locale: DraftLocale,
    status: SuggestionStatus = SuggestionStatus.open,
) -> Response:
    """One language's open suggestions as a `.po` patch, which the apply script
    reads too -- and which a translation tool opens."""
    rows = suggestion_service.listed(session, household.id, status=status)
    return Response(
        content=suggestion_service.as_po(session, rows, locale),
        media_type="text/x-gettext-translation; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="suggestions-{locale}.po"'},
    )
