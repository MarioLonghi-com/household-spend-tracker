"""Fetch now: published rates, fetched when a member asks and only then (#396 §5.1).

There is no scheduled job and no fetch on page load. A self-hosted ledger
makes no outbound request nobody asked for, so a firewalled or offline
install needs no exception and no setting; the person who pressed the button
is the provenance, recorded on an `fx_fetches` row; and nothing changes past
figures overnight.

What one press does:

1. Takes the remote sources in the household's chain (the ECB in v1) and the
   currencies it holds, base excluded -- plus the base itself when the
   source quotes against another currency, because a cross rate needs both.
2. Asks each source for every missing date: from the last stored rate, or the
   first time from the earliest transaction in that currency, up to today.
   *Refetch a range* asks for the dates given instead, and overwrites.
3. Upserts the rows. Rerunning a day changes nothing.
4. Writes one `fx_fetches` row per source with the outcome and the counts.

**One fetch at a time per instance.** A second press while one runs answers
409. The work runs off the request, in a thread, so the button answers at
once and the page polls `fx/status`. A fetch the process did not finish -- a
restart mid-fetch -- is marked failed, "interrupted", at the next boot.

A failed fetch keeps the rates already stored and never touches a report.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date as Date
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..errors import Conflict, ValidationError
from ..models import (
    Account,
    FxFetch,
    FxFetchMode,
    FxFetchOutcome,
    FxRate,
    FxSource,
    Household,
    Transaction,
    User,
    utcnow,
)
from . import fx_sources
from .fx import chain_of

log = logging.getLogger(__name__)

#: How long the fetch history is kept. Swept by `app/auth/housekeeping.py`.
RETENTION = timedelta(days=90)

#: What a fetch row says when the process stopped under it.
INTERRUPTED = "interrupted: the application stopped before the fetch finished"

#: Held from the press until the background work ends, by whichever thread
#: that is. A plain `Lock`, which any thread may release.
_RUNNING = threading.Lock()

#: The currency each remote source quotes against.
QUOTES: dict[FxSource, str] = {FxSource.ecb: "EUR"}


@dataclass(frozen=True, slots=True)
class Plan:
    """What one source would be asked for."""

    source: FxSource
    mode: FxFetchMode
    currencies: list[str]
    since: Date
    until: Date


def held_currencies(session: Session, household: Household) -> list[str]:
    """The currencies of the household's accounts, base excluded."""
    base = household.base_currency.upper()
    return sorted(
        {
            code.upper()
            for code in session.execute(
                select(Account.currency).where(Account.household_id == household.id).distinct()
            ).scalars()
        }
        - {base}
    )


def earliest_transactions(session: Session, household: Household) -> dict[str, Date]:
    """Per currency, the date of the household's earliest transaction in it."""
    rows = session.execute(
        select(Account.currency, func.min(Transaction.date))
        .join(Transaction, Transaction.account_id == Account.id)
        .where(Account.household_id == household.id)
        .group_by(Account.currency)
    ).all()
    return {code.upper(): first for code, first in rows if first is not None}


def _last_stored(session: Session, source: FxSource, quote: str, currencies: list[str]) -> dict[str, Date]:
    rows = session.execute(
        select(FxRate.currency, func.max(FxRate.date))
        .where(FxRate.source == source, FxRate.quote_currency == quote, FxRate.currency.in_(currencies))
        .group_by(FxRate.currency)
    ).all()
    return {code: last for code, last in rows}


def plan(
    session: Session,
    household: Household,
    *,
    mode: FxFetchMode = FxFetchMode.missing,
    since: Date | None = None,
    until: Date | None = None,
    today: Date | None = None,
) -> list[Plan]:
    """What a press of Fetch now would ask each remote source for. Writes nothing.

    The dashboard shows this before anyone presses, so nobody presses to find
    out: "Would fetch GBP, SEK, BRL from 30 Oct 2026 to today".
    """
    today = today or Date.today()
    remote = [source for source in chain_of(household) if source.is_remote]
    if not remote:
        raise ValidationError(
            "no source in this household's chain is fetched -- add the ECB to it first",
            code="fx.nothing_to_fetch",
        )
    held = held_currencies(session, household)
    if not held:
        raise ValidationError(
            "this household keeps every account in its base currency, so there is no rate to fetch",
            code="fx.no_foreign_currency",
        )
    if mode is FxFetchMode.range:
        if since is None or until is None:
            raise ValidationError(
                "refetching a range needs a first and a last date", code="fx.range_needs_dates"
            )
        if since > until:
            raise ValidationError(
                f"the range starts on {since.isoformat()}, after it ends on {until.isoformat()}",
                code="fx.range_backwards",
                params={"since": since, "until": until},
            )
        if until > today:
            raise ValidationError(
                f"{until.isoformat()} is in the future, and no rate is published for it yet",
                code="fx.range_in_future",
                params={"until": until},
            )

    base = household.base_currency.upper()
    first_seen = earliest_transactions(session, household)
    plans: list[Plan] = []
    for source in remote:
        quote = QUOTES[source]
        wanted = sorted((set(held) | {base}) - {quote})
        if mode is FxFetchMode.range:
            plans.append(Plan(source, mode, wanted, since, until))  # type: ignore[arg-type]
            continue
        last = _last_stored(session, source, quote, wanted)
        foreign_first = min((first_seen[c] for c in held if c in first_seen), default=None)
        starts: list[Date] = []
        for code in wanted:
            if code in last:
                starts.append(last[code])
            else:
                first = first_seen.get(code) if code in held else foreign_first
                starts.append(first or today - timedelta(days=household.fx_stale_days))
        start = min(min(starts), today)
        plans.append(Plan(source, mode, wanted, start, today))
    return plans


def is_running(session: Session) -> bool:
    return (
        session.execute(
            select(func.count()).select_from(FxFetch).where(FxFetch.outcome == FxFetchOutcome.running)
        ).scalar_one()
        > 0
    )


def start(
    session: Session,
    household: Household,
    user: User,
    *,
    mode: FxFetchMode = FxFetchMode.missing,
    since: Date | None = None,
    until: Date | None = None,
) -> list[FxFetch]:
    """Record the fetch and hold the instance's one slot for it.

    Writes one `running` row per source and commits, so the page's first poll
    sees it. The caller hands the ids to :func:`run` off the request; the slot
    is released when that finishes, or here if anything before it fails.
    """
    plans = plan(session, household, mode=mode, since=since, until=until)
    if not _RUNNING.acquire(blocking=False):
        raise Conflict("a fetch is already running -- wait for it to finish", code="fx.fetch_running")
    try:
        if is_running(session):
            raise Conflict("a fetch is already running -- wait for it to finish", code="fx.fetch_running")
        rows = [
            FxFetch(
                household_id=household.id,
                started_by_id=user.id,
                source=one.source,
                mode=one.mode,
                requested_since=one.since,
                requested_until=one.until,
                currencies=one.currencies,
                outcome=FxFetchOutcome.running,
            )
            for one in plans
        ]
        session.add_all(rows)
        session.commit()
        return rows
    except BaseException:
        _RUNNING.release()
        raise


def run(fetch_ids: list[str], *, get=None) -> None:
    """Do the fetches :func:`start` recorded, then free the slot. Never raises.

    Its own session: this runs after the response, with no request around it.
    """
    from ..db import session_scope

    try:
        for fetch_id in fetch_ids:
            with session_scope() as session:
                _one(session, fetch_id, get=get)
    except Exception:  # pragma: no cover - defensive; `_one` records its own failures
        log.exception("an exchange-rate fetch failed outside its own record")
    finally:
        _RUNNING.release()


def _one(session: Session, fetch_id: str, *, get=None) -> None:
    fetch = session.get(FxFetch, fetch_id)
    if fetch is None or fetch.outcome is not FxFetchOutcome.running:
        return
    began = time.monotonic()
    try:
        if fetch.source is FxSource.ecb:
            published = fx_sources.fetch_ecb(
                fetch.currencies, fetch.requested_since, fetch.requested_until, get=get
            )
        else:  # pragma: no cover - every remote FxSource has an adapter above
            raise fx_sources.SourceError(f"no adapter for {fetch.source.value}")
        added, unchanged = store(
            session, fetch.source, published, overwrite=fetch.mode is FxFetchMode.range
        )
    except fx_sources.SourceError as exc:
        session.rollback()
        fetch = session.get(FxFetch, fetch_id)
        fetch.outcome = FxFetchOutcome.failed
        fetch.error = str(exc)
    except Exception as exc:
        log.exception("exchange-rate fetch %s failed", fetch_id)
        session.rollback()
        fetch = session.get(FxFetch, fetch_id)
        fetch.outcome = FxFetchOutcome.failed
        fetch.error = f"the fetch stopped on an error: {type(exc).__name__}"
    else:
        fetch.outcome = FxFetchOutcome.ok
        fetch.rates_added = added
        fetch.rates_unchanged = unchanged
    fetch.finished_at = utcnow()
    session.commit()
    log.info(
        "exchange rates: %s %s in %.1f s, %d added, %d unchanged%s",
        fetch.source.value,
        fetch.outcome.value,
        time.monotonic() - began,
        fetch.rates_added,
        fetch.rates_unchanged,
        f" ({fetch.error})" if fetch.error else "",
    )


def store(
    session: Session,
    source: FxSource,
    published: list[fx_sources.Published],
    *,
    overwrite: bool = False,
) -> tuple[int, int]:
    """Upsert published rates. Returns (added, unchanged).

    A day already stored is left as it is, unless ``overwrite`` -- the range
    refetch -- and its rate differs, when it is replaced and counted as added.
    Not audited: `fx_rates` is excluded, so these are plain ORM writes with no
    batch.
    """
    if not published:
        return 0, 0
    days = [p.date for p in published]
    existing = {
        (row.quote_currency, row.currency, row.date): row
        for row in session.execute(
            select(FxRate).where(
                FxRate.source == source,
                FxRate.currency.in_({p.currency for p in published}),
                FxRate.date >= min(days),
                FxRate.date <= max(days),
            )
        ).scalars()
    }
    added = unchanged = 0
    now = utcnow()
    for one in published:
        key = (one.quote, one.currency, one.date)
        row = existing.get(key)
        if row is None:
            row = FxRate(
                source=source,
                quote_currency=one.quote,
                currency=one.currency,
                date=one.date,
                rate=one.rate,
                fetched_at=now,
            )
            session.add(row)
            existing[key] = row
            added += 1
        elif overwrite and row.rate != one.rate:
            row.rate = one.rate
            row.fetched_at = now
            added += 1
        else:
            unchanged += 1
    session.flush()
    return added, unchanged


def mark_interrupted(engine: Engine) -> int:
    """At boot: a fetch still `running` belongs to a process that has stopped.

    Whatever it upserted before the stop stays; the next press fetches the
    rest. `fx_fetches` is not audited, so a bulk update.
    """
    with Session(engine) as own:
        done = own.execute(
            update(FxFetch)  # audit-exempt: fx_fetches is not audited
            .where(FxFetch.outcome == FxFetchOutcome.running)
            .values(outcome=FxFetchOutcome.failed, error=INTERRUPTED, finished_at=utcnow())
        ).rowcount or 0
        own.commit()
    return done


def sweep(session: Session, *, now=None) -> int:
    """Fetch history older than :data:`RETENTION`. Not audited, so a bulk delete."""
    cutoff = (now or utcnow()) - RETENTION
    return session.execute(
        delete(FxFetch).where(  # audit-exempt: fx_fetches is not audited
            FxFetch.started_at < cutoff, FxFetch.outcome != FxFetchOutcome.running
        )
    ).rowcount or 0
