"""The one place an amount is converted from one currency into another (#396 §6).

**The ledger never converts.** Amounts are stored in their account's currency
and stay there. A *report* may show a figure in the household's base currency,
and when it does, the conversion happens here and nowhere else: no other
module divides an amount by a rate (`tests/test_fx_service.py` greps for it).

Public API -- small on purpose, because reports are built on it:

``convert(session, household, amount_minor, currency, on_date, *, policy=None)``
    Minor units of ``currency`` on ``on_date``, in minor units of the
    household's base currency, as a :class:`Converted` -- the amount and the
    :class:`RateUsed` -- or ``None`` when no source in the chain has a rate.
    **Callers must handle ``None``**: show the amount unconverted, list it
    under "not converted", leave it out of totals. Never zero, never 1.

``rate_on(session, household, currency, on_date, *, policy=None)``
    The :class:`RateUsed` alone, or ``None``.

``rates(session, household)``
    A :class:`Rates`: the same two calls as methods, with the rates read once
    and cached by (currency, date, policy) for the life of the object. One per
    request; never kept between requests, so a manual rate entered a moment
    ago is in the next report.

``rates_status(session, household, currencies, *, rates=None)``
    The ``rates_status`` block every converting response carries
    (:class:`RatesStatus`; `schemas.RatesStatusOut` validates from it): how old
    the rates for those currencies are and when someone last fetched them.
    The "Rates as of" line is drawn from it, in the same response as the
    figures, so the two can never disagree.

``policy`` is an :class:`~app.models.FxPolicy`, or ``None`` for the
household's own monthly setting. A rate is quoted as **units of the currency
per one unit of the base**, the ECB's convention; converting divides by it.

How a rate is found:

- **The chain.** The household's sources in order (`Household.fx_chain`).
  The first with a rate within the stale limit wins. If none has one, the
  newest rate any of them has is used and marked ``stale``. If none has any,
  ``None``.
- **Cross rates.** A source quoting against another currency is crossed
  through it on the same date: rate(C per base) = rate(C per EUR) / rate(base
  per EUR). ``via`` names the pivot.
- **Rounding.** ``Decimal`` throughout, and the one rounding is
  `money.to_minor`, half-up at the base currency's exponent -- the rule every
  entered amount already follows. Nothing intermediate is rounded.
"""

from __future__ import annotations

import bisect
import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import money
from ..models import (
    FxFetch,
    FxFetchOutcome,
    FxManualRate,
    FxPeriod,
    FxPolicy,
    FxRate,
    FxSource,
    Household,
    User,
)

#: What a new household's chain is: rates entered by hand, then the ECB.
DEFAULT_CHAIN: tuple[FxSource, ...] = (FxSource.manual, FxSource.ecb)

#: What `RateUsed.source` says when nothing needed converting.
BASE = "base"

#: Significant digits a rate is *shown* with. Display only: the conversion
#: uses the full figure.
SHOWN_DIGITS = 10


# --------------------------------------------------------------------------- #
# What a conversion says about itself
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RateUsed:
    """The rate a figure was converted at, and where it came from."""

    #: Units of the converted currency per one unit of the base currency.
    rate: Decimal
    #: The rate's own date: the publication day, or the first day of the
    #: month for a monthly rate. Not the date of the figure.
    rate_date: Date
    #: An `FxSource` value, or ``"base"`` for an amount already in the base.
    source: str
    #: Older than the household's stale limit. A flag, never a block.
    stale: bool
    #: The currency crossed through (``"EUR"`` for the ECB and a non-euro
    #: base), or ``None`` when the source quotes against the base itself.
    via: str | None = None

    @property
    def rate_text(self) -> str:
        """The rate as a person reads it: at most ten significant digits."""
        return rate_text(self.rate)


@dataclass(frozen=True, slots=True)
class Converted:
    """An amount in the base currency, and the rate that made it."""

    amount_minor: int
    rate_used: RateUsed


def rate_text(rate: Decimal) -> str:
    """A rate as a plain decimal string, at most ten significant digits."""
    if rate == 0:
        return "0"
    exponent = rate.adjusted()
    places = max(SHOWN_DIGITS - 1 - exponent, 0)
    shown = rate.quantize(Decimal(1).scaleb(-places)) if places else rate.to_integral_value()
    text = format(shown, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def parse_rate(text: str) -> Decimal | None:
    """A stored rate as a ``Decimal``, or ``None`` for one that is not positive."""
    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def chain_of(household: Household) -> list[FxSource]:
    """The household's sources, in order. Unknown names are skipped, not guessed."""
    out: list[FxSource] = []
    for name in (household.fx_chain or "").split(","):
        name = name.strip()
        if name in FxSource.__members__ and FxSource(name) not in out:
            out.append(FxSource(name))
    return out


def monthly_policy_of(household: Household) -> FxPolicy:
    try:
        return FxPolicy(household.fx_monthly_policy)
    except ValueError:
        return FxPolicy.month_avg


# --------------------------------------------------------------------------- #
# The rates of one source, as units of a currency per one base
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Point:
    date: Date
    rate: Decimal
    via: str | None = None


@dataclass(slots=True)
class _Series:
    """One source's rates for one currency, already in base terms."""

    days: list[_Point] = field(default_factory=list)
    #: Monthly rates (manual only), by the first day of their month.
    months: dict[Date, _Point] = field(default_factory=dict)
    _dates: list[Date] = field(default_factory=list)

    def seal(self) -> _Series:
        self.days.sort(key=lambda p: p.date)
        self._dates = [p.date for p in self.days]
        return self

    def __bool__(self) -> bool:
        return bool(self.days or self.months)

    def last_on_or_before(self, day: Date) -> _Point | None:
        i = bisect.bisect_right(self._dates, day)
        return self.days[i - 1] if i else None

    def within(self, first: Date, last: Date) -> list[_Point]:
        lo = bisect.bisect_left(self._dates, first)
        hi = bisect.bisect_right(self._dates, last)
        return self.days[lo:hi]


def _month_start(day: Date) -> Date:
    return day.replace(day=1)


def _month_end(day: Date) -> Date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _cross(
    table: dict[tuple[str, str], dict[tuple[Date, FxPeriod], Decimal]], currency: str, base: str
) -> dict[tuple[Date, FxPeriod], _Point]:
    """``currency`` per one ``base``, from rows quoted against anything.

    Direct rows win, then the inverse of a row quoted the other way round,
    then a cross through a third currency on the same date and period.
    """
    out: dict[tuple[Date, FxPeriod], _Point] = {}
    pivots = {quote for quote, _ in table if quote not in (base, currency)}
    for pivot in sorted(pivots):
        c_rows = table.get((pivot, currency), {})
        b_rows = table.get((pivot, base), {})
        for key, b_rate in b_rows.items():
            c_rate = c_rows.get(key)
            if c_rate is not None:
                out.setdefault(key, _Point(key[0], c_rate / b_rate, via=pivot))
    # The pivot *is* the currency: units of it per base is one over base per it.
    for key, rate in table.get((currency, base), {}).items():
        out[key] = _Point(key[0], Decimal(1) / rate)
    for key, rate in table.get((base, currency), {}).items():
        out[key] = _Point(key[0], rate)
    return out


def _series_from(points: dict[tuple[Date, FxPeriod], _Point]) -> _Series:
    series = _Series()
    for (_, period), point in points.items():
        if period is FxPeriod.month:
            series.months[_month_start(point.date)] = point
        else:
            series.days.append(point)
    return series.seal()


@dataclass(frozen=True, slots=True)
class _Found:
    point: _Point
    #: The day the rate counts as current on, for its age.
    effective: Date
    #: The day the figure is "as of": its own date, capped at today.
    reference: Date


# --------------------------------------------------------------------------- #
# One request's rates
# --------------------------------------------------------------------------- #


class Rates:
    """A household's rates for the life of one request.

    Every table is read at most once per currency and source, and every answer
    is cached by (currency, date, policy). Build one per request with
    :func:`rates`; never keep one across requests.
    """

    def __init__(self, session: Session, household: Household, *, today: Date | None = None) -> None:
        self.session = session
        self.household = household
        self.base = household.base_currency.upper()
        self.chain = chain_of(household)
        self.stale_days = int(household.fx_stale_days)
        self.monthly_policy = monthly_policy_of(household)
        self.show_own_rates = bool(household.fx_show_own_rates)
        self.today = today or Date.today()
        self._series: dict[tuple[FxSource, str], _Series] = {}
        self._tables: dict[FxSource, dict[tuple[str, str], dict[tuple[Date, FxPeriod], Decimal]]] = {}
        self._own: dict[str, _Series] | None = None
        self._answers: dict[tuple[str, Date, FxPolicy], RateUsed | None] = {}

    # -- reading -------------------------------------------------------------

    def _table(self, source: FxSource, currencies: set[str]) -> dict:
        """Raw rows of one stored source touching ``currencies``, by (quote, currency)."""
        table = self._tables.setdefault(source, defaultdict(dict))
        wanted = {c for c in currencies if not any(c == cur for _, cur in table)}
        if not wanted:
            return table
        if source is FxSource.manual:
            rows = self.session.execute(
                select(
                    FxManualRate.quote_currency,
                    FxManualRate.currency,
                    FxManualRate.date,
                    FxManualRate.period,
                    FxManualRate.rate,
                ).where(
                    FxManualRate.household_id == self.household.id,
                    FxManualRate.currency.in_(wanted),
                )
            ).all()
        else:
            rows = [
                (quote, cur, day, FxPeriod.day, rate)
                for quote, cur, day, rate in self.session.execute(
                    select(FxRate.quote_currency, FxRate.currency, FxRate.date, FxRate.rate).where(
                        FxRate.source == source, FxRate.currency.in_(wanted)
                    )
                ).all()
            ]
        for quote, cur, day, period, text in rows:
            value = parse_rate(text)
            if value is not None:
                table[(quote, cur)][(day, FxPeriod(period))] = value
        for cur in wanted:
            # Marks the currency as read, rows or none, so it is not asked again.
            table.setdefault(("", cur), {})
        return table

    def _own_series(self, currency: str) -> _Series:
        """The household's own transfers, as units of ``currency`` per one base.

        `review.observed_rates` quotes each pair as destination units per
        source unit, whichever way the money went. A transfer out of the base
        is taken as it is; one into the base is inverted; one with neither leg
        in the base says nothing about this rate and is left out. Several on
        one day are averaged.
        """
        if self._own is None:
            from .review import observed_rates

            by_day: dict[str, dict[Date, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
            for seen in observed_rates(self.session, self.household.id):
                value = parse_rate(seen.rate)
                if value is None:
                    continue
                if seen.from_currency == self.base and seen.to_currency != self.base:
                    by_day[seen.to_currency][seen.date].append(value)
                elif seen.to_currency == self.base and seen.from_currency != self.base:
                    by_day[seen.from_currency][seen.date].append(Decimal(1) / value)
            self._own = {}
            for cur, days in by_day.items():
                series = _Series(
                    days=[_Point(day, sum(values) / len(values)) for day, values in days.items()]
                )
                self._own[cur] = series.seal()
        return self._own.get(currency, _Series())

    def series(self, source: FxSource, currency: str) -> _Series:
        """One source's rates for ``currency``, as units per one base."""
        currency = currency.upper()
        key = (source, currency)
        if key not in self._series:
            if source is FxSource.own:
                self._series[key] = self._own_series(currency)
            else:
                table = self._table(source, {currency, self.base})
                self._series[key] = _series_from(_cross(table, currency, self.base))
        return self._series[key]

    # -- finding -------------------------------------------------------------

    def _spot(self, series: _Series, day: Date) -> _Found | None:
        reference = min(day, self.today)
        best: _Found | None = None
        point = series.last_on_or_before(day)
        if point is not None:
            best = _Found(point, point.date, reference)
        month = series.months.get(_month_start(day))
        if month is None:
            earlier = [start for start in series.months if start <= day]
            month = series.months[max(earlier)] if earlier else None
        if month is not None:
            effective = min(_month_end(month.date), day)
            if best is None or effective > best.effective:
                best = _Found(month, effective, reference)
        return best

    def _find(self, series: _Series, day: Date, policy: FxPolicy) -> _Found | None:
        if policy is FxPolicy.spot:
            return self._spot(series, day)
        end = _month_end(day)
        if policy is FxPolicy.month_end:
            found = self._spot(series, end)
            return found and _Found(found.point, found.effective, min(end, self.today))
        # month_avg: a monthly rate is the month's figure; else the mean of
        # the month's daily rates; else, for a month with none, its last rate.
        start = _month_start(day)
        reference = min(end, self.today)
        month = series.months.get(start)
        if month is not None:
            return _Found(month, reference, reference)
        dailies = series.within(start, end)
        if dailies:
            mean = sum((p.rate for p in dailies), Decimal(0)) / len(dailies)
            vias = {p.via for p in dailies}
            last = dailies[-1]
            return _Found(_Point(last.date, mean, vias.pop() if len(vias) == 1 else None), last.date, reference)
        found = self._spot(series, end)
        return found and _Found(found.point, found.effective, reference)

    def _used(self, source: FxSource, found: _Found) -> RateUsed:
        stale = (found.reference - found.effective).days > self.stale_days
        return RateUsed(
            rate=found.point.rate,
            rate_date=found.point.date,
            source=source.value,
            stale=stale,
            via=found.point.via,
        )

    def rate_on(self, currency: str, on_date: Date, *, policy: FxPolicy | None = None) -> RateUsed | None:
        """The rate ``currency`` converts at on ``on_date``, or ``None``."""
        currency = currency.upper()
        policy = FxPolicy(policy) if policy is not None else self.monthly_policy
        if currency == self.base:
            return RateUsed(rate=Decimal(1), rate_date=on_date, source=BASE, stale=False)
        key = (currency, on_date, policy)
        if key in self._answers:
            return self._answers[key]
        fallback: RateUsed | None = None
        answer: RateUsed | None = None
        for source in self.chain:
            found = self._find(self.series(source, currency), on_date, policy)
            if found is None:
                continue
            used = self._used(source, found)
            if not used.stale:
                answer = used
                break
            if fallback is None or used.rate_date > fallback.rate_date:
                fallback = used
        answer = answer or fallback
        self._answers[key] = answer
        return answer

    def convert(
        self, amount_minor: int, currency: str, on_date: Date, *, policy: FxPolicy | None = None
    ) -> Converted | None:
        """``amount_minor`` of ``currency`` in minor units of the base, or ``None``."""
        used = self.rate_on(currency, on_date, policy=policy)
        if used is None:
            return None
        if used.source == BASE:
            return Converted(amount_minor=amount_minor, rate_used=used)
        value = money.to_decimal(amount_minor, currency) / used.rate
        return Converted(amount_minor=money.to_minor(value, self.base), rate_used=used)

    def latest(self, currency: str) -> RateUsed | None:
        """The newest rate the chain would use today: what "as of" is read from."""
        return self.rate_on(currency, self.today, policy=FxPolicy.spot)

    def own_rate(self, currency: str, on_date: Date) -> Decimal | None:
        """The household's own rate on that day, normalised to units per base.

        For showing beside the rate in use (#396 §2 D), never for converting
        unless the chain holds ``own``.
        """
        if currency.upper() == self.base:
            return None
        for point in self._own_series(currency.upper()).within(on_date, on_date):
            return point.rate
        return None

    def rate_from(
        self, source: FxSource, currency: str, on_date: Date, *, policy: FxPolicy | None = None
    ) -> RateUsed | None:
        """What one source alone gives ``currency`` on ``on_date`` under ``policy``.

        The same finding `rate_on` does for each source in the chain, without
        the chain: for a list filtered to one source (#410). Never converts.
        """
        currency = currency.upper()
        if currency == self.base:
            return None
        policy = FxPolicy(policy) if policy is not None else self.monthly_policy
        found = self._find(self.series(FxSource(source), currency), on_date, policy)
        return self._used(FxSource(source), found) if found is not None else None

    def own_rate_in_month(self, currency: str, month: Date, policy: FxPolicy) -> Decimal | None:
        """The household's own rate for ``month`` under ``policy``, or ``None``.

        Only from transfers inside that month: a month with none says nothing,
        rather than borrowing an earlier month's rate the way a conversion may.
        """
        if currency.upper() == self.base:
            return None
        found = self._find(self._own_series(currency.upper()), month, FxPolicy(policy))
        if found is None or not (_month_start(month) <= found.point.date <= _month_end(month)):
            return None
        return found.point.rate


def rates(session: Session, household: Household, *, today: Date | None = None) -> Rates:
    """The household's rates for one request. See :class:`Rates`."""
    return Rates(session, household, today=today)


def rate_on(
    session: Session,
    household: Household,
    currency: str,
    on_date: Date,
    *,
    policy: FxPolicy | None = None,
) -> RateUsed | None:
    """The rate ``currency`` converts into the base at on ``on_date``, or ``None``."""
    return rates(session, household).rate_on(currency, on_date, policy=policy)


def convert(
    session: Session,
    household: Household,
    amount_minor: int,
    currency: str,
    on_date: Date,
    *,
    policy: FxPolicy | None = None,
) -> Converted | None:
    """Minor units of ``currency`` as minor units of the base, or ``None``.

    For more than one figure in a request, build :func:`rates` once and call
    its ``convert``: the rates are read once and the answers cached.
    """
    return rates(session, household).convert(amount_minor, currency, on_date, policy=policy)


# --------------------------------------------------------------------------- #
# How old the rates are: the `rates_status` block
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CurrencyStatus:
    currency: str
    #: The newest rate the chain would use today, by its own date.
    rate_date: Date | None
    source: str | None
    stale: bool
    #: Days from the rate's date to today.
    age_days: int | None


@dataclass(frozen=True, slots=True)
class FailedFetch:
    at: datetime
    error: str


@dataclass(frozen=True, slots=True)
class RatesStatus:
    """What the "Rates as of" line says, for the currencies a response converted.

    ``as_of`` is the oldest of ``per_currency``'s rate dates: the weakest link
    sets the date. ``per_currency`` holds the currencies that have a rate;
    ``not_converted`` the ones that have none.
    """

    as_of: Date | None
    stale: bool
    #: A fetch is running on this instance now.
    running: bool
    #: This household's last finished fetch, and who started it.
    fetched_at: datetime | None
    fetched_by: str | None
    #: When this household's last finished fetch failed, and why.
    last_fetch_failed: FailedFetch | None
    chain: list[str]
    stale_days: int
    #: A source in the chain is fetched from outside, so "fetch them" applies.
    remote_in_chain: bool
    #: The last change to this household's manual rates, and by whom.
    manual_changed_at: datetime | None
    manual_changed_by: str | None
    per_currency: list[CurrencyStatus]
    not_converted: list[str]


def _name(session: Session, user_id: str | None) -> str | None:
    if not user_id:
        return None
    user = session.get(User, user_id)
    return user.display_name if user is not None else None


def rates_status(
    session: Session,
    household: Household,
    currencies,
    *,
    rates: Rates | None = None,
) -> RatesStatus:
    """The ``rates_status`` block for the currencies a response converted.

    ``currencies`` is any iterable of codes; the base currency is dropped, so
    a report that showed only the base gets an empty ``per_currency`` and the
    line hides itself.
    """
    rates = rates or Rates(session, household)
    wanted = sorted({c.upper() for c in currencies} - {rates.base})
    per_currency: list[CurrencyStatus] = []
    missing: list[str] = []
    for code in wanted:
        used = rates.latest(code)
        if used is None:
            missing.append(code)
            continue
        per_currency.append(
            CurrencyStatus(
                currency=code,
                rate_date=used.rate_date,
                source=used.source,
                stale=used.stale,
                age_days=(rates.today - used.rate_date).days,
            )
        )

    finished = session.execute(
        select(FxFetch)
        .where(FxFetch.household_id == household.id, FxFetch.outcome != FxFetchOutcome.running)
        .order_by(FxFetch.finished_at.desc(), FxFetch.started_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    running = session.execute(
        select(func.count()).select_from(FxFetch).where(FxFetch.outcome == FxFetchOutcome.running)
    ).scalar_one() > 0

    from .fx_manual import last_change

    changed_at, changed_by = last_change(session, household.id)
    return RatesStatus(
        as_of=min((c.rate_date for c in per_currency if c.rate_date), default=None),
        stale=any(c.stale for c in per_currency),
        running=running,
        fetched_at=finished.finished_at if finished else None,
        fetched_by=_name(session, finished.started_by_id) if finished else None,
        last_fetch_failed=(
            FailedFetch(at=finished.finished_at or finished.started_at, error=finished.error or "")
            if finished is not None and finished.outcome is FxFetchOutcome.failed
            else None
        ),
        chain=[s.value for s in rates.chain],
        stale_days=rates.stale_days,
        remote_in_chain=any(s.is_remote for s in rates.chain),
        manual_changed_at=changed_at,
        manual_changed_by=_name(session, changed_by),
        per_currency=per_currency,
        not_converted=missing,
    )
