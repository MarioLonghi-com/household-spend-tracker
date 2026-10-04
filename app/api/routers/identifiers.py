"""What banks call the household's accounts, and recognising a statement by it.

Issue #66. The identifiers are what let a file find its own account and a
descriptor like "TO A/C <number>" find the account it moved money to.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from ...audit.batch import batch
from ...models import Account, BatchKind
from ...schemas import (
    HolderSamplesOut,
    IdentifierCreate,
    IdentifierOut,
    IdentifierSuggestionIgnore,
    IdentifierSuggestionOut,
    IdentifierSuggestionsOut,
    IgnoredSuggestionOut,
    RecognisedOut,
)
from ...services import identifier_suggestions as suggestion_service
from ...services import identifiers as identifier_service
from ..deps import CurrentHousehold, CurrentUser, SessionDep, load_for
from ..offload import run_cpu
from ..uploads import read_capped, refuse_declared_size
from .imports import MAX_UPLOAD_BYTES, TOO_BIG

router = APIRouter(tags=["identifiers"])


@router.get("/households/{household_id}/identifiers", response_model=list[IdentifierOut])
def list_identifiers(household: CurrentHousehold, session: SessionDep) -> list[IdentifierOut]:
    return [
        IdentifierOut.model_validate(row)
        for row in identifier_service.list_for_household(session, household.id)
    ]


@router.get(
    "/households/{household_id}/identifiers/holder-samples", response_model=list[HolderSamplesOut]
)
def holder_samples(household: CurrentHousehold, session: SessionDep) -> list[HolderSamplesOut]:
    """For each holder, a few bank texts its name matches. Read-only (#132).

    Holder matching forgives word order, dropped middle names, initials and a
    cut-off surname; this is how a person sees what that catches.
    """
    return [
        HolderSamplesOut(identifier_id=one.identifier_id, rows=one.rows, samples=one.samples)
        for one in identifier_service.holder_samples(session, household.id)
    ]


@router.post(
    "/households/{household_id}/identifiers", response_model=IdentifierOut, status_code=201
)
def add_identifier(
    body: IdentifierCreate,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
) -> IdentifierOut:
    account = load_for(session, user, Account, body.account_id) if body.account_id else None
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        row = identifier_service.add(
            session,
            household_id=household.id,
            kind=body.kind,
            value=body.value,
            account=account,
        )
    return IdentifierOut.model_validate(row)


@router.get(
    "/households/{household_id}/identifiers/suggestions", response_model=IdentifierSuggestionsOut
)
def suggestions(
    household: CurrentHousehold,
    session: SessionDep,
    #: Also say how many transfer pairs each would link. Asked for rather than
    #: assumed: it runs the matcher once per suggestion, so the list comes
    #: back without it and the counts follow in a second request (#232).
    count_pairs: bool = False,
) -> IdentifierSuggestionsOut:
    """Identifiers the ledger names and nobody has added (#130). Reads only.

    Each says how many rows mention it and, with `count_pairs`, how many
    transfer pairs adding it would link, counted by the matcher with nothing
    written. Never added by itself: a person adds or ignores each one.
    """
    found = suggestion_service.suggest(session, household.id, count_pairs=count_pairs)
    accounts = {
        row.id: row.name
        for row in session.execute(
            select(Account).where(Account.household_id == household.id)
        ).scalars()
    }
    return IdentifierSuggestionsOut(
        items=[
            IdentifierSuggestionOut(
                kind=one.kind,
                value=one.value,
                account_id=one.account_id,
                account_name=accounts.get(one.account_id) if one.account_id else None,
                source=one.source,
                why=one.why,
                mentions=one.mentions,
                unit=one.unit,
                sample=one.sample,
                would_link=one.would_link,
            )
            for one in found.items
        ],
        would_link=found.would_link,
    )


@router.post(
    "/households/{household_id}/identifiers/suggestions/add",
    response_model=IdentifierOut,
    status_code=201,
)
def add_suggestion(
    body: IdentifierCreate, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> IdentifierOut:
    """Add a suggested identifier -- through the same service, and with the
    same checks, as one typed in. The account is the person's choice: the
    suggested one, or any other when the suggestion matched none."""
    return add_identifier(body, household, session, user)


@router.post(
    "/households/{household_id}/identifiers/suggestions/ignore",
    response_model=IgnoredSuggestionOut,
    status_code=201,
)
def ignore_suggestion(
    body: IdentifierSuggestionIgnore,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
) -> IgnoredSuggestionOut:
    """Never suggest this value, of this kind, again. Undone in History."""
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        row = suggestion_service.ignore(session, household.id, kind=body.kind, value=body.value)
    return IgnoredSuggestionOut.model_validate(row)


@router.delete("/households/{household_id}/identifiers/{identifier_id}", status_code=204)
def remove_identifier(
    identifier_id: str, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> None:
    row = identifier_service.get_for_household(session, identifier_id, household.id)
    with batch(session, kind=BatchKind.manual, actor_id=user.id, household_id=household.id):
        identifier_service.remove(session, row)


@router.post("/households/{household_id}/imports/recognise", response_model=RecognisedOut)
async def recognise(
    household: CurrentHousehold,
    session: SessionDep,
    file: Annotated[UploadFile | None, File()] = None,
    filename: Annotated[str | None, Form()] = None,
) -> RecognisedOut:
    """Which account a statement is for, before anything is staged.

    Read-only: the Import screen calls it the moment a file is chosen, to
    pre-select the account. The upload still names its account explicitly --
    this is a suggestion shown to a person, never a routing decision.
    """
    refuse_declared_size(file, MAX_UPLOAD_BYTES, TOO_BIG)
    raw = await read_capped(file, MAX_UPLOAD_BYTES, TOO_BIG) if file is not None else None
    name = filename or (file.filename if file is not None else None)
    # Reading the file is CPU and this route is `async`, so it runs on the
    # shared pool; matching needs the session and stays here. Issue #84.
    evidence = await run_cpu(identifier_service.read_evidence, raw) if raw else None
    # Matching needs the session: a threadpool worker, not the loop. #233.
    return await run_in_threadpool(_recognised, session, household.id, name, evidence)


def _recognised(session, household_id: str, name: str | None, evidence) -> RecognisedOut:
    found = identifier_service.recognise_file(
        session, household_id, filename=name, evidence=evidence
    )
    if found is None:
        # Nothing named the account, so the person will pick it: offer the
        # file name's tag for next time (#130). Offered, never added.
        tag = suggestion_service.for_file(session, household_id, name)
        if tag is None:
            return RecognisedOut()
        return RecognisedOut(tag_kind=tag[0], tag=tag[1])
    return RecognisedOut(account_id=found.account.id, account_name=found.account.name, how=found.how)
