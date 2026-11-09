"""Exchange rates for a person: settings, the dashboard, manual rates, Fetch now (#396).

Every route is household-scoped through ``current_household``, so a household
somebody is not in answers 404 exactly as one that does not exist. **Any
member** may change the settings, enter rates and fetch -- the same gate as the
household PATCH that sets the base currency. An agent key reaches none of
these; it reads rates and the status through `agent_review.py`.

Nothing here converts an amount. The rates are what a report converts with,
through `services/fx.py`.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, Response, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ...audit.batch import batch
from ...models import BatchKind, FxFetchMode, Household
from ...schemas import (
    FxFetchIn,
    FxFetchOut,
    FxManualIn,
    FxManualOut,
    FxManualRowOut,
    FxRateOut,
    FxRatesOut,
    FxSettingsIn,
    FxSettingsOut,
    FxStatusOut,
)
from ...services import fx, fx_browse, fx_dashboard, fx_fetching, fx_manual
from ..deps import CurrentHousehold, CurrentUser, SessionDep
from ..uploads import read_capped, refuse_declared_size

router = APIRouter(tags=["exchange rates"])

_HOUSE = "/households/{household_id}/fx"


def settings_out(household: Household) -> FxSettingsOut:
    return FxSettingsOut(
        base_currency=household.base_currency,
        chain=[s.value for s in fx.chain_of(household)],
        stale_days=household.fx_stale_days,
        monthly_policy=fx.monthly_policy_of(household).value,
        show_own_rates=household.fx_show_own_rates,
    )


@router.get(f"{_HOUSE}/settings", response_model=FxSettingsOut)
def get_settings(household: CurrentHousehold) -> FxSettingsOut:
    return settings_out(household)


@router.put(f"{_HOUSE}/settings", response_model=FxSettingsOut)
def put_settings(
    body: FxSettingsIn, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> FxSettingsOut:
    """Change the chain and how rates are used. Audited; past converted figures change.

    The confirmation that says so is the screen's. Here it is one batch, so
    History has who changed it and Undo puts it back.
    """
    with batch(session, kind=BatchKind.admin, actor_id=user.id, household_id=household.id):
        fx_dashboard.update_settings(
            household,
            chain=body.chain,
            stale_days=body.stale_days,
            monthly_policy=body.monthly_policy,
            show_own_rates=body.show_own_rates,
        )
    return settings_out(household)


def status_out(session: Session, household: Household) -> FxStatusOut:
    return FxStatusOut.model_validate(fx_dashboard.status(session, household))


@router.get(f"{_HOUSE}/status", response_model=FxStatusOut)
def get_status(household: CurrentHousehold, session: SessionDep) -> FxStatusOut:
    """The dashboard: tiles, per-currency rows, the running fetch, the history."""
    return status_out(session, household)


# --------------------------------------------------------------------------- #
# Stored rates
# --------------------------------------------------------------------------- #

MAX_RATES = 5000

#: The query parameters both `fx/rates` routes share, so they cannot disagree.
SortParam = Annotated[str, Query(pattern=f"^({'|'.join(fx_browse.SORTS)})$")]
DirectionParam = Annotated[str, Query(pattern="^(asc|desc)$")]
GranularityParam = Annotated[str, Query(pattern=f"^({'|'.join(fx_browse.GRANULARITIES)})$")]


def stored_rates(
    session: Session,
    household: Household,
    *,
    currency: str | None,
    since: Date | None,
    until: Date | None,
    source: str | None,
    limit: int,
    offset: int = 0,
    granularity: str = fx_browse.DAY,
    sort: str = "date",
    direction: str = "desc",
    at: Date | None = None,
) -> FxRatesOut:
    """A page of the rows the chain reads, newest first unless sorted otherwise.

    Stored rows with their base meaning, or a month per line (#410). Shared by
    the person's route and the agent's, so they cannot disagree.
    """
    found = fx_browse.page(
        session,
        household,
        currency=currency,
        since=since,
        until=until,
        source=source,
        granularity=granularity,
        sort=sort,
        direction=direction,
        limit=limit,
        offset=offset,
        at=at,
    )
    return FxRatesOut(
        base_currency=found.base,
        granularity=found.granularity,
        total=found.total,
        limit=limit,
        offset=found.offset,
        focus=found.focus,
        rates=[
            FxRateOut(
                id=row.id,
                source=row.source,
                quote_currency=row.quote_currency,
                currency=row.currency,
                date=row.date,
                period=row.period.value,
                rate=row.rate_text,
                rate_in_base=row.in_base_shown,
                own_rate=fx.rate_text(row.own) if row.own is not None else None,
                rate_date=row.rate_date,
                stale=row.stale,
            )
            for row in found.rows
        ],
    )


@router.get(f"{_HOUSE}/rates", response_model=FxRatesOut)
def get_rates(
    household: CurrentHousehold,
    session: SessionDep,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    since: Date | None = None,
    until: Date | None = None,
    source: Annotated[str | None, Query(pattern="^(ecb|manual)$")] = None,
    limit: int = Query(default=500, ge=1, le=MAX_RATES),
    offset: int = Query(default=0, ge=0),
    granularity: GranularityParam = fx_browse.DAY,
    sort: SortParam = "date",
    direction: DirectionParam = "desc",
    at: Date | None = None,
) -> FxRatesOut:
    """Stored rates as decimal strings, with their source and their base meaning.

    A page at a time (`limit`, `offset`), in the order of `sort` and
    `direction`; by day as stored, or a month per line by a rate policy
    (`granularity`). `at` answers the page holding the row nearest that date.
    """
    return stored_rates(
        session,
        household,
        currency=currency,
        since=since,
        until=until,
        source=source,
        limit=limit,
        offset=offset,
        granularity=granularity,
        sort=sort,
        direction=direction,
        at=at,
    )


# --------------------------------------------------------------------------- #
# Manual rates
# --------------------------------------------------------------------------- #


def manual_out(outcome: fx_manual.Outcome) -> FxManualOut:
    return FxManualOut(
        dry_run=outcome.dry_run,
        added=outcome.added,
        replaced=outcome.replaced,
        unchanged=outcome.unchanged,
        rows=[
            FxManualRowOut.model_validate(
                {**asdict(row), "period": row.period.value if row.period else None}
            )
            for row in outcome.rows
        ],
    )


@router.post(f"{_HOUSE}/rates/manual", response_model=FxManualOut)
def add_manual_rate(
    body: FxManualIn,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
) -> FxManualOut:
    """Add or replace one rate: units of the currency per one base, for a day or a month.

    `dry_run` true answers 200 with the verdict -- the inverse, and the rate it
    would replace -- and keeps nothing. Otherwise 201, or 422 and nothing kept.
    """
    outcome = fx_manual.save_rates(
        session,
        household=household,
        actor_id=user.id,
        entries=[(1, body.currency, body.date, body.rate)],
        dry_run=body.dry_run,
    )
    response.status_code = 200 if outcome.dry_run else 201
    return manual_out(outcome)


FILE_TOO_BIG = (
    f"that file is larger than a list of rates is ({fx_manual.MAX_RATE_CSV_BYTES // 1024} KB)"
)


@router.get(f"{_HOUSE}/rates/manual/template.csv", response_class=PlainTextResponse)
def manual_template(household: CurrentHousehold) -> PlainTextResponse:
    """The columns a rates file has -- currency, date, rate -- with nothing under them."""
    return PlainTextResponse(
        fx_manual.template(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fx_manual.TEMPLATE_FILENAME}"'},
    )


@router.post(f"{_HOUSE}/rates/manual/import", response_model=FxManualOut)
async def import_manual_rates(
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
    response: Response,
    file: Annotated[UploadFile, File()],
    dry_run: Annotated[bool, Form()] = True,
) -> FxManualOut:
    """Many rates from one CSV file (`currency,date,rate`), all or none, dry run first."""
    refuse_declared_size(file, fx_manual.MAX_RATE_CSV_BYTES, FILE_TOO_BIG)
    raw = await read_capped(file, fx_manual.MAX_RATE_CSV_BYTES, FILE_TOO_BIG)
    outcome = await run_in_threadpool(
        fx_manual.import_csv,
        session,
        household=household,
        actor_id=user.id,
        raw=raw,
        filename=file.filename,
        dry_run=dry_run,
    )
    response.status_code = 200 if outcome.dry_run else 201
    return manual_out(outcome)


@router.delete(f"{_HOUSE}/rates/manual/{{rate_id}}", status_code=204)
def delete_manual_rate(
    rate_id: str, household: CurrentHousehold, session: SessionDep, user: CurrentUser
) -> None:
    """Delete one manual rate. Undo in History brings it back."""
    fx_manual.delete_rate(session, household=household, rate_id=rate_id, actor_id=user.id)


# --------------------------------------------------------------------------- #
# Fetch now
# --------------------------------------------------------------------------- #


@router.post(f"{_HOUSE}/fetch", response_model=list[FxFetchOut], status_code=202)
def fetch_now(
    body: FxFetchIn,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
    background: BackgroundTasks,
) -> list[FxFetchOut]:
    """Fetch the missing rates, or refetch a range. 202 at once; 409 while one runs.

    The only way a rate arrives from outside. The work runs after the
    response, in a thread; the page polls `fx/status` until `running` is false.
    """
    rows = fx_fetching.start(
        session,
        household,
        user,
        mode=FxFetchMode(body.mode),
        since=body.since,
        until=body.until,
    )
    background.add_task(fx_fetching.run, [row.id for row in rows])
    return [FxFetchOut.model_validate(fx_dashboard.fetch_view(session, row)) for row in rows]
