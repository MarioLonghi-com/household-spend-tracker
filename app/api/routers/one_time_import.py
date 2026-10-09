"""One-time Import from another budgeting app: YNAB first (#183).

Four doors, all stateless: the client resends the source every time -- the
export file, or the token and plan id -- and nothing about it is kept between
visits. Owner-only, and a cookie door only: no agent key reaches any of them.

A fifth, read-only door lists the imports already done, for the Import
screen. Every member's, like History, whose batches it reads; still no key.

The token is the person's YNAB credential. It is read from the form, handed to
``ynab_api`` and dropped; it is never in a response, a log line, an error, or
the batch's source. Its form field carries no constraint for the same reason:
FastAPI's own 422 echoes the value that failed one.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile
from pydantic import ValidationError as SchemaError
from starlette.concurrency import run_in_threadpool

from ...errors import ValidationError
from ...schemas import (
    OneTimeAnalysis,
    OneTimeImportHistory,
    OneTimeImportPlan,
    OneTimeImportReport,
    YnabPlansIn,
    YnabPlansOut,
)
from ...services.one_time_import import engine, ynab_source
from ...services.one_time_import.model import Source
from ..deps import CurrentHousehold, OwnerOnly, SessionDep
from ..offload import run_cpu
from ..uploads import read_capped, refuse_declared_size

router = APIRouter(tags=["one-time-import"])

PREFIX = "/households/{household_id}/one-time-import/ynab"

FILE_TOO_BIG = (
    f"that file is larger than a YNAB export is "
    f"({ynab_source.MAX_FILE_BYTES // (1024 * 1024)} MB)"
)

#: A personal access token is a few dozen characters.
MAX_TOKEN_LENGTH = 500


def _token(token: str | None) -> str:
    if not token or not token.strip():
        raise ValidationError("a YNAB personal access token is needed", code="ynab.token_needed")
    if len(token) > MAX_TOKEN_LENGTH:
        raise ValidationError("that is not a YNAB personal access token", code="ynab.token_malformed")
    return token.strip()


async def _source(
    via: str | None,
    file: UploadFile | None,
    token: str | None,
    plan_id: str | None,
) -> Source:
    if via == "csv":
        if file is None:
            raise ValidationError(
            "choose the YNAB export: the zip, or its Register.csv", code="ynab.choose_export"
        )
        refuse_declared_size(file, ynab_source.MAX_FILE_BYTES, FILE_TOO_BIG)
        raw = await read_capped(file, ynab_source.MAX_FILE_BYTES, FILE_TOO_BIG)
        # Parsing ten thousand rows is CPU, and this is an async route.
        return await run_cpu(ynab_source.from_file, raw, file.filename)
    if via == "api":
        if not plan_id or len(plan_id) > 64:
            raise ValidationError("choose which YNAB plan to import", code="ynab.choose_plan")
        # Network, off the event loop.
        return await run_in_threadpool(ynab_source.from_api, _token(token), plan_id)
    raise ValidationError("say how to reach YNAB: via is 'csv' or 'api'", code="ynab.say_how")


def _plan(text: str) -> engine.Plan:
    try:
        body = OneTimeImportPlan.model_validate(json.loads(text))
    except (ValueError, SchemaError) as exc:
        detail = exc.errors(include_input=False) if isinstance(exc, SchemaError) else str(exc)
        raise ValidationError(
            f"the import plan cannot be read: {detail}", code="ynab.plan_unreadable"
        ) from None
    return engine.Plan(
        currency=body.currency.upper(),
        date_format=body.date_format,
        accounts={
            key: choice.model_dump(exclude_none=True, mode="json")
            for key, choice in body.accounts.items()
        },
        categories={
            key: choice.model_dump(exclude_none=True, mode="json")
            for key, choice in body.categories.items()
        },
        flags=body.flags,
        starting_balance=body.starting_balance,
        date_from=body.date_from,
        date_to=body.date_to,
        acknowledge_cleared_reset=body.acknowledge_cleared_reset,
        duplicates_all=body.duplicates.all,
        duplicates_import=set(body.duplicates.import_),
    )


@router.post(f"{PREFIX}/plans", response_model=YnabPlansOut)
def list_plans(
    body: YnabPlansIn, household: CurrentHousehold, owner: OwnerOnly
) -> YnabPlansOut:
    """The plans this token can see, so the person can pick one."""
    return YnabPlansOut.model_validate({"plans": ynab_source.list_plans(_token(body.token))})


@router.post(f"{PREFIX}/analyse", response_model=OneTimeAnalysis)
async def analyse(
    household: CurrentHousehold,
    session: SessionDep,
    owner: OwnerOnly,
    via: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    token: Annotated[str | None, Form()] = None,
    plan_id: Annotated[str | None, Form()] = None,
    date_format: Annotated[str | None, Form(max_length=32)] = None,
    currency: Annotated[str | None, Form(max_length=3)] = None,
) -> OneTimeAnalysis:
    """What the source holds, what it could map to, and what was imported before."""
    source = await _source(via, file, token, plan_id)
    # The session is used from one thread at a time: the loop hands it over
    # and waits. Ten thousand rows of matching should not hold the loop.
    found = await run_in_threadpool(
        engine.analyse, session, household, source, currency=currency, date_format=date_format
    )
    return OneTimeAnalysis.model_validate(found)


async def _run(household, session, owner, via, file, token, plan_id, plan, *, commit: bool):
    parsed = _plan(plan)
    source = await _source(via, file, token, plan_id)
    report = await run_in_threadpool(
        engine.run, session, household, actor_id=owner.id, source=source, plan=parsed, commit=commit
    )
    return OneTimeImportReport.model_validate(report)


@router.post(f"{PREFIX}/preview", response_model=OneTimeImportReport)
async def preview(
    household: CurrentHousehold,
    session: SessionDep,
    owner: OwnerOnly,
    plan: Annotated[str, Form()],
    via: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    token: Annotated[str | None, Form()] = None,
    plan_id: Annotated[str | None, Form()] = None,
) -> OneTimeImportReport:
    """A dry run: everything the commit would do, then thrown away."""
    return await _run(household, session, owner, via, file, token, plan_id, plan, commit=False)


@router.post(f"{PREFIX}/commit", response_model=OneTimeImportReport, status_code=201)
async def commit(
    household: CurrentHousehold,
    session: SessionDep,
    owner: OwnerOnly,
    plan: Annotated[str, Form()],
    via: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    token: Annotated[str | None, Form()] = None,
    plan_id: Annotated[str | None, Form()] = None,
) -> OneTimeImportReport:
    """The import, in one batch that one undo in History reverses."""
    return await _run(household, session, owner, via, file, token, plan_id, plan, commit=True)


@router.get("/households/{household_id}/one-time-import/history", response_model=OneTimeImportHistory)
def history(household: CurrentHousehold, session: SessionDep) -> OneTimeImportHistory:
    """Every one-time import done here, newest first, undone ones marked so.

    The Import screen points at the One-time Import until one has been done.
    Same rows as the wizard's repeat-run warning, for every workflow.
    """
    found = engine.previous_imports(session, household, workflow=None)
    return OneTimeImportHistory.model_validate({
        "imports": [
            {**one, "workflow_name": engine.WORKFLOW_NAMES.get(one["workflow"], one["workflow"])}
            for one in found
        ]
    })
