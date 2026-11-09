"""The Exchange rates page as data: what the household chose, and what it has (#396 §5.3).

One call, `GET .../fx/status`, reads top to bottom: the headline tiles, what
Fetch now would do, one row per currency the household holds, a month strip
per currency, the fetch history, and what no source covers. Every figure is
counted here from stored rows; nothing is fetched by reading it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import currencies as iso
from ..errors import DomainError, ValidationError
from ..models import (
    Account,
    FxFetch,
    FxFetchOutcome,
    FxManualRate,
    FxPolicy,
    FxSource,
    Household,
    Transaction,
)
from . import fx, fx_fetching, fx_manual, fx_sources

#: How many fetches the history shows.
HISTORY = 50


@dataclass(frozen=True, slots=True)
class FetchView:
    id: str
    source: str
    mode: str
    requested_since: Date
    requested_until: Date
    currencies: list[str]
    started_at: datetime
    finished_at: datetime | None
    outcome: str
    rates_added: int
    rates_unchanged: int
    error: str | None
    started_by: str | None
    #: Whole milliseconds, NULL while it runs.
    duration_ms: int | None
    #: How far it got (#409): requests in all, and how many are stored.
    chunks_total: int
    chunks_done: int
    #: The dates of the request in flight, or the one it stopped on.
    chunk_since: Date | None
    chunk_until: Date | None


@dataclass(frozen=True, slots=True)
class AccountRef:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class MonthCell:
    #: ``YYYY-MM``.
    month: str
    #: ``rates``: has rates, and every transaction date in it has one within
    #: the stale limit. ``gap``: has a transaction date without one.
    #: ``empty``: neither transactions nor rates.
    state: str


@dataclass(frozen=True, slots=True)
class CurrencyRow:
    currency: str
    accounts: list[AccountRef]
    #: The first source in the chain with a rate for it today, or NULL.
    source_in_use: str | None
    first_rate: Date | None
    last_rate: Date | None
    #: Distinct dates the chain has a rate for.
    rates: int
    age_days: int | None
    #: Distinct dates with a transaction in this currency.
    transaction_dates: int
    #: Of those, the share with a rate within the stale limit, rounded down.
    coverage_percent: int | None
    gaps: int
    #: ``ok``, ``stale``, ``not_covered`` or ``manual_only``.
    status: str
    #: Days the household's own linked transfers give a rate for it.
    own_rates: int
    #: ISO 4217 assigned the code. False for a code from before #110's check.
    iso: bool
    months: list[MonthCell]


@dataclass(frozen=True, slots=True)
class NotCovered:
    currency: str
    #: ``not_iso``: the code is not a currency, so no source will ever publish
    #: it. ``no_source``: a real currency no source in the chain publishes.
    reason: str
    accounts: list[AccountRef]


@dataclass(frozen=True, slots=True)
class WouldFetch:
    source: str
    mode: str
    currencies: list[str]
    since: Date
    until: Date


@dataclass(frozen=True, slots=True)
class Tiles:
    last_fetch: FetchView | None
    as_of: Date | None
    stale: bool
    covered: int
    total: int
    missing: list[str]
    gaps: int
    manual_count: int
    manual_changed_at: datetime | None
    manual_changed_by: str | None


@dataclass(frozen=True, slots=True)
class Dashboard:
    base_currency: str
    chain: list[str]
    stale_days: int
    monthly_policy: str
    show_own_rates: bool
    tiles: Tiles
    #: A fetch is running on this instance, whoever pressed it.
    running: bool
    #: This household's fetches still running.
    running_fetches: list[FetchView]
    would_fetch: list[WouldFetch]
    #: Why nothing would be fetched: an error code, when there is one.
    would_fetch_refused: str | None
    would_fetch_reason: str | None
    currencies: list[CurrencyRow]
    not_covered: list[NotCovered]
    history: list[FetchView]
    rates_status: fx.RatesStatus


def fetch_view(session: Session, row: FxFetch) -> FetchView:
    duration = None
    if row.finished_at is not None:
        duration = int((row.finished_at - row.started_at).total_seconds() * 1000)
    return FetchView(
        id=row.id,
        source=row.source.value,
        mode=row.mode.value,
        requested_since=row.requested_since,
        requested_until=row.requested_until,
        currencies=list(row.currencies or []),
        started_at=row.started_at,
        finished_at=row.finished_at,
        outcome=row.outcome.value,
        rates_added=row.rates_added,
        rates_unchanged=row.rates_unchanged,
        error=row.error,
        started_by=fx._name(session, row.started_by_id),
        duration_ms=duration,
        chunks_total=row.chunks_total,
        chunks_done=row.chunks_done,
        chunk_since=row.chunk_since,
        chunk_until=row.chunk_until,
    )


def _months(first: Date, last: Date) -> list[Date]:
    out, cursor = [], first.replace(day=1)
    while cursor <= last:
        out.append(cursor)
        cursor = cursor.replace(year=cursor.year + (cursor.month == 12), month=cursor.month % 12 + 1)
    return out


def status(session: Session, household: Household, *, today: Date | None = None) -> Dashboard:
    """Everything the Exchange rates page shows, from one read."""
    rates = fx.rates(session, household, today=today)
    today = rates.today
    base = rates.base

    accounts: dict[str, list[AccountRef]] = {}
    for account in session.execute(
        select(Account).where(Account.household_id == household.id).order_by(Account.name)
    ).scalars():
        code = account.currency.upper()
        if code != base:
            accounts.setdefault(code, []).append(AccountRef(id=account.id, name=account.name))

    dates_by_currency: dict[str, list[Date]] = {}
    for code, day in session.execute(
        select(Account.currency, Transaction.date)
        .join(Transaction, Transaction.account_id == Account.id)
        .where(Account.household_id == household.id)
        .distinct()
        .order_by(Transaction.date)
    ).all():
        dates_by_currency.setdefault(code.upper(), []).append(day)

    rows: list[CurrencyRow] = []
    not_covered: list[NotCovered] = []
    for code in sorted(accounts):
        series = [rates.series(source, code) for source in rates.chain]
        rate_days = sorted(
            {p.date for s in series for p in s.days} | {m for s in series for m in s.months}
        )
        latest = rates.latest(code)
        txn_dates = dates_by_currency.get(code, [])
        uncovered: set[Date] = set()
        for day in txn_dates:
            used = rates.rate_on(code, day, policy=FxPolicy.spot)
            if used is None or used.stale:
                uncovered.add(day)
        months: list[MonthCell] = []
        first_month = min([*txn_dates[:1], *rate_days[:1]], default=None)
        if first_month is not None:
            txn_months = {d.replace(day=1) for d in txn_dates}
            gap_months = {d.replace(day=1) for d in uncovered}
            rate_months = {d.replace(day=1) for d in rate_days}
            for month in _months(first_month, today):
                state = (
                    "gap"
                    if month in gap_months
                    else "rates"
                    if month in txn_months or month in rate_months
                    else "empty"
                )
                months.append(MonthCell(month=month.strftime("%Y-%m"), state=state))

        is_iso = code in iso.ASSIGNED
        publishes = (
            (FxSource.ecb in rates.chain and code in fx_sources.ECB_PUBLISHES)
            or any(series)
        )
        if latest is None:
            row_status = "not_covered"
        elif latest.stale:
            row_status = "stale"
        elif latest.source == FxSource.manual.value and not any(
            s for source, s in zip(rates.chain, series, strict=True) if source is not FxSource.manual
        ):
            row_status = "manual_only"
        else:
            row_status = "ok"
        if latest is None and (not is_iso or not publishes):
            not_covered.append(
                NotCovered(
                    currency=code,
                    reason="not_iso" if not is_iso else "no_source",
                    accounts=accounts[code],
                )
            )
        rows.append(
            CurrencyRow(
                currency=code,
                accounts=accounts[code],
                source_in_use=latest.source if latest else None,
                first_rate=rate_days[0] if rate_days else None,
                last_rate=rate_days[-1] if rate_days else None,
                rates=len(rate_days),
                age_days=(today - latest.rate_date).days if latest else None,
                transaction_dates=len(txn_dates),
                coverage_percent=(
                    (len(txn_dates) - len(uncovered)) * 100 // len(txn_dates) if txn_dates else None
                ),
                gaps=len(uncovered),
                status=row_status,
                own_rates=len(rates.series(FxSource.own, code).days),
                iso=is_iso,
                months=months,
            )
        )

    fetches = list(
        session.execute(
            select(FxFetch)
            .where(FxFetch.household_id == household.id)
            .order_by(FxFetch.started_at.desc())
            .limit(HISTORY)
        ).scalars()
    )
    history = [fetch_view(session, row) for row in fetches]
    finished = [view for view in history if view.outcome != FxFetchOutcome.running.value]

    would: list[WouldFetch] = []
    refused_code: str | None = None
    refused_reason: str | None = None
    try:
        for one in fx_fetching.plan(session, household, today=today):
            would.append(
                WouldFetch(
                    source=one.source.value,
                    mode=one.mode.value,
                    currencies=one.currencies,
                    since=one.since,
                    until=one.until,
                )
            )
    except DomainError as exc:
        refused_code, refused_reason = exc.code, str(exc)

    manual_count = session.execute(
        select(func.count()).select_from(FxManualRate).where(FxManualRate.household_id == household.id)
    ).scalar_one()
    changed_at, changed_by = fx_manual.last_change(session, household.id)
    rates_status = fx.rates_status(session, household, accounts.keys(), rates=rates)

    return Dashboard(
        base_currency=base,
        chain=[s.value for s in rates.chain],
        stale_days=rates.stale_days,
        monthly_policy=rates.monthly_policy.value,
        show_own_rates=rates.show_own_rates,
        tiles=Tiles(
            last_fetch=finished[0] if finished else None,
            as_of=rates_status.as_of,
            stale=rates_status.stale,
            covered=sum(1 for row in rows if row.status != "not_covered"),
            total=len(rows),
            missing=[row.currency for row in rows if row.status == "not_covered"],
            gaps=sum(row.gaps for row in rows),
            manual_count=int(manual_count),
            manual_changed_at=changed_at,
            manual_changed_by=fx._name(session, changed_by),
        ),
        running=rates_status.running,
        running_fetches=[v for v in history if v.outcome == FxFetchOutcome.running.value],
        would_fetch=would,
        would_fetch_refused=refused_code,
        would_fetch_reason=refused_reason,
        currencies=rows,
        not_covered=not_covered,
        history=history,
        rates_status=rates_status,
    )


# --------------------------------------------------------------------------- #
# Changing the settings
# --------------------------------------------------------------------------- #

#: The stale limit a person may choose, in days.
STALE_DAYS_MIN = 1
STALE_DAYS_MAX = 365


def update_settings(
    household: Household,
    *,
    chain: list[str],
    stale_days: int,
    monthly_policy: str,
    show_own_rates: bool,
) -> Household:
    """Set where rates come from and how they are used. The caller opens the batch.

    Any member may, like the household PATCH that sets `base_currency`, which
    changes every past converted figure just as much. Audited through the
    batch, so `/changes/latest` moves and History shows who changed what.
    """
    names: list[str] = []
    for name in chain:
        name = name.strip().lower()
        if name not in FxSource.__members__:
            raise ValidationError(
                f"{name!r} is not a source of exchange rates -- use manual, ecb or own",
                code="fx.unknown_source",
                params={"source": name},
            )
        if name in names:
            raise ValidationError(
                f"{name} is in the chain twice", code="fx.source_twice", params={"source": name}
            )
        names.append(name)
    if not STALE_DAYS_MIN <= stale_days <= STALE_DAYS_MAX:
        raise ValidationError(
            f"a rate can be treated as stale after {STALE_DAYS_MIN} to {STALE_DAYS_MAX} days",
            code="fx.stale_days_range",
            params={"min": STALE_DAYS_MIN, "max": STALE_DAYS_MAX},
        )
    if monthly_policy not in FxPolicy.__members__:
        raise ValidationError(
            f"{monthly_policy!r} is not a rate for monthly figures -- use month_avg, "
            "month_end or spot",
            code="fx.unknown_policy",
            params={"policy": monthly_policy},
        )
    household.fx_chain = ",".join(names)
    household.fx_stale_days = stale_days
    household.fx_monthly_policy = monthly_policy
    household.fx_show_own_rates = show_own_rates
    return household
