"""Fetch now always ends, and stops asking for what nobody publishes (#444).

Two halves. A fetch row used to stay `running` until the next restart when a
commit in the background work hit "database is locked", or when the request
failed after writing the row; every later press then answered 409. And the
missing mode asked the ECB, on every press, for currencies it has never
published or stopped publishing years ago, so the start date never moved.

The made-up ECB here publishes GBP and SEK on the first of each month, and
RUB until its last published day, 1 March 2022. No test reaches the network.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.error
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import FxFetch, FxFetchOutcome, FxRate
from app.services import fx_dashboard, fx_fetching, fx_sources
from tests.conftest import HEADERS, _setup_owner

_ASKED = re.compile(r"D\.(?P<codes>[A-Z+]+)\.EUR\.SP00\.A\?startPeriod=(?P<since>[\d-]+)&endPeriod=(?P<until>[\d-]+)")
_RUB_LAST = date(2022, 3, 1)
_RUN = fx_fetching.run
_FETCH_VIEW = fx_dashboard.fetch_view


def _firsts(since: date, until: date) -> list[date]:
    out, cursor = [], date(since.year, since.month, 1)
    while cursor <= until:
        if cursor >= since:
            out.append(cursor)
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return out


def _answer(url: str) -> bytes:
    asked = _ASKED.search(url)
    assert asked, url
    since, until = date.fromisoformat(asked["since"]), date.fromisoformat(asked["until"])
    lines = ["KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE"]
    for code, rate, last in (("GBP", "0.85", until), ("SEK", "11.2", until), ("RUB", "90.5", _RUB_LAST)):
        if code in asked["codes"].split("+"):
            for day in _firsts(since, min(until, last)):
                lines.append(f"EXR.D.{code}.EUR.SP00.A,D,{code},EUR,SP00,A,{day.isoformat()},{rate}")
    return ("\n".join(lines) + "\n").encode()


def _codes(url: str) -> list[str]:
    return _ASKED.search(url)["codes"].split("+")


@pytest.fixture(autouse=True)
def asked(monkeypatch):
    made: list[str] = []

    def answer(url: str) -> bytes:
        made.append(url)
        return _answer(url)

    monkeypatch.setattr(fx_sources, "_get", answer)
    monkeypatch.setattr(fx_fetching, "FAIL_RETRIES", (0.0, 0.0))
    yield made
    with fx_fetching._GUARD:
        fx_fetching._claim = None


def _household(client, name: str, accounts: list[tuple[str, str, list[str]]]) -> str:
    h = client.post("/api/households", json={"name": name}, headers=HEADERS).json()["id"]
    for account_name, currency, days in accounts:
        account = client.post(
            f"/api/households/{h}/accounts",
            json={"name": account_name, "type": "checking", "currency": currency},
            headers=HEADERS,
        ).json()
        for day in days:
            made = client.post(
                f"/api/households/{h}/transactions",
                json={"account_id": account["id"], "date": day, "amount": -1_000, "payee_name": "Shop"},
                headers=HEADERS,
            )
            assert made.status_code == 201, made.text
    return h


BOTH_YEARS = ["2021-06-15", "2024-02-03"]


@pytest.fixture()
def world(client):
    """Two EUR households.

    Home holds GBP and SEK, which the ECB publishes, ARS, which it never has,
    and RUB, which it stopped publishing on 1 March 2022 -- each with rows in
    2021 and 2024. Away holds only ARS and RUB from 2024, after the RUB
    series ended: nothing it needs is published.
    """
    _setup_owner(client)
    home = _household(
        client,
        "Home",
        [
            ("Checking", "EUR", BOTH_YEARS),
            ("Pounds", "GBP", BOTH_YEARS),
            ("Kronor", "SEK", BOTH_YEARS),
            ("Pesos", "ARS", BOTH_YEARS),
            ("Rubles", "RUB", BOTH_YEARS),
        ],
    )
    away = _household(
        client,
        "Away",
        [("Checking", "EUR", ["2024-02-03"]), ("Pesos", "ARS", ["2024-02-03"]), ("Rubles", "RUB", ["2024-03-01", "2024-04-01"])],
    )
    return {"home": home, "away": away}


def _engine(client):
    return client.app_module.db_engine


def _fetches(client) -> list[FxFetch]:
    with Session(_engine(client), expire_on_commit=False) as s:
        return list(s.execute(select(FxFetch).order_by(FxFetch.started_at)).scalars())


def _rate_count(client) -> int:
    with Session(_engine(client)) as s:
        return s.execute(select(func.count()).select_from(FxRate)).scalar_one()


def _press(client, h: str, **body):
    return client.post(f"/api/households/{h}/fx/fetch", json=body, headers=HEADERS)


# --------------------------------------------------------------------------- #
# A fetch row always ends
# --------------------------------------------------------------------------- #

_JAN_FEB_2024 = {"mode": "range", "since": "2024-01-01", "until": "2024-02-10"}


@pytest.fixture()
def held_back(monkeypatch):
    """The press writes its rows; the work is run by the test, not the request."""
    handed: list[list[str]] = []
    monkeypatch.setattr(fx_fetching, "run", lambda ids, **_: handed.append(ids))
    return handed


def _locked_on(monkeypatch, points: set[int]) -> list[int]:
    """Every commit from here on is counted; the ones numbered in ``points``
    raise what SQLite raises when another writer holds the database."""
    real = Session.commit
    seen: list[int] = []

    def commit(self):
        seen.append(len(seen) + 1)
        if seen[-1] in points:
            raise OperationalError("COMMIT", {}, sqlite3.OperationalError("database is locked"))
        return real(self)

    monkeypatch.setattr(Session, "commit", commit)
    return seen


@pytest.mark.parametrize(
    ("point", "stored", "done"),
    [
        (1, 0, 0),  # marking the year in flight
        (2, 0, 0),  # storing the year and the counts: the year goes with it
        (3, 4, 1),  # recording the outcome: the year is already kept
    ],
    ids=["before-the-year", "after-the-year", "at-the-end"],
)
def test_a_locked_database_at_any_commit_ends_the_row_failed_and_frees_the_slot(
    client, world, held_back, monkeypatch, point, stored, done
):
    h = world["home"]
    pressed = _press(client, h, **_JAN_FEB_2024)
    assert pressed.status_code == 202, pressed.text
    [ids] = held_back
    assert client.get(f"/api/households/{h}/fx/status").json()["running"] is True

    monkeypatch.setattr(fx_fetching, "run", _RUN)
    real_commit = Session.commit
    _locked_on(monkeypatch, {point})
    fx_fetching.run(ids)
    monkeypatch.setattr(Session, "commit", real_commit)

    [row] = _fetches(client)
    assert row.outcome is FxFetchOutcome.failed
    assert row.error == "the fetch stopped on an error: OperationalError"
    assert row.finished_at is not None
    assert row.chunks_done == done
    assert _rate_count(client) == stored, "GBP and SEK on 1 January and 1 February"
    assert fx_fetching.live_ids() == frozenset()

    status = client.get(f"/api/households/{h}/fx/status").json()
    assert status["running"] is False
    assert status["history"][0]["outcome"] == "failed"
    again = _press(client, h, **_JAN_FEB_2024)
    assert again.status_code == 202, "the next press is not refused"
    assert _fetches(client)[-1].outcome is FxFetchOutcome.ok


def test_a_database_locked_for_good_leaves_a_row_that_reads_interrupted_and_the_next_press_closes(
    client, world, held_back, monkeypatch
):
    h = world["home"]
    _press(client, h, **_JAN_FEB_2024)
    [ids] = held_back
    monkeypatch.setattr(fx_fetching, "run", _RUN)
    real_commit = Session.commit
    seen = _locked_on(monkeypatch, set(range(1, 100)))
    fx_fetching.run(ids)
    assert len(seen) >= 4, "the failure was tried more than once"
    monkeypatch.setattr(Session, "commit", real_commit)

    [row] = _fetches(client)
    assert row.outcome is FxFetchOutcome.running, "the failure itself could not be written"
    status = client.get(f"/api/households/{h}/fx/status").json()
    assert status["running"] is False, "nothing works on it, so it is not running"
    assert status["running_fetches"] == []
    assert (status["history"][0]["outcome"], status["history"][0]["error"]) == ("failed", fx_fetching.ABANDONED)

    again = _press(client, h, **_JAN_FEB_2024)
    assert again.status_code == 202
    first, second = _fetches(client)
    assert (first.outcome, first.error) == (FxFetchOutcome.failed, fx_fetching.ABANDONED)
    assert first.finished_at is not None
    assert second.outcome is FxFetchOutcome.ok
    assert _rate_count(client) == 4


def test_a_request_that_fails_after_writing_the_rows_closes_them_and_frees_the_slot(
    client, world, held_back, monkeypatch
):
    h, away = world["home"], world["away"]

    def broken(session, row):
        raise RuntimeError("serialising the answer failed")

    monkeypatch.setattr(fx_dashboard, "fetch_view", broken)
    with pytest.raises(RuntimeError):
        _press(client, h, **_JAN_FEB_2024)

    assert held_back == [], "the work was never handed on"
    [row] = _fetches(client)
    assert (row.outcome, row.error) == (FxFetchOutcome.failed, fx_fetching.ABANDONED)
    assert row.finished_at is not None
    assert fx_fetching.live_ids() == frozenset()
    assert _rate_count(client) == 0

    monkeypatch.setattr(fx_dashboard, "fetch_view", _FETCH_VIEW)
    monkeypatch.setattr(fx_fetching, "run", _RUN)
    again = _press(client, h, **_JAN_FEB_2024)
    assert again.status_code == 202, "the slot was freed"
    assert _fetches(client)[-1].outcome is FxFetchOutcome.ok
    assert client.get(f"/api/households/{away}/fx/status").json()["history"] == []


def test_rows_never_picked_up_are_given_up_after_the_handoff_and_their_late_work_does_nothing(
    client, world, held_back, monkeypatch, asked
):
    h = world["home"]
    _press(client, h, **_JAN_FEB_2024)
    [stuck] = held_back
    assert _press(client, h, **_JAN_FEB_2024).status_code == 409, "inside the handoff it still counts"

    monkeypatch.setattr(fx_fetching, "HANDOFF_SECONDS", -1.0)
    assert client.get(f"/api/households/{h}/fx/status").json()["running"] is False
    taken = _press(client, h, **_JAN_FEB_2024)
    assert taken.status_code == 202
    first, second = _fetches(client)
    assert (first.outcome, first.error) == (FxFetchOutcome.failed, fx_fetching.ABANDONED)
    assert second.outcome is FxFetchOutcome.running

    monkeypatch.setattr(fx_fetching, "HANDOFF_SECONDS", 60.0)
    asked.clear()
    _RUN(stuck)
    assert asked == [], "the given-up press fetches nothing"
    assert [r.outcome for r in _fetches(client)] == [FxFetchOutcome.failed, FxFetchOutcome.running]
    assert fx_fetching.is_running() is True, "and leaves the new press's slot alone"


# --------------------------------------------------------------------------- #
# Only what is published is asked for
# --------------------------------------------------------------------------- #


def test_a_currency_never_published_is_not_asked_for_and_an_ended_one_stops_once_stored(client, world, asked):
    h = world["home"]
    first = _press(client, h)
    assert first.status_code == 202
    assert first.json()[0]["currencies"] == ["GBP", "RUB", "SEK"], "ARS is never published"
    assert all("ARS" not in _codes(url) for url in asked)
    with Session(_engine(client)) as s:
        rub_last = s.execute(select(func.max(FxRate.date)).where(FxRate.currency == "RUB")).scalar_one()
        gbp_last = s.execute(select(func.max(FxRate.date)).where(FxRate.currency == "GBP")).scalar_one()
    assert rub_last == _RUB_LAST

    would = client.get(f"/api/households/{h}/fx/status").json()["would_fetch"]
    assert [(w["currencies"], w["since"]) for w in would] == [(["GBP", "SEK"], gbp_last.isoformat())], (
        "RUB is complete and ARS never comes, so neither pins the start to 2021"
    )
    asked.clear()
    second = _press(client, h)
    assert second.json()[0]["requested_since"] == gbp_last.isoformat()
    assert {code for url in asked for code in _codes(url)} == {"GBP", "SEK"}


def test_a_range_leaves_out_what_was_not_published_in_it(client, world):
    h = world["home"]
    answer = _press(client, h, mode="range", since="2023-01-01", until="2023-03-31")
    assert answer.json()[0]["currencies"] == ["GBP", "SEK"]
    answer = _press(client, h, mode="range", since="2022-02-01", until="2022-03-31")
    assert answer.json()[0]["currencies"] == ["GBP", "RUB", "SEK"]


def test_a_household_needing_only_unpublished_rates_is_told_so_and_lists_them_not_covered(client, world, asked):
    away = world["away"]
    answer = _press(client, away)
    assert answer.status_code == 422
    assert answer.json()["code"] == "fx.nothing_published"
    assert asked == []
    assert _fetches(client) == []

    status = client.get(f"/api/households/{away}/fx/status").json()
    assert status["would_fetch"] == []
    assert status["would_fetch_refused"] == "fx.nothing_published"
    assert [(n["currency"], n["reason"]) for n in status["not_covered"]] == [("ARS", "no_source"), ("RUB", "no_source")]

    home = client.get(f"/api/households/{world['home']}/fx/status").json()
    assert [(n["currency"], n["reason"]) for n in home["not_covered"]] == [("ARS", "no_source")], (
        "RUB from 2021 is published for Home's first rows"
    )


# --------------------------------------------------------------------------- #
# The source
# --------------------------------------------------------------------------- #


def test_every_historical_ecb_currency_has_its_last_published_day():
    assert set(fx_sources.ECB_LAST_PUBLISHED) == fx_sources.ECB_HISTORICAL
    assert fx_sources.last_published("rub") == _RUB_LAST
    assert fx_sources.last_published("GBP") is None
    assert (fx_sources.publishes("SEK"), fx_sources.publishes("ARS")) == (True, False)


def test_a_404_is_no_rates_and_any_other_refusal_is_still_an_error():
    def not_found(url):
        raise urllib.error.HTTPError(url, 404, "No results found", {}, None)

    def broken(url):
        raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)

    assert fx_sources.fetch_ecb(["GBP", "SEK"], date(2026, 1, 1), date(2026, 1, 1), get=not_found) == []
    with pytest.raises(fx_sources.SourceError, match="500"):
        fx_sources.fetch_ecb(["GBP", "SEK"], date(2026, 1, 1), date(2026, 1, 1), get=broken)
