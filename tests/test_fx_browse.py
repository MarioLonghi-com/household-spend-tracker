"""Browsing a long range of rates: a page at a time, by day or by month (#410).

Years of daily rates for two currencies, so a page boundary, a sort and a jump
all have something to get wrong. Every assertion is on the rows that come
back, not on a status code. The rates are made up by formula -- public-shaped
data, nobody's.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import Account, AccountType, BatchKind, FxManualRate, FxPeriod, FxPolicy, FxSource
from app.services import fx, fx_browse, fx_fetching, fx_sources
from app.services import transactions as txn_service
from tests.conftest import HEADERS, _setup_owner

#: The day the tests run "on".
TODAY = date(2026, 11, 4)
FIRST = date(2024, 1, 1)


def _weekdays(first: date, last: date) -> list[date]:
    days, day = [], first
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _rate(code: str, index: int) -> str:
    """A rate that moves every day and never repeats within a page."""
    start = {"GBP": Decimal("0.8"), "SEK": Decimal("10")}[code]
    return str(start + Decimal(index % 997) / Decimal(10_000))


def _publish_years(session) -> int:
    """Daily ECB rates for GBP and SEK from 2024 to TODAY. Returns how many."""
    published = [
        fx_sources.Published("EUR", code, day, _rate(code, i))
        for code in ("GBP", "SEK")
        for i, day in enumerate(_weekdays(FIRST, TODAY))
    ]
    fx_fetching.store(session, FxSource.ecb, published)
    session.commit()
    return len(published)


@pytest.fixture()
def held(session, household, owner):
    """Accounts in EUR (the base), GBP and SEK, so both foreign codes are held."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        made = {
            code: Account(
                household_id=household.id, name=f"{code} acct", type=AccountType.checking, currency=code
            )
            for code in ("EUR", "GBP", "SEK")
        }
        session.add_all(made.values())
    session.commit()
    return made


def _manual(session, household, owner, currency, day, rate, period=FxPeriod.day):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        session.add(
            FxManualRate(
                household_id=household.id,
                quote_currency=household.base_currency,
                currency=currency,
                date=day,
                period=period,
                rate=rate,
                created_by_id=owner.id,
            )
        )
    session.commit()


# --------------------------------------------------------------------------- #
# A page at a time
# --------------------------------------------------------------------------- #


def test_thousands_of_rates_come_back_one_page_at_a_time_quickly(session, household, held):
    stored = _publish_years(session)
    assert stored > 1400, "thousands of rates"

    started = time.perf_counter()
    first = fx_browse.page(session, household, limit=100, today=TODAY)
    took = time.perf_counter() - started

    assert first.total == stored
    assert len(first.rows) == 100
    # Newest first, both currencies of the newest day before the day before.
    assert [(r.date, r.currency) for r in first.rows[:3]] == [
        (TODAY, "SEK"),
        (TODAY, "GBP"),
        (TODAY - timedelta(days=1), "SEK"),
    ]
    assert first.rows[0].in_base == Decimal(first.rows[0].rate_text)
    assert took < 2, f"one page of {stored} rates took {took:.2f}s"

    second = fx_browse.page(session, household, limit=100, offset=100, today=TODAY)

    def keys(page):
        return {(r.date, r.currency, r.source) for r in page.rows}

    assert len(second.rows) == 100 and not keys(first) & keys(second)
    assert second.rows[0].date <= first.rows[-1].date

    last = fx_browse.page(session, household, limit=100, offset=stored - 5, today=TODAY)
    assert [(r.date, r.currency) for r in last.rows][-2:] == [(FIRST, "SEK"), (FIRST, "GBP")]


def test_filters_narrow_what_is_counted_not_only_what_is_shown(session, household, held):
    _publish_years(session)
    narrowed = fx_browse.page(
        session, household, currency="gbp", since=date(2026, 10, 1), until=date(2026, 10, 31), today=TODAY
    )
    assert narrowed.total == len(_weekdays(date(2026, 10, 1), date(2026, 10, 31)))
    assert {r.currency for r in narrowed.rows} == {"GBP"}
    assert min(r.date for r in narrowed.rows) == date(2026, 10, 1)
    assert max(r.date for r in narrowed.rows) == date(2026, 10, 30)


# --------------------------------------------------------------------------- #
# Every column sorts, through the server
# --------------------------------------------------------------------------- #


def test_every_column_sorts_and_blanks_sort_last_both_ways(session, household, owner, held):
    _publish_years(session)
    # A manual rate quoted against GBP has no base meaning on a day with no
    # euro rate for the pound: a blank in the "per 1 EUR" column.
    _manual(session, household, owner, "SEK", date(2026, 11, 7), "11.5")
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        session.add(
            FxManualRate(
                household_id=household.id,
                quote_currency="GBP",
                currency="SEK",
                date=date(2026, 11, 8),
                period=FxPeriod.day,
                rate="13",
                created_by_id=owner.id,
            )
        )
    session.commit()

    for direction in ("asc", "desc"):
        page = fx_browse.page(
            session, household, sort="in_base", direction=direction, limit=5000, today=TODAY
        )
        figures = [r.in_base for r in page.rows]
        assert figures[-1] is None, "the blank is last whichever way the column points"
        shown = [f for f in figures if f is not None]
        assert shown == sorted(shown, reverse=direction == "desc")

    cheapest = fx_browse.page(session, household, sort="in_base", direction="asc", limit=1, today=TODAY)
    assert cheapest.rows[0].in_base == Decimal("0.8")

    # "As published" keeps the quote currencies grouped in one order, and
    # turns only the figures round inside each group.
    for direction in ("asc", "desc"):
        page = fx_browse.page(session, household, sort="rate", direction=direction, limit=5000, today=TODAY)
        quotes = [r.quote_currency for r in page.rows]
        assert quotes == sorted(quotes), "EUR group then GBP group, both ways"
        euros = [r.rate for r in page.rows if r.quote_currency == "EUR"]
        assert euros == sorted(euros, reverse=direction == "desc")

    by_source = fx_browse.page(session, household, sort="source", direction="desc", limit=2, today=TODAY)
    assert [r.source for r in by_source.rows] == ["manual", "manual"]
    by_currency = fx_browse.page(session, household, sort="currency", direction="asc", limit=1, today=TODAY)
    assert by_currency.rows[0].currency == "GBP"
    oldest = fx_browse.page(session, household, sort="date", direction="asc", limit=1, today=TODAY)
    assert oldest.rows[0].date == FIRST


# --------------------------------------------------------------------------- #
# A month per line
# --------------------------------------------------------------------------- #


def test_a_month_per_line_is_what_the_rate_policy_gives_that_month(session, household, held):
    _publish_years(session)
    rates = fx.rates(session, household, today=TODAY)
    for policy in (FxPolicy.month_avg, FxPolicy.month_end):
        page = fx_browse.page(session, household, granularity=policy.value, limit=5000, today=TODAY)
        # 2024-01 to 2026-11 is 35 months, for two currencies.
        assert page.total == 70
        october = next(r for r in page.rows if r.currency == "GBP" and r.date == date(2026, 10, 1))
        used = rates.rate_on("GBP", date(2026, 10, 1), policy=policy)
        assert (october.rate, october.rate_date, october.source) == (used.rate, used.rate_date, "ecb")
        assert october.period is FxPeriod.month and october.quote_currency == "EUR"

    dailies = [
        Decimal(_rate("GBP", i))
        for i, day in enumerate(_weekdays(FIRST, TODAY))
        if day.month == 10 and day.year == 2026
    ]
    october = next(
        r
        for r in fx_browse.page(session, household, granularity="month_avg", limit=5000, today=TODAY).rows
        if r.currency == "GBP" and r.date == date(2026, 10, 1)
    )
    assert october.rate == sum(dailies) / len(dailies)
    end = next(
        r
        for r in fx_browse.page(session, household, granularity="month_end", limit=5000, today=TODAY).rows
        if r.currency == "GBP" and r.date == date(2026, 10, 1)
    )
    assert end.rate == dailies[-1] and end.rate_date == date(2026, 10, 30)


def test_a_month_list_filtered_to_one_source_reads_that_source_alone(session, household, owner, held):
    _publish_years(session)
    _manual(session, household, owner, "GBP", date(2026, 9, 1), "0.9", period=FxPeriod.month)
    chain = fx_browse.page(
        session, household, currency="GBP", granularity="month_avg", limit=5000, today=TODAY
    )
    september = {r.date: r for r in chain.rows}[date(2026, 9, 1)]
    assert (september.source, september.rate) == ("manual", Decimal("0.9")), "manual is first in the chain"

    ecb_only = fx_browse.page(
        session, household, currency="GBP", source="ecb", granularity="month_avg", limit=5000, today=TODAY
    )
    september = {r.date: r for r in ecb_only.rows}[date(2026, 9, 1)]
    assert september.source == "ecb" and september.rate != Decimal("0.9")

    manual_only = fx_browse.page(
        session, household, source="manual", granularity="month_avg", limit=5000, today=TODAY
    )
    assert [(r.currency, r.date) for r in manual_only.rows] == [("GBP", date(2026, 9, 1))]


def test_a_months_own_rate_comes_only_from_that_months_transfers(session, household, owner, held):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        txn_service.create_transfer(
            session,
            source=held["EUR"],
            destination=held["GBP"],
            date=date(2026, 10, 6),
            amount=10_000,
            to_amount=8_000,
        )
        txn_service.create_transfer(
            session,
            source=held["EUR"],
            destination=held["GBP"],
            date=date(2026, 10, 20),
            amount=10_000,
            to_amount=9_000,
        )
    session.commit()
    _publish_years(session)
    page = fx_browse.page(
        session, household, currency="GBP", granularity="month_avg", limit=5000, today=TODAY
    )
    by_month = {r.date: r.own for r in page.rows}
    assert by_month[date(2026, 10, 1)] == Decimal("0.85")
    assert by_month[date(2026, 11, 1)] is None, "no transfer in November: nothing borrowed from October"
    end = fx_browse.page(
        session, household, currency="GBP", granularity="month_end", limit=5000, today=TODAY
    )
    assert {r.date: r.own for r in end.rows}[date(2026, 10, 1)] == Decimal("0.9")


# --------------------------------------------------------------------------- #
# Jumping to a date
# --------------------------------------------------------------------------- #


def test_a_jump_answers_the_page_holding_the_nearest_row(session, household, held):
    _publish_years(session)
    page = fx_browse.page(session, household, limit=100, at=date(2025, 3, 15), today=TODAY)  # a Saturday
    assert page.offset % 100 == 0 and page.offset > 0
    landed = page.rows[page.focus]
    # Friday is one day away and Monday two: the first of Friday's rows.
    assert (landed.date, landed.currency) == (date(2025, 3, 14), "SEK")
    before = fx_browse.page(session, household, limit=100, offset=page.offset + page.focus - 1, today=TODAY)
    assert before.rows[0].date == date(2025, 3, 17), "the row above it is the Monday"

    months = fx_browse.page(
        session, household, granularity="month_end", limit=10, at=date(2024, 6, 20), today=TODAY
    )
    assert months.rows[months.focus].date == date(2024, 6, 1)

    nothing = fx_browse.page(session, household, currency="USD", at=date(2025, 1, 1), today=TODAY)
    assert (nothing.total, nothing.offset, nothing.focus) == (0, 0, None)


# --------------------------------------------------------------------------- #
# Over HTTP: the filters reach the server
# --------------------------------------------------------------------------- #


def test_the_route_pages_sorts_and_lists_months(client):
    _setup_owner(client)
    h = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()["id"]
    for name, code in (("Pounds", "GBP"), ("Kronor", "SEK")):
        client.post(
            f"/api/households/{h}/accounts",
            json={"name": name, "type": "checking", "currency": code},
            headers=HEADERS,
        )
    with Session(client.app_module.db_engine) as own:
        stored = _publish_years(own)

    url = f"/api/households/{h}/fx/rates"
    answer = client.get(url, params={"limit": 50, "offset": 50}).json()
    assert (answer["total"], answer["offset"], answer["limit"], len(answer["rates"])) == (
        stored,
        50,
        50,
        50,
    )
    assert answer["granularity"] == "day" and answer["focus"] is None

    sorted_asc = client.get(url, params={"sort": "date", "direction": "asc", "limit": 1}).json()
    assert sorted_asc["rates"][0]["date"] == FIRST.isoformat()

    months = client.get(
        url,
        params={
            "granularity": "month_end",
            "currency": "SEK",
            "since": "2026-01-01",
            "until": "2026-03-31",
        },
    ).json()
    assert [r["date"] for r in months["rates"]] == ["2026-03-01", "2026-02-01", "2026-01-01"]
    assert all(r["period"] == "month" and r["rate"] == r["rate_in_base"] for r in months["rates"])
    assert months["rates"][0]["rate_date"] == "2026-03-31"

    jumped = client.get(url, params={"limit": 100, "at": "2025-06-02", "currency": "GBP"}).json()
    assert jumped["rates"][jumped["focus"]]["date"] == "2025-06-02"

    assert client.get(url, params={"sort": "rate; drop"}).status_code == 422
    assert client.get(url, params={"granularity": "week"}).status_code == 422
    assert client.get(url, params={"offset": -1}).status_code == 422
