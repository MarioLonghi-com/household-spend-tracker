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
   A currency the source has never published is left out, and so is one it
   stopped publishing once its last published day is stored (#444): asking
   again would bring back nothing, on every press, forever.
2. Asks each source for every missing date: from the last stored rate, or the
   first time from the earliest transaction in that currency, up to today.
   *Refetch a range* asks for the dates given instead, and overwrites.
3. **One calendar year per request** (#409), oldest first, and each year is
   upserted and committed as it arrives. A backfill over many years used to
   be one answer, which could pass the size or time limit in
   `fx_sources`; now a failure part-way keeps the years before it, and the
   next press of Fetch now starts from the last stored rate -- where it
   stopped. Rerunning a day changes nothing.
4. Writes one `fx_fetches` row per source with the outcome and the counts,
   and, while it runs, how far it has got: the year in flight and how many
   of how many are stored. A failure leaves the year it stopped on there.

**One fetch at a time per instance.** A second press while one runs answers
409. The work runs off the request, in a thread, so the button answers at
once and the page polls `fx/status`. The fetch belongs to the household, not
to the page: it carries on when whoever pressed leaves or signs out, and any
member who opens the page sees it running, who started it and how far it has
got. A fetch the process did not finish -- a restart mid-fetch -- is marked
failed, "interrupted", at the next boot.

**A fetch row always ends** (#444). Any error in the work, a "database is
locked" on one of its commits included, marks the row failed. If even that
cannot be written, the row is only `running` while this process is working
on it: the slot is what says a fetch runs, not the row, so a row left behind
reads as interrupted and the next press records it so.

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

#: What a fetch row says when nothing is working on it any more, but it was
#: never marked: the work could not write its own failure, or the request that
#: started it failed before handing it on.
ABANDONED = "interrupted: the fetch stopped without recording how it ended"

#: How long a press's rows may wait for :func:`run` to pick them up. Past
#: this the request that wrote them is taken to have failed before handing
#: them on, and the next press may take the slot.
HANDOFF_SECONDS = 60.0

#: The pauses between attempts to record a failure, in seconds -- each
#: attempt also waits out SQLite's own busy timeout.
FAIL_RETRIES: tuple[float, ...] = (0.5, 2.0, 5.0)


@dataclass(slots=True)
class _Claim:
    """The instance's one slot, taken by a press and freed when its work ends."""

    ids: tuple[str, ...]
    at: float
    begun: bool = False

    def abandoned(self) -> bool:
        return not self.begun and time.monotonic() - self.at > HANDOFF_SECONDS


#: Guards :data:`_claim`. Held only to read or swap it, never across work.
_GUARD = threading.Lock()
_claim: _Claim | None = None

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


def chunks(since: Date, until: Date) -> list[tuple[Date, Date]]:
    """The requests a range is fetched in: one per calendar year, oldest first.

    The first and the last are cut to the range, so 2021-06-01 to 2024-02-10
    is four: the rest of 2021, 2022, 2023, and 2024 to 10 February.
    """
    out: list[tuple[Date, Date]] = []
    cursor = since
    while cursor <= until:
        end = min(Date(cursor.year, 12, 31), until)
        out.append((cursor, end))
        cursor = end + timedelta(days=1)
    return out


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
        # Only what the source publishes (#444). The base is asked for only
        # when a held currency can be crossed through it.
        published = [code for code in held if publishes(source, code)]
        if not published:
            continue
        wanted = sorted((set(published) | {base}) - {quote})
        wanted = [code for code in wanted if publishes(source, code)]
        if mode is FxFetchMode.range:
            asked = [code for code in wanted if not _ended_before(source, code, since)]  # type: ignore[arg-type]
            if asked:
                plans.append(Plan(source, mode, asked, since, until))  # type: ignore[arg-type]
            continue
        last = _last_stored(session, source, quote, wanted)
        foreign_first = min((first_seen[c] for c in held if c in first_seen), default=None)
        asked, starts = [], []
        for code in wanted:
            if code in last:
                # Stored up to the source's last publication: nothing more
                # will come, so it is complete.
                if not _ended_before(source, code, last[code] + timedelta(days=1)):
                    asked.append(code)
                    starts.append(last[code])
            else:
                first = first_seen.get(code) if code in held else foreign_first
                first = first or today - timedelta(days=household.fx_stale_days)
                if not _ended_before(source, code, first):
                    asked.append(code)
                    starts.append(first)
        if asked:
            plans.append(Plan(source, mode, asked, min(min(starts), today), today))
    if not plans:
        raise ValidationError(
            "no source in this household's chain publishes a rate it is missing -- enter those by hand",
            code="fx.nothing_published",
        )
    return plans


def publishes(source: FxSource, code: str) -> bool:
    """Whether ``source`` has ever published a rate for ``code``."""
    if source is FxSource.ecb:
        return fx_sources.publishes(code)
    return False  # pragma: no cover - every remote FxSource is named above


def _ended_before(source: FxSource, code: str, day: Date) -> bool:
    """Whether ``source`` stopped publishing ``code`` before ``day``."""
    end = fx_sources.last_published(code) if source is FxSource.ecb else None
    return end is not None and end < day


def live_ids() -> frozenset[str]:
    """The fetch rows this process is working on, or about to."""
    with _GUARD:
        if _claim is None or _claim.abandoned():
            return frozenset()
        return frozenset(_claim.ids)


def is_running() -> bool:
    """A fetch is running on this instance, whoever pressed it."""
    return bool(live_ids())


def shown(row: FxFetch, live: frozenset[str] | None = None) -> tuple[FxFetchOutcome, str | None]:
    """The outcome and error a fetch row is shown with.

    A `running` row nothing is working on is shown as what it is, failed and
    interrupted; the next press stores that.
    """
    if row.outcome is FxFetchOutcome.running and row.id not in (live_ids() if live is None else live):
        return FxFetchOutcome.failed, ABANDONED
    return row.outcome, row.error


def _take_slot() -> _Claim:
    global _claim
    with _GUARD:
        if _claim is not None and not _claim.abandoned():
            raise Conflict("a fetch is already running -- wait for it to finish", code="fx.fetch_running")
        _claim = _Claim(ids=(), at=time.monotonic())
        return _claim


def _free_slot(claim: _Claim) -> None:
    global _claim
    with _GUARD:
        if _claim is claim:
            _claim = None


def _close_abandoned(session: Session) -> int:
    """Mark failed every `running` row nothing works on. Called holding the slot,
    so that is all of them. `fx_fetches` is not audited, so a bulk update."""
    live = live_ids()
    return session.execute(
        update(FxFetch)  # audit-exempt: fx_fetches is not audited
        .where(FxFetch.outcome == FxFetchOutcome.running, FxFetch.id.not_in(live))
        .values(outcome=FxFetchOutcome.failed, error=ABANDONED, finished_at=utcnow())
    ).rowcount or 0


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
    is released when that finishes, here if anything before it fails, or by
    :func:`abandon` if the caller fails before handing them on.
    """
    plans = plan(session, household, mode=mode, since=since, until=until)
    claim = _take_slot()
    try:
        if closed := _close_abandoned(session):
            log.warning("exchange rates: %d fetch(es) left running with nothing working on them, marked failed", closed)
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
                chunks_total=len(chunks(one.since, one.until)),
                chunks_done=0,
            )
            for one in plans
        ]
        session.add_all(rows)
        session.commit()
        ids = tuple(row.id for row in rows)
        with _GUARD:
            claim.ids = ids
        return rows
    except BaseException:
        session.rollback()
        _free_slot(claim)
        raise


def abandon(fetch_ids: list[str], error: str = ABANDONED) -> None:
    """The request that started these failed before handing them to :func:`run`.

    Marks them failed and frees the slot. Its own session, because the
    request's may be the thing that failed. Never raises.
    """
    from ..db import session_scope

    with _GUARD:
        claim = _claim if _claim is not None and _claim.ids == tuple(fetch_ids) else None
    try:
        for fetch_id in fetch_ids:
            with session_scope() as session:
                _fail(session, fetch_id, error)
    except Exception:  # pragma: no cover - defensive; the slot is freed below
        log.exception("could not record an abandoned exchange-rate fetch")
    finally:
        if claim is not None:
            _free_slot(claim)


def run(fetch_ids: list[str], *, get=None) -> None:
    """Do the fetches :func:`start` recorded, then free the slot. Never raises.

    Its own session: this runs after the response, with no request around it.
    """
    from ..db import session_scope

    with _GUARD:
        claim = _claim
        if claim is None or claim.ids != tuple(fetch_ids):
            # Taken over after HANDOFF_SECONDS: the next press closed these.
            log.warning("exchange rates: a fetch began after its slot was given up, skipped")
            return
        claim.begun = True
    try:
        for fetch_id in fetch_ids:
            with session_scope() as session:
                _one(session, fetch_id, get=get)
    except Exception:
        log.exception("an exchange-rate fetch failed outside its own record")
    finally:
        _free_slot(claim)


def _fail(session: Session, fetch_id: str, error: str) -> FxFetch | None:
    """Mark a fetch failed, from whatever state the session is in.

    Tried again after each of :data:`FAIL_RETRIES`, because the error being
    recorded may be the database being locked. Returns the row, or None if it
    could not be written -- then the row reads as interrupted once the slot is
    free (:func:`shown`), and the next press stores that.
    """
    for wait in (*FAIL_RETRIES, None):
        try:
            session.rollback()
            row = session.get(FxFetch, fetch_id)
            if row is None:
                return None
            if row.outcome is FxFetchOutcome.running:
                row.outcome = FxFetchOutcome.failed
                row.error = error
                row.finished_at = utcnow()
                session.commit()
            return row
        except Exception:
            log.exception("could not record that exchange-rate fetch %s failed", fetch_id)
            if wait is None:
                return None
            time.sleep(wait)
    return None  # pragma: no cover - the loop returns


def _one(session: Session, fetch_id: str, *, get=None) -> None:
    """Fetch one source's range a year at a time, committing each year.

    Before each request the row says which dates are in flight, and is
    committed, so a member polling the page sees it. After each, the year's
    rates and the row's counts are committed together. A failure rolls back
    only the year it was on, and leaves its dates on the row. Whatever raises
    -- a commit as much as a request -- the row ends failed (#444).
    """
    fetch = session.get(FxFetch, fetch_id)
    if fetch is None or fetch.outcome is not FxFetchOutcome.running:
        return
    began = time.monotonic()
    try:
        _years(session, fetch, get=get)
    except Exception as exc:
        log.exception("exchange-rate fetch %s failed", fetch_id)
        fetch = _fail(session, fetch_id, f"the fetch stopped on an error: {type(exc).__name__}")
        if fetch is None:
            return
    log.info(
        "exchange rates: %s %s in %.1f s, %d of %d requests, %d added, %d unchanged%s",
        fetch.source.value,
        fetch.outcome.value,
        time.monotonic() - began,
        fetch.chunks_done,
        fetch.chunks_total,
        fetch.rates_added,
        fetch.rates_unchanged,
        f" ({fetch.error})" if fetch.error else "",
    )


def _years(session: Session, fetch: FxFetch, *, get=None) -> None:
    """The work of :func:`_one`. Records a source's failure itself; anything
    else that raises is for the caller to record."""
    fetch_id = fetch.id
    spans = chunks(fetch.requested_since, fetch.requested_until)
    fetch.chunks_total = len(spans)
    overwrite = fetch.mode is FxFetchMode.range
    error: str | None = None
    for number, (since, until) in enumerate(spans, start=1):
        fetch.chunk_since, fetch.chunk_until = since, until
        session.commit()
        try:
            if fetch.source is FxSource.ecb:
                published = fx_sources.fetch_ecb(fetch.currencies, since, until, get=get)
            else:  # pragma: no cover - every remote FxSource has an adapter above
                raise fx_sources.SourceError(f"no adapter for {fetch.source.value}")
            added, unchanged = store(session, fetch.source, published, overwrite=overwrite)
        except fx_sources.SourceError as exc:
            error = str(exc)
        except Exception as exc:
            log.exception("exchange-rate fetch %s failed", fetch_id)
            error = f"the fetch stopped on an error: {type(exc).__name__}"
        if error is not None:
            # Only this year's rates go; the years before it are committed.
            session.rollback()
            fetch = session.get(FxFetch, fetch_id)
            fetch.outcome = FxFetchOutcome.failed
            fetch.error = error
            break
        fetch.rates_added += added
        fetch.rates_unchanged += unchanged
        fetch.chunks_done = number
        session.commit()
        log.info(
            "exchange rates: %s %s to %s, %d of %d, %d added, %d unchanged",
            fetch.source.value,
            since.isoformat(),
            until.isoformat(),
            number,
            len(spans),
            added,
            unchanged,
        )
    else:
        fetch.outcome = FxFetchOutcome.ok
        fetch.chunk_since = fetch.chunk_until = None
    fetch.finished_at = utcnow()
    session.commit()


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
