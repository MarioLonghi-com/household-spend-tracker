"""Net Worth: every account's balance at each month end, added up (#397).

The one report here that **may** add two currencies, and only through a rate.
Every other report answers one currency at a time because the ledger never
converts; this one exists to answer "how much does the household have, all
accounts together", which in a household with a EUR and a GBP account has no
answer without a rate. So the rule `insights.py` states -- **no response ever
sums two currencies** -- is kept here in the only way it can be:

- **One currency in scope:** everything is in that currency, exactly, and no
  rate is asked for. The report works with no rate source at all.
- **Several, and rates available:** each account is converted into the
  household's base currency *first*, at the rate on that date, and the
  converted values are added. A currency with no rate is left out of the total
  and listed under ``not_converted``; it is never treated as zero or as 1.
- **Several, and no rates:** no grand total. ``display_currency`` is null, the
  top-level figures are absent, and ``currencies`` carries each currency's own
  book side by side.

Conversion happens behind one seam, :func:`rates_for`, which answers "no
rates" until the exchange-rates service (#396) lands and is pointed at it.
Nothing converted is stored: a rate entered tomorrow changes the next answer,
and a change of base currency needs no migration.

**Balances are one query whatever the number of points**
(:func:`insights.monthly_movements`), read at each point as a running sum.
The arithmetic is :func:`insights.balances`' -- every row on or before the
date, opening balance included -- and the tests hold the two to each other.

**Liabilities subtract by their own sign.** A credit card or another
liability (``AccountType.is_liability``) owes money as a negative balance
already; nothing here flips a sign, and ``liabilities`` is the sum of those
values, shown for information and already inside the total.

A report is a question, not an act: nothing here writes.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import money
from ..errors import ValidationError
from ..models import Account, Household
from . import insights


class Split(enum.StrEnum):
    """What the trend chart's bars and the "Where it is held" table are cut by."""

    currency = "currency"
    country = "country"
    institution = "institution"
    type = "type"


#: The key an account is filed under when it does not say: no bank, no
#: country. The empty string rather than a word, so no bank's real name can
#: collide with it; a filter sends it the same way (`institution=`).
UNSET = ""

#: More month ends than this is a range nobody meant: a hundred years.
MAX_POINTS = 1200


# --------------------------------------------------------------------------- #
# The seam for exchange rates (#396)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RateUsed:
    """One rate: units of the currency per one unit of the base currency."""

    rate: Decimal
    #: The date the rate was published. Earlier than the date asked for over a
    #: weekend or a holiday: the last published rate on or before it.
    rate_date: Date
    source: str
    #: Older than the household's staleness limit. Still used, and flagged.
    stale: bool


class Rates(Protocol):
    """What this report needs from an exchange-rate source, and nothing more."""

    #: The sources tried in order, as the response's `rate_source` names them.
    chain: tuple[str, ...]
    #: Always "spot" here: a balance is converted at the rate on its date.
    policy: str

    def available(self) -> bool:
        """Whether a rate source is set up at all. False means "never convert"."""
        ...

    def rate_on(self, currency: str, on_date: Date) -> RateUsed | None:
        """The last rate on or before `on_date`, or None when there is none."""
        ...

    def status(self, currencies: list[str]) -> dict | None:
        """The "Rates as of" block for the currencies this answer converted."""
        ...


class NoRates:
    """No rate source: a report in one currency, or per-currency books."""

    chain: tuple[str, ...] = ()
    policy = "spot"

    def available(self) -> bool:
        return False

    def rate_on(self, currency: str, on_date: Date) -> RateUsed | None:
        return None

    def status(self, currencies: list[str]) -> dict | None:
        return None


NO_RATES = NoRates()


def rates_for(session: Session, household: Household) -> Rates:
    """The household's rates, for this one request.

    The one place the exchange-rates service plugs in (#396). Until it does,
    there is no rate anywhere in the schema and the answer is "none": a report
    in one currency still works, and one spanning several shows each
    currency's own figures and never a sum.
    """
    return NO_RATES


def convert(minor: int, currency: str, to_currency: str, used: RateUsed) -> int:
    """`minor` of `currency` in `to_currency`, at `used`: divided, then rounded once.

    In `Decimal` at each currency's own exponent, never a float, and rounded by
    `money.to_minor` -- the rule every entered amount already follows.
    """
    return money.to_minor(money.to_decimal(minor, currency) / used.rate, to_currency)


def rate_text(rate: Decimal) -> str:
    """A rate as the decimal string the money API sends. Never a float."""
    return format(rate, "f")


# --------------------------------------------------------------------------- #
# Which dates are points
# --------------------------------------------------------------------------- #


def month_end(day: Date) -> Date:
    following = day.replace(day=28) + timedelta(days=4)
    return following - timedelta(days=following.day)


def last_closed(today: Date) -> Date:
    """The last day of the last month that has ended by the calendar."""
    return today.replace(day=1) - timedelta(days=1)


def last_point(until: Date, *, today: Date, closed_only: bool) -> Date:
    """The date the answer stops at: its "as of"."""
    end = month_end(until)
    if closed_only:
        return min(end, last_closed(today))
    return end if end < today else today


@dataclass(frozen=True, slots=True)
class Window:
    points: list[Date]
    #: Today, when the closed-months rule cut the range short of where it ran.
    clipped_from: Date | None
    #: The last point is today, in a month still open.
    today_point: bool


def window(since: Date, until: Date, *, today: Date, closed_only: bool) -> Window:
    """The month ends in the range, clipped by the closed-months rule (§3.1, §5.1).

    The report is month-based: a range that starts or stops inside a month
    counts that month's end. **Closed months only** (the default) drops every
    month that has not ended by the calendar, and when that leaves nothing --
    "This month" -- shows the last closed month instead; ``clipped_from`` says
    the cut happened. Off, the running month's point is **today**, and months
    after it are not shown, because nothing has happened in them.
    """
    ends: list[Date] = []
    cursor, stop = month_end(since), month_end(until)
    while cursor <= stop:
        ends.append(cursor)
        cursor = month_end(cursor + timedelta(days=1))

    if closed_only:
        closed = last_closed(today)
        points = [end for end in ends if end <= closed]
        clipped = today if stop > closed else None
        return Window(points or [closed], clipped, False)

    points = [end for end in ends if end < today]
    reaches = stop >= today
    if reaches or not points:
        points.append(today)
    return Window(points, None, reaches or points == [today])


# --------------------------------------------------------------------------- #
# What the answer holds
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Point:
    date: Date
    #: This point is today rather than a month end (closed months off).
    today: bool
    net_worth: int
    liabilities: int
    #: The same values summed by the split. They add up to `net_worth` exactly.
    parts: dict[str, int]
    #: Accounts in scope with no row yet on or before this date. They count as
    #: zero, and the chart marks the bar: an early bar is missing imports, not
    #: poverty.
    accounts_without_data: int
    stale_rate: bool
    #: Currencies held on this date with no rate: left out of this total.
    missing_rates: list[str]


@dataclass(slots=True)
class Headline:
    net_worth: int
    liabilities: int
    change_prev: int | None
    prev_date: Date | None
    change_range: int | None
    range_start: Date


@dataclass(slots=True)
class Bridge:
    """From the start of the range to its end (§4.3, §5.5)."""

    start: int
    #: What the start balances are worth at the end rates, less what they were
    #: worth at the start rates. Zero in one currency.
    fx_effect: int
    money_added_or_spent: int
    end: int


@dataclass(slots=True)
class Held:
    """One part of the total at the "as of" date, cut by the split."""

    key: str
    #: In its own currency: only when the split is by currency.
    native_minor: int | None
    native_currency: str | None
    rate: str | None
    rate_date: Date | None
    value: int
    #: Of the total, as a decimal string to four places. Null on a total of 0.
    share: str | None


@dataclass(slots=True)
class Book:
    """Everything in one display currency: the page's figures, for one currency."""

    currency: str
    accounts: int
    points: list[Point]
    headline: Headline
    bridge: Bridge
    held: list[Held]


@dataclass(slots=True)
class AccountLine:
    id: str
    name: str
    institution: str | None
    country: str | None
    type: str
    is_liability: bool
    closed: bool
    currency: str
    native_minor: int
    native_change: int
    #: In the display currency. Null with no display currency or no rate.
    value: int | None
    change_range: int | None


@dataclass(slots=True)
class NotConverted:
    currency: str
    native_minor: int
    accounts: int


@dataclass(slots=True)
class RateLine:
    currency: str
    rate: str
    rate_date: Date
    source: str
    stale: bool
    #: Published before the "as of" date: the last rate before it (§4.6).
    before_as_of: bool


@dataclass(slots=True)
class LargestForeign:
    account_id: str
    account_name: str
    currency: str
    native_minor: int
    rate: str
    value: int


@dataclass(slots=True)
class Explanation:
    """What §4.6 says in words, as the figures on screen. None recomputed there."""

    accounts_counted: int
    accounts_hidden_closed: int
    accounts_without_data: int
    accounts_without_data_at_start: int
    currencies: list[str]
    rates: list[RateLine] = field(default_factory=list)
    largest_foreign: LargestForeign | None = None
    stale: bool = False
    missing: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RateSource:
    chain: list[str]
    policy: str


@dataclass(slots=True)
class NetWorth:
    as_of: Date
    since: Date
    until: Date
    today: Date
    closed_only: bool
    clipped_from: Date | None
    split: str
    base_currency: str
    #: What the top-level figures are in. Null when several currencies are in
    #: scope and nothing converts them: then there is no total to give.
    display_currency: str | None
    converted: bool
    rate_source: RateSource | None
    rates_status: dict | None
    points: list[Point]
    headline: Headline | None
    bridge: Bridge | None
    held: list[Held]
    accounts: list[AccountLine]
    not_converted: list[NotConverted]
    #: Each currency's own book, exact and unconverted. Always present.
    currencies: list[Book]
    explanation: Explanation


# --------------------------------------------------------------------------- #
# The arithmetic
# --------------------------------------------------------------------------- #


def _split_key(account: Account, split: Split) -> str:
    if split is Split.currency:
        return account.currency
    if split is Split.country:
        return account.country or UNSET
    if split is Split.institution:
        return (account.institution or "").strip() or UNSET
    return account.type.value


def _history(movements: dict[str, int], points: list[Date]) -> list[int | None]:
    """The balance at each point, from per-month movements. None: no row yet."""
    ordered = sorted(movements.items())
    answer: list[int | None] = []
    running, seen, at = 0, False, 0
    for point in points:
        label = f"{point.year:04d}-{point.month:02d}"
        while at < len(ordered) and ordered[at][0] <= label:
            running += ordered[at][1]
            seen = True
            at += 1
        answer.append(running if seen else None)
    return answer


class _Valuer:
    """Values an account's balance in one display currency, caching each rate."""

    def __init__(self, display: str, rates: Rates, points: list[Date]):
        self.display = display
        self.rates = rates
        self.points = points
        self._cache: dict[tuple[str, int], RateUsed | None] = {}

    def rate(self, currency: str, at: int) -> RateUsed | None:
        key = (currency, at)
        if key not in self._cache:
            self._cache[key] = self.rates.rate_on(currency, self.points[at])
        return self._cache[key]

    def value(self, currency: str, balance: int, at: int) -> tuple[int | None, RateUsed | None]:
        """(value, rate used). Value None: a foreign balance with no rate."""
        if currency == self.display or balance == 0:
            return balance, None
        used = self.rate(currency, at)
        if used is None:
            return None, None
        return convert(balance, currency, self.display, used), used


def _share(value: int, total: int) -> str | None:
    if total == 0:
        return None
    return str((Decimal(value) / Decimal(total)).quantize(Decimal("0.0001")))


def _book(
    accounts: list[Account],
    history: dict[str, list[int | None]],
    points: list[Date],
    today_point: bool,
    valuer: _Valuer,
    split: Split,
) -> Book:
    last = len(points) - 1
    made: list[Point] = []
    for at, day in enumerate(points):
        total = owed = without = 0
        parts: dict[str, int] = {}
        stale = False
        missing: set[str] = set()
        for account in accounts:
            balance = history[account.id][at]
            if balance is None:
                without += 1
                continue
            value, used = valuer.value(account.currency, balance, at)
            if value is None:
                missing.add(account.currency)
                continue
            stale = stale or bool(used and used.stale)
            total += value
            if account.type.is_liability:
                owed += value
            key = _split_key(account, split)
            parts[key] = parts.get(key, 0) + value
        made.append(
            Point(
                date=day,
                today=today_point and at == last,
                net_worth=total,
                liabilities=owed,
                parts=parts,
                accounts_without_data=without,
                stale_rate=stale,
                missing_rates=sorted(missing),
            )
        )

    first, end = made[0], made[-1]
    headline = Headline(
        net_worth=end.net_worth,
        liabilities=end.liabilities,
        change_prev=end.net_worth - made[-2].net_worth if len(made) > 1 else None,
        prev_date=made[-2].date if len(made) > 1 else None,
        change_range=end.net_worth - first.net_worth if len(made) > 1 else None,
        range_start=first.date,
    )

    # §5.5: the rate movement is credited to what was held at the start.
    fx_effect = 0
    for account in accounts:
        opening = history[account.id][0]
        if not opening or account.currency == valuer.display:
            continue
        at_start, at_end = valuer.rate(account.currency, 0), valuer.rate(account.currency, last)
        if at_start is None or at_end is None:
            continue
        fx_effect += convert(opening, account.currency, valuer.display, at_end) - convert(
            opening, account.currency, valuer.display, at_start
        )
    bridge = Bridge(
        start=first.net_worth,
        fx_effect=fx_effect,
        money_added_or_spent=end.net_worth - first.net_worth - fx_effect,
        end=end.net_worth,
    )

    held: dict[str, Held] = {}
    for account in accounts:
        balance = history[account.id][last]
        if balance is None:
            continue
        value, used = valuer.value(account.currency, balance, last)
        if value is None:
            continue
        key = _split_key(account, split)
        line = held.get(key)
        if line is None:
            line = held[key] = Held(
                key=key, native_minor=None, native_currency=None, rate=None,
                rate_date=None, value=0, share=None,
            )
            if split is Split.currency:
                line.native_minor, line.native_currency = 0, account.currency
                if account.currency != valuer.display:
                    rate = valuer.rate(account.currency, last)
                    if rate is not None:
                        line.rate, line.rate_date = rate_text(rate.rate), rate.rate_date
        line.value += value
        if line.native_minor is not None:
            line.native_minor += balance
    for line in held.values():
        line.share = _share(line.value, end.net_worth)

    return Book(
        currency=valuer.display,
        accounts=len(accounts),
        points=made,
        headline=headline,
        bridge=bridge,
        held=sorted(held.values(), key=lambda one: (-one.value, one.key)),
    )


@dataclass(frozen=True, slots=True)
class Filters:
    """None is "all". Choices inside one widen (OR); filters narrow (AND)."""

    institutions: list[str] | None = None
    countries: list[str] | None = None
    currencies: list[str] | None = None
    account_ids: list[str] | None = None

    def admits(self, account: Account) -> bool:
        bank = (account.institution or "").strip() or UNSET
        if self.institutions is not None and bank not in self.institutions:
            return False
        if self.countries is not None and (account.country or UNSET) not in self.countries:
            return False
        if self.currencies is not None and account.currency not in self.currencies:
            return False
        return self.account_ids is None or account.id in self.account_ids


def report(
    session: Session,
    household: Household,
    *,
    since: Date | None = None,
    until: Date | None = None,
    closed_only: bool = True,
    filters: Filters | None = None,
    include_closed_accounts: bool = False,
    split: Split = Split.currency,
    today: Date | None = None,
    rates: Rates | None = None,
) -> NetWorth:
    """The Net Worth report (#397).

    `since` and `until` may each be omitted, which is what "All dates" sends:
    an omitted start is the first month any account in scope has a row, an
    omitted end is today -- and the closed-months rule then applies as usual.
    `today` and `rates` are for tests; a request takes the calendar's and the
    household's.
    """
    today = today or Date.today()
    filters = filters or Filters()
    if since and until and since > until:
        raise ValidationError(
            "the date range ends before it starts",
            code="report.range_backwards",
            params={"since": since.isoformat(), "until": until.isoformat()},
        )
    rates = rates if rates is not None else rates_for(session, household)
    until = until or today

    everyone = (
        session.execute(
            select(Account)
            .where(Account.household_id == household.id)
            .order_by(Account.sort_order, Account.name)
        )
        .scalars()
        .all()
    )
    accounts = [one for one in everyone if filters.admits(one)]

    cap = last_point(until, today=today, closed_only=closed_only)
    movements = (
        insights.monthly_movements(
            session, household.id, until=cap, account_ids=[one.id for one in accounts]
        )
        if accounts
        else {}
    )
    if since is None:
        months = [month for moves in movements.values() for month in moves]
        since = (
            Date(int(min(months)[:4]), int(min(months)[5:7]), 1)
            if months
            else until.replace(day=1)
        )
    if since > until:
        since = until.replace(day=1)

    span = window(since, until, today=today, closed_only=closed_only)
    if len(span.points) > MAX_POINTS:
        raise ValidationError(
            f"that range has {len(span.points)} month ends; the most is {MAX_POINTS}",
            code="report.range_too_long",
            params={"months": len(span.points), "limit": MAX_POINTS},
        )
    points = span.points
    last = len(points) - 1
    history = {one.id: _history(movements.get(one.id, {}), points) for one in accounts}

    in_scope = sorted({one.currency for one in accounts})
    native = {
        code: _book(
            [one for one in accounts if one.currency == code],
            history, points, span.today_point,
            _Valuer(code, NO_RATES, points), split,
        )
        for code in in_scope
    }

    main: Book | None = None
    valuer: _Valuer | None = None
    converted = False
    if len(in_scope) == 1:
        main = native[in_scope[0]]
        valuer = _Valuer(in_scope[0], NO_RATES, points)
    elif in_scope and rates.available():
        valuer = _Valuer(household.base_currency, rates, points)
        main = _book(accounts, history, points, span.today_point, valuer, split)
        converted = True

    # The accounts table: one row per account with a balance on the "as of"
    # date. A closed one is left out unless asked for; its past still counts.
    lines: list[AccountLine] = []
    hidden_closed = 0
    for account in accounts:
        balance = history[account.id][last]
        if balance is None:
            continue
        if account.closed and not include_closed_accounts:
            hidden_closed += 1
            continue
        opening = history[account.id][0] or 0
        value = change = None
        if valuer is not None:
            value, _ = valuer.value(account.currency, balance, last)
            start, _ = valuer.value(account.currency, opening, 0)
            if value is not None and start is not None:
                change = value - start
        lines.append(
            AccountLine(
                id=account.id,
                name=account.name,
                institution=account.institution,
                country=account.country,
                type=account.type.value,
                is_liability=account.type.is_liability,
                closed=account.closed,
                currency=account.currency,
                native_minor=balance,
                native_change=balance - opening,
                value=value,
                change_range=change,
            )
        )
    if main is not None:
        lines.sort(key=lambda one: (-(one.value or 0), one.name))
    else:
        lines.sort(key=lambda one: (one.currency, -one.native_minor, one.name))

    not_converted: list[NotConverted] = []
    explanation = Explanation(
        accounts_counted=len(accounts),
        accounts_hidden_closed=hidden_closed,
        accounts_without_data=sum(1 for one in accounts if history[one.id][last] is None),
        accounts_without_data_at_start=sum(1 for one in accounts if history[one.id][0] is None),
        currencies=in_scope,
    )
    rates_status = None
    if converted and valuer is not None and main is not None:
        foreign = [code for code in in_scope if code != valuer.display]
        for code in foreign:
            used = valuer.rate(code, last)
            if used is None:
                held = [one for one in accounts if one.currency == code]
                not_converted.append(
                    NotConverted(
                        currency=code,
                        native_minor=sum(history[one.id][last] or 0 for one in held),
                        accounts=len(held),
                    )
                )
                continue
            explanation.rates.append(
                RateLine(
                    currency=code,
                    rate=rate_text(used.rate),
                    rate_date=used.rate_date,
                    source=used.source,
                    stale=used.stale,
                    before_as_of=used.rate_date < points[last],
                )
            )
        explanation.stale = any(one.stale for one in explanation.rates)
        explanation.missing = [one.currency for one in not_converted]
        largest = max(
            (
                one
                for one in lines
                if one.currency != valuer.display and one.value is not None and one.native_minor
            ),
            key=lambda one: abs(one.value or 0),
            default=None,
        )
        if largest is not None:
            used = valuer.rate(largest.currency, last)
            assert used is not None  # a value means a rate was found
            explanation.largest_foreign = LargestForeign(
                account_id=largest.id,
                account_name=largest.name,
                currency=largest.currency,
                native_minor=largest.native_minor,
                rate=rate_text(used.rate),
                value=largest.value or 0,
            )
        rates_status = rates.status([one.currency for one in explanation.rates])

    return NetWorth(
        as_of=points[last],
        since=since,
        until=until,
        today=today,
        closed_only=closed_only,
        clipped_from=span.clipped_from,
        split=split.value,
        base_currency=household.base_currency,
        display_currency=main.currency if main else None,
        converted=converted,
        rate_source=RateSource(chain=list(rates.chain), policy=rates.policy) if converted else None,
        rates_status=rates_status,
        points=main.points if main else [],
        headline=main.headline if main else None,
        bridge=main.bridge if main else None,
        held=main.held if main else [],
        accounts=lines,
        not_converted=not_converted,
        currencies=list(native.values()),
        explanation=explanation,
    )
