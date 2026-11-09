"""Fetch now over a long range: a year per request, stored as it arrives (#409).

A backfill over several years used to be one request to the ECB, which could
pass the size or time limit of one answer and then kept nothing. These tests
answer every request from a made-up ECB that publishes a rate on the first of
each month for whatever years are asked, and say which requests were made.
No test reaches the network.
"""

from __future__ import annotations

import re
import urllib.error
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.audit.batch import batch
from app.models import BatchKind, FxRate, HouseholdMember, Role, User, utcnow
from app.services import fx_fetching, fx_sources
from tests.conftest import HEADERS, _setup_owner

_ASKED = re.compile(r"D\.(?P<codes>[A-Z+]+)\.EUR\.SP00\.A\?startPeriod=(?P<since>[\d-]+)&endPeriod=(?P<until>[\d-]+)")


def _firsts(since: date, until: date) -> list[date]:
    out, cursor = [], date(since.year, since.month, 1)
    while cursor <= until:
        if cursor >= since:
            out.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return out


def _answer(url: str) -> bytes:
    """The ECB's CSV for the codes and dates in ``url``: a rate on each 1st."""
    asked = _ASKED.search(url)
    assert asked, url
    since, until = date.fromisoformat(asked["since"]), date.fromisoformat(asked["until"])
    lines = ["KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE"]
    for code, rate in (("GBP", "0.85"), ("SEK", "11.2")):
        if code in asked["codes"].split("+"):
            for day in _firsts(since, until):
                lines.append(f"EXR.D.{code}.EUR.SP00.A,D,{code},EUR,SP00,A,{day.isoformat()},{rate}")
    return ("\n".join(lines) + "\n").encode()


def _period(url: str) -> tuple[str, str]:
    asked = _ASKED.search(url)
    return asked["since"], asked["until"]


@pytest.fixture()
def asked(monkeypatch):
    """Every request a fetch makes, answered by the made-up ECB."""
    made: list[str] = []

    def answer(url: str) -> bytes:
        made.append(url)
        return _answer(url)

    monkeypatch.setattr(fx_sources, "_get", answer)
    return made


@pytest.fixture()
def world(client):
    """A EUR household with GBP and SEK accounts, the first rows in June 2021,
    and two members: Jane, who presses, and Bob, who only looks."""
    owner = _setup_owner(client)
    h = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()["id"]
    for name, currency in (("Checking", "EUR"), ("Pounds", "GBP"), ("Kronor", "SEK")):
        account = client.post(
            f"/api/households/{h}/accounts",
            json={"name": name, "type": "checking", "currency": currency},
            headers=HEADERS,
        ).json()
        for day in ("2021-06-15", "2024-02-03"):
            made = client.post(
                f"/api/households/{h}/transactions",
                json={"account_id": account["id"], "date": day, "amount": -1_000, "payee_name": "Shop"},
                headers=HEADERS,
            )
            assert made.status_code == 201, made.text

    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        jane_id = owner["user"]["id"]
        bob = User(
            email="bob@example.com", email_canonical="bob@example.com",
            display_name="Bob", password_hash="argon2-placeholder",
            role=Role.member, totp_secret=b"sealed-placeholder",
        )
        with batch(own, kind=BatchKind.setup, actor_id=jane_id, own_transaction=False):
            own.add(bob)
        own.commit()
        with batch(own, kind=BatchKind.admin, actor_id=jane_id, household_id=h):
            own.add(HouseholdMember(household_id=h, user_id=bob.id, added_by_id=jane_id, added_at=utcnow()))
        own.commit()
    return {"house": h, "bob": bob, "jane": client.get("/api/me").json()["display_name"]}


def _rates(client) -> list[tuple[str, date]]:
    with Session(client.app_module.db_engine) as s:
        return [(r.currency, r.date) for r in s.execute(select(FxRate).order_by(FxRate.currency, FxRate.date)).scalars()]


def _count(client) -> int:
    with Session(client.app_module.db_engine) as s:
        return s.execute(select(func.count()).select_from(FxRate)).scalar_one()


# --------------------------------------------------------------------------- #
# The chunks
# --------------------------------------------------------------------------- #


def test_a_range_is_cut_into_calendar_years_oldest_first():
    assert fx_fetching.chunks(date(2021, 6, 1), date(2024, 2, 10)) == [
        (date(2021, 6, 1), date(2021, 12, 31)),
        (date(2022, 1, 1), date(2022, 12, 31)),
        (date(2023, 1, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 2, 10)),
    ]
    assert fx_fetching.chunks(date(2026, 10, 30), date(2026, 11, 9)) == [(date(2026, 10, 30), date(2026, 11, 9))]
    assert fx_fetching.chunks(date(2025, 12, 31), date(2026, 1, 1)) == [
        (date(2025, 12, 31), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 1)),
    ]


# --------------------------------------------------------------------------- #
# Fetch now
# --------------------------------------------------------------------------- #


def test_a_range_of_several_years_is_fetched_a_year_per_request_and_stored(client, world, asked):
    h = world["house"]
    started = client.post(
        f"/api/households/{h}/fx/fetch",
        json={"mode": "range", "since": "2021-06-01", "until": "2024-02-10"},
        headers=HEADERS,
    )
    assert started.status_code == 202, started.text
    assert started.json()[0]["chunks_total"] == 4

    assert [_period(url) for url in asked] == [
        ("2021-06-01", "2021-12-31"),
        ("2022-01-01", "2022-12-31"),
        ("2023-01-01", "2023-12-31"),
        ("2024-01-01", "2024-02-10"),
    ]
    assert all("D.GBP+SEK.EUR" in url for url in asked), "each request still asks for every currency"
    # 7 + 12 + 12 + 2 firsts of the month, for each of two currencies.
    assert _count(client) == 2 * 33
    assert ("GBP", date(2021, 6, 1)) in _rates(client)
    assert ("SEK", date(2024, 2, 1)) in _rates(client)

    last = client.get(f"/api/households/{h}/fx/status").json()["history"][0]
    assert (last["outcome"], last["chunks_done"], last["chunks_total"]) == ("ok", 4, 4)
    assert (last["rates_added"], last["rates_unchanged"]) == (66, 0)
    assert (last["chunk_since"], last["chunk_until"]) == (None, None), "nothing in flight once it is done"


def test_a_failure_in_the_third_year_keeps_the_first_two_and_says_where_it_stopped(
    client, world, asked, monkeypatch
):
    h = world["house"]

    def third_fails(url: str) -> bytes:
        asked.append(url)
        if len(asked) == 3:
            raise urllib.error.URLError("no route to host")
        return _answer(url)

    monkeypatch.setattr(fx_sources, "_get", third_fails)
    answer = client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
    assert answer.status_code == 202
    assert answer.json()[0]["requested_since"] == "2021-06-15", "the first fetch starts at the earliest row"

    assert len(asked) == 3, "it stops at the failure rather than skipping the year"
    stored = _rates(client)
    assert {day.year for _, day in stored} == {2021, 2022}, "the first two years stay, the third is not half-kept"
    assert len(stored) == 2 * (6 + 12)

    status = client.get(f"/api/households/{h}/fx/status").json()
    row = status["history"][0]
    assert row["outcome"] == "failed"
    assert row["error"] == "could not reach data-api.ecb.europa.eu: no route to host"
    assert (row["chunks_done"], row["chunk_since"], row["chunk_until"]) == (2, "2023-01-01", "2023-12-31")
    assert row["chunks_total"] >= 6, "2021 to this year"
    assert (row["rates_added"], row["rates_unchanged"]) == (36, 0)
    assert status["running"] is False

    # The next press carries on from the last stored rate, not from 2021.
    monkeypatch.setattr(fx_sources, "_get", lambda url: asked.append(url) or _answer(url))
    again = client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
    assert again.json()[0]["requested_since"] == "2022-12-01"
    assert _period(asked[3]) == ("2022-12-01", "2022-12-31")
    assert ("GBP", date(2023, 6, 1)) in _rates(client)
    done = client.get(f"/api/households/{h}/fx/status").json()["history"][0]
    assert done["outcome"] == "ok"
    assert done["rates_unchanged"] == 2, "only the overlap day is fetched twice"


def test_a_second_member_sees_the_fetch_running_with_its_progress_and_cannot_start_another(
    client, world, monkeypatch
):
    h, bob = world["house"], world["bob"]
    seen: dict = {}
    calls: list[str] = []

    def as_bob_on_the_second_year(url: str) -> bytes:
        calls.append(url)
        if len(calls) == 2:
            # Jane's press is still running; Bob opens the page.
            client.app.dependency_overrides[current_user] = lambda: bob
            try:
                seen["status"] = client.get(f"/api/households/{h}/fx/status").json()
                seen["press"] = client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
            finally:
                client.app.dependency_overrides.pop(current_user)
        return _answer(url)

    monkeypatch.setattr(fx_sources, "_get", as_bob_on_the_second_year)
    answer = client.post(
        f"/api/households/{h}/fx/fetch",
        json={"mode": "range", "since": "2021-06-01", "until": "2023-03-31"},
        headers=HEADERS,
    )
    assert answer.status_code == 202

    status = seen["status"]
    assert status["running"] is True
    [running] = status["running_fetches"]
    assert running["outcome"] == "running"
    assert running["started_by"] == world["jane"], "Bob sees who started it"
    assert (running["chunks_done"], running["chunks_total"]) == (1, 3)
    assert (running["chunk_since"], running["chunk_until"]) == ("2022-01-01", "2022-12-31")
    assert running["rates_added"] == 14, "the first year is already stored"
    assert running["currencies"] == ["GBP", "SEK"]

    assert seen["press"].status_code == 409
    assert seen["press"].json()["code"] == "fx.fetch_running"
    assert len(calls) == 3, "Bob's press started nothing"

    after = client.get(f"/api/households/{h}/fx/status").json()
    assert after["running"] is False
    assert [row["outcome"] for row in after["history"]] == ["ok"]


def test_another_household_does_not_see_this_households_fetch(client, world, asked):
    """`running` is the instance's one slot; the fetch itself is this household's."""
    h = world["house"]
    client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
    assert len(client.get(f"/api/households/{h}/fx/status").json()["history"]) == 1
    second = client.post("/api/households", json={"name": "Elsewhere"}, headers=HEADERS).json()["id"]
    assert client.get(f"/api/households/{second}/fx/status").json()["history"] == []
