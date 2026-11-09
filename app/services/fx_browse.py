"""The stored rates, a page at a time, by day or by month (#410).

What the Exchange rates page's Browse list and the agent's ``fx/rates`` read.
Years of daily rates for several currencies are tens of thousands of rows, so
the list is ordered here and only one page of it is answered; the screen never
holds the whole range.

Two granularities of list:

- **day**: the rows as stored -- the ECB's publications and the rates entered
  by hand -- each with what it means per one base currency and what the
  household's own transfers gave that day.
- **month_avg** / **month_end**: one line per currency per month, the figure
  the rate policy of that name gives (`FxPolicy`), found by `fx.Rates` exactly
  as a report finds it. Nothing is averaged here; the chain's own finding is
  reused, so this list and a converted report cannot disagree about a month.

**Nothing here converts an amount.** It reads rates and orders them.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FxPeriod, FxPolicy, FxRate, FxSource, Household
from . import fx, fx_fetching, fx_manual

#: The list's granularities: stored rows, or a month per line by a policy.
DAY = "day"
GRANULARITIES = (DAY, FxPolicy.month_avg.value, FxPolicy.month_end.value)

#: What the list sorts by, one per column.
SORTS = ("date", "currency", "source", "rate", "in_base", "rate_date", "own")


@dataclass(slots=True)
class Row:
    source: str
    quote_currency: str
    currency: str
    #: The publication day, or the first day of the month.
    date: Date
    period: FxPeriod
    #: The rate as stored (a day), or as the policy found it per one base (a month).
    rate_text: str
    rate: Decimal | None
    id: str | None = None
    in_base: Decimal | None = None
    #: The stored text, when the stored rate already is per one base.
    in_base_text: str | None = None
    own: Decimal | None = None
    #: A month's line only: the date of the rate the policy used, and its age.
    rate_date: Date | None = None
    stale: bool | None = None

    @property
    def in_base_shown(self) -> str | None:
        if self.in_base_text is not None:
            return self.in_base_text
        return fx.rate_text(self.in_base) if self.in_base is not None else None


@dataclass(slots=True)
class Page:
    base: str
    granularity: str
    total: int
    offset: int
    rows: list[Row]
    #: Where in ``rows`` a jump landed, or ``None`` when nothing was asked.
    focus: int | None


# --------------------------------------------------------------------------- #
# The rows
# --------------------------------------------------------------------------- #


def _month_end(start: Date) -> Date:
    return start.replace(day=calendar.monthrange(start.year, start.month)[1])


def _daily(
    session: Session,
    household: Household,
    rates: fx.Rates,
    *,
    wanted: set[str],
    currency: str | None,
    since: Date | None,
    until: Date | None,
    source: str | None,
) -> tuple[list[Row], Callable[[Row], None]]:
    """The stored rows, and how to give one its base meaning and own rate.

    The second half is applied to a page alone unless the sort needs it for
    every row: crossing ten thousand rates to show a hundred is the slow part.
    """
    base = rates.base
    rows: list[Row] = []
    if source in (None, FxSource.ecb.value) and wanted:
        stmt = select(FxRate.quote_currency, FxRate.currency, FxRate.date, FxRate.rate).where(
            FxRate.source == FxSource.ecb, FxRate.currency.in_(wanted)
        )
        if since:
            stmt = stmt.where(FxRate.date >= since)
        if until:
            stmt = stmt.where(FxRate.date <= until)
        for quote, code, day, text in session.execute(stmt).all():
            rows.append(
                Row(
                    source=FxSource.ecb.value,
                    quote_currency=quote,
                    currency=code,
                    date=day,
                    period=FxPeriod.day,
                    rate_text=text,
                    rate=fx.parse_rate(text),
                )
            )
    if source in (None, FxSource.manual.value):
        for one in fx_manual.list_rates(session, household.id, currency=currency, since=since, until=until):
            rows.append(
                Row(
                    id=one.id,
                    source=FxSource.manual.value,
                    quote_currency=one.quote_currency,
                    currency=one.currency,
                    date=one.date,
                    period=FxPeriod(one.period),
                    rate_text=one.rate,
                    rate=fx.parse_rate(one.rate),
                    in_base_text=one.rate if one.quote_currency == base else None,
                )
            )

    def fill(row: Row) -> None:
        if row.in_base_text is not None:
            row.in_base = fx.parse_rate(row.in_base_text)
        elif row.currency != base:
            for point in rates.series(FxSource(row.source), row.currency).within(row.date, row.date):
                row.in_base = point.rate
                break
        row.own = rates.own_rate(row.currency, row.date)

    return rows, fill


def _months(
    rates: fx.Rates,
    *,
    wanted: set[str],
    since: Date | None,
    until: Date | None,
    source: str | None,
    policy: FxPolicy,
) -> list[Row]:
    """One line per currency per month that holds a rate, at ``policy``.

    The months are the ones a source has a rate in, within the dates asked.
    Without a source the chain finds each month's figure, as a report would;
    with one, that source alone does.
    """
    sources = [FxSource(source)] if source else rates.chain
    first = since or Date.min
    last = until or Date.max
    rows: list[Row] = []
    for code in sorted(wanted - {rates.base}):
        months: set[Date] = set()
        for src in sources:
            series = rates.series(src, code)
            months.update(point.date.replace(day=1) for point in series.within(first, last))
            months.update(start for start in series.months if start <= last and _month_end(start) >= first)
        for start in months:
            used = (
                rates.rate_from(FxSource(source), code, start, policy=policy)
                if source
                else rates.rate_on(code, start, policy=policy)
            )
            if used is None:
                continue
            rows.append(
                Row(
                    source=used.source,
                    quote_currency=rates.base,
                    currency=code,
                    date=start,
                    period=FxPeriod.month,
                    rate_text=used.rate_text,
                    rate=used.rate,
                    in_base=used.rate,
                    own=rates.own_rate_in_month(code, start, policy),
                    rate_date=used.rate_date,
                    stale=used.stale,
                )
            )
    return rows


# --------------------------------------------------------------------------- #
# The order
# --------------------------------------------------------------------------- #

_VALUE: dict[str, Callable[[Row], Any]] = {
    "date": lambda row: row.date,
    "currency": lambda row: row.currency,
    "source": lambda row: row.source,
    "rate": lambda row: row.rate,
    "in_base": lambda row: row.in_base,
    "rate_date": lambda row: row.rate_date,
    "own": lambda row: row.own,
}

#: The columns whose value needs `fill` before it can be sorted on.
_FILLED = {"in_base", "own"}


def _ordered(rows: list[Row], sort: str, direction: str) -> list[Row]:
    """Rows in the order the heading says, the way `lib/sorting.ts` orders them.

    A blank sorts last in both directions. "As published" groups by the
    currency it is quoted against, in one order whichever way the column
    points, and turns only the figures round. Ties fall back to newest
    first, then currency and source, so a page boundary is the same on
    every request.
    """
    rows.sort(key=lambda r: (r.date, r.currency, r.source, r.period.value), reverse=True)
    if sort == "date" and direction == "desc":
        return rows
    value = _VALUE[sort]
    filled = [r for r in rows if value(r) is not None]
    blank = [r for r in rows if value(r) is None]
    filled.sort(key=value, reverse=direction == "desc")
    if sort == "rate":
        filled.sort(key=lambda r: r.quote_currency)
    return filled + blank


def _nearest(rows: list[Row], at: Date) -> int | None:
    """The first row, in the order shown, whose date is closest to ``at``."""
    if not rows:
        return None
    return min(range(len(rows)), key=lambda i: (abs((rows[i].date - at).days), i))


# --------------------------------------------------------------------------- #
# A page
# --------------------------------------------------------------------------- #


def page(
    session: Session,
    household: Household,
    *,
    currency: str | None = None,
    since: Date | None = None,
    until: Date | None = None,
    source: str | None = None,
    granularity: str = DAY,
    sort: str = "date",
    direction: str = "desc",
    limit: int = 500,
    offset: int = 0,
    at: Date | None = None,
    today: Date | None = None,
) -> Page:
    """One page of the list. ``at`` asks for the page holding that date.

    With ``at``, ``offset`` is ignored and the answer's own ``offset`` and
    ``focus`` say where the nearest row landed.
    """
    rates = fx.rates(session, household, today=today)
    wanted = {currency.upper()} if currency else set(fx_fetching.held_currencies(session, household))
    if granularity == DAY:
        rows, fill = _daily(
            session,
            household,
            rates,
            wanted=wanted,
            currency=currency,
            since=since,
            until=until,
            source=source,
        )
    else:
        rows = _months(
            rates, wanted=wanted, since=since, until=until, source=source, policy=FxPolicy(granularity)
        )
        fill = None
    if fill is not None and sort in _FILLED:
        for row in rows:
            fill(row)
        fill = None
    rows = _ordered(rows, sort, direction)

    focus: int | None = None
    if at is not None:
        target = at.replace(day=1) if granularity != DAY else at
        index = _nearest(rows, target)
        if index is not None:
            offset = index - index % limit
            focus = index - offset
        else:
            offset = 0
    shown = rows[offset : offset + limit]
    if fill is not None:
        for row in shown:
            fill(row)
    return Page(
        base=rates.base,
        granularity=granularity,
        total=len(rows),
        offset=offset,
        rows=shown,
        focus=focus,
    )
