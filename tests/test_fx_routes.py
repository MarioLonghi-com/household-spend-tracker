"""Exchange rates over HTTP: settings, manual rates, Fetch now, the dashboard, agents (#396).

Through the real app and its real lifespan. **No test reaches the network**:
the ECB's answer is the recorded sample in `tests/fixtures/fx/`, handed to the
adapter in place of the socket, and the autouse fixture below fails any test
that lets a fetch through to the real one.
"""

from __future__ import annotations

import urllib.error
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import (
    Account,
    AccountType,
    BatchKind,
    Change,
    FxFetch,
    FxFetchMode,
    FxFetchOutcome,
    FxManualRate,
    FxRate,
    FxSource,
    Household,
    User,
)
from app.services import agent_keys as key_service
from app.services import fx_fetching, fx_sources
from tests.conftest import HEADERS, _setup_owner

SAMPLE = Path(__file__).resolve().parent / "fixtures/fx/ecb-gbp-sek-usd-2026-10-29-to-11-04.csv"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every fetch in this module answers from the recording, and says what it asked."""
    asked: list[str] = []

    def answer(url: str) -> bytes:
        asked.append(url)
        return SAMPLE.read_bytes()

    monkeypatch.setattr(fx_sources, "_get", answer)
    return asked


@pytest.fixture()
def world(client):
    """One household in EUR with accounts in EUR, GBP and SEK and a row in each."""
    owner = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    h = house["id"]
    made = {}
    for name, currency in (("Checking", "EUR"), ("Pounds", "GBP"), ("Kronor", "SEK")):
        made[currency] = client.post(
            f"/api/households/{h}/accounts",
            json={"name": name, "type": "checking", "currency": currency},
            headers=HEADERS,
        ).json()
        for day in ("2026-10-30", "2026-11-03"):
            answer = client.post(
                f"/api/households/{h}/transactions",
                json={"account_id": made[currency]["id"], "date": day, "amount": -1_000, "payee_name": "Shop"},
                headers=HEADERS,
            )
            assert answer.status_code == 201, answer.text
    return {"house": h, "accounts": made, "owner": owner}


def _engine(client):
    return client.app_module.db_engine


def _count(client, model, *where) -> int:
    with Session(_engine(client)) as s:
        return s.execute(select(func.count()).select_from(model).where(*where)).scalar_one()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_settings_start_at_manual_then_ecb_and_a_change_is_audited(client, world):
    h = world["house"]
    got = client.get(f"/api/households/{h}/fx/settings").json()
    assert got == {
        "base_currency": "EUR",
        "chain": ["manual", "ecb"],
        "stale_days": 7,
        "monthly_policy": "month_avg",
        "show_own_rates": True,
    }
    before = client.get(f"/api/households/{h}/changes/latest").json()["seq"]

    put = client.put(
        f"/api/households/{h}/fx/settings",
        json={"chain": ["own", "ecb"], "stale_days": 10, "monthly_policy": "spot", "show_own_rates": False},
        headers=HEADERS,
    )
    assert put.status_code == 200, put.text
    assert put.json()["chain"] == ["own", "ecb"]
    assert client.get(f"/api/households/{h}/fx/settings").json()["stale_days"] == 10

    after = client.get(f"/api/households/{h}/changes/latest").json()["seq"]
    assert after > before, "an open page notices through /changes/latest"
    with Session(_engine(client)) as s:
        change = s.execute(
            select(Change).where(Change.table_name == "households").order_by(Change.seq.desc())
        ).scalars().first()
    assert change.before["fx_chain"] == "manual,ecb"
    assert change.after["fx_chain"] == "own,ecb"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"chain": ["boe"]}, "fx.unknown_source"),
        ({"chain": ["ecb", "ecb"]}, "fx.source_twice"),
        ({"stale_days": 0}, "fx.stale_days_range"),
        ({"monthly_policy": "weekly"}, "fx.unknown_policy"),
    ],
)
def test_a_setting_that_means_nothing_is_refused_and_nothing_changes(client, world, body, code):
    h = world["house"]
    full = {"chain": ["manual", "ecb"], "stale_days": 7, "monthly_policy": "month_avg", "show_own_rates": True}
    answer = client.put(f"/api/households/{h}/fx/settings", json=full | body, headers=HEADERS)
    assert answer.status_code == 422
    assert answer.json()["code"] == code
    assert client.get(f"/api/households/{h}/fx/settings").json()["chain"] == ["manual", "ecb"]


def test_another_households_rates_are_a_404(client, world):
    with Session(_engine(client)) as s:
        user = s.execute(select(User)).scalars().first()
        with batch(s, kind=BatchKind.admin, actor_id=user.id):
            stranger = Household(name="Next door", base_currency="GBP")
            s.add(stranger)
        s.commit()
        other = stranger.id
    for path in ("settings", "status", "rates"):
        assert client.get(f"/api/households/{other}/fx/{path}").status_code == 404
    assert client.post(f"/api/households/{other}/fx/fetch", json={}, headers=HEADERS).status_code == 404
    assert _count(client, FxFetch) == 0


# --------------------------------------------------------------------------- #
# Manual rates
# --------------------------------------------------------------------------- #


def test_a_dry_run_shows_the_inverse_and_keeps_nothing(client, world):
    h = world["house"]
    answer = client.post(
        f"/api/households/{h}/fx/rates/manual",
        json={"currency": "gbp", "date": "2026-10-30", "rate": "0.8", "dry_run": True},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    row = answer.json()["rows"][0]
    assert (row["currency"], row["period"], row["inverse"], row["problems"]) == ("GBP", "day", "1.25", [])
    assert _count(client, FxManualRate) == 0


def test_a_rate_is_saved_replaced_and_deleted_and_undo_brings_it_back(client, world):
    h = world["house"]
    url = f"/api/households/{h}/fx/rates/manual"
    first = client.post(url, json={"currency": "SEK", "date": "2026-10", "rate": "11.2"}, headers=HEADERS)
    assert first.status_code == 201, first.text
    assert first.json()["added"] == 1

    second = client.post(url, json={"currency": "SEK", "date": "2026-10", "rate": "11.3"}, headers=HEADERS)
    assert second.json()["replaced"] == 1
    assert second.json()["rows"][0]["replaces"] == "11.2"

    rates = client.get(f"/api/households/{h}/fx/rates", params={"source": "manual"}).json()["rates"]
    assert [(r["currency"], r["date"], r["period"], r["rate"]) for r in rates] == [
        ("SEK", "2026-10-01", "month", "11.3")
    ]

    gone = client.delete(f"{url}/{rates[0]['id']}", headers=HEADERS)
    assert gone.status_code == 204
    assert _count(client, FxManualRate) == 0

    with Session(_engine(client)) as s:
        deletion = s.execute(
            select(Change.batch_id).where(Change.table_name == "fx_manual_rates", Change.op == "delete")
        ).scalar_one()
    undone = client.post(f"/api/households/{h}/batches/{deletion}/undo", headers=HEADERS)
    assert undone.status_code == 200, undone.text
    with Session(_engine(client)) as s:
        back = s.execute(select(FxManualRate)).scalar_one()
    assert (back.currency, back.rate) == ("SEK", "11.3")


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ({"currency": "EUR", "date": "2026-10-30", "rate": "1"}, "base currency"),
        ({"currency": "GPB", "date": "2026-10-30", "rate": "1"}, "ISO 4217"),
        ({"currency": "GBP", "date": "30/10/2026", "rate": "1"}, "YYYY-MM-DD"),
        ({"currency": "GBP", "date": "2026-10-30", "rate": "0"}, "more than zero"),
        ({"currency": "GBP", "date": "2026-10-30", "rate": "0,85"}, "not a rate"),
        ({"currency": "GBP", "date": (date.today() + timedelta(days=2)).isoformat(), "rate": "1"}, "future"),
    ],
)
def test_a_rate_that_cannot_be_kept_says_why_and_nothing_is_kept(client, world, entry, says):
    answer = client.post(
        f"/api/households/{world['house']}/fx/rates/manual", json=entry, headers=HEADERS
    )
    assert answer.status_code == 422
    assert answer.json()["code"] == "fx_manual.rows_refused"
    preview = client.post(
        f"/api/households/{world['house']}/fx/rates/manual", json=entry | {"dry_run": True}, headers=HEADERS
    ).json()
    assert says in " ".join(preview["rows"][0]["problems"])
    assert _count(client, FxManualRate) == 0


def test_a_file_of_rates_is_all_or_none_with_a_dry_run_first(client, world):
    h = world["house"]
    url = f"/api/households/{h}/fx/rates/manual/import"
    good = b"currency,date,rate\r\nGBP,2026-10-29,0.84\r\nSEK,2026-10,11.1\r\n"
    bad = good + b"GBP,2026-10-29,0.85\r\n"

    preview = client.post(url, files={"file": ("rates.csv", bad, "text/csv")}, data={"dry_run": "true"}, headers=HEADERS)
    assert preview.status_code == 200
    rows = preview.json()["rows"]
    assert [r["line"] for r in rows] == [2, 3, 4]
    assert rows[2]["problems"] == ["line 2 already gives a rate for GBP on 2026-10-29"]

    refused = client.post(url, files={"file": ("rates.csv", bad, "text/csv")}, data={"dry_run": "false"}, headers=HEADERS)
    assert refused.status_code == 422
    assert refused.json()["params"] == {"refused": 1, "rows": 3}
    assert _count(client, FxManualRate) == 0, "one bad line keeps the whole file out"

    kept = client.post(url, files={"file": ("rates.csv", good, "text/csv")}, data={"dry_run": "false"}, headers=HEADERS)
    assert kept.status_code == 201, kept.text
    assert kept.json()["added"] == 2
    with Session(_engine(client)) as s:
        stored = {(r.currency, r.date, r.period.value, r.rate) for r in s.execute(select(FxManualRate)).scalars()}
    assert stored == {("GBP", date(2026, 10, 29), "day", "0.84"), ("SEK", date(2026, 10, 1), "month", "11.1")}


def test_a_file_with_a_column_it_does_not_know_is_refused_whole(client, world):
    answer = client.post(
        f"/api/households/{world['house']}/fx/rates/manual/import",
        files={"file": ("rates.csv", b"currency,day,rate\nGBP,2026-10-29,0.84\n", "text/csv")},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json()["code"] == "fx_manual.unknown_column"


def test_the_template_is_the_header_and_nothing_else(client, world):
    answer = client.get(f"/api/households/{world['house']}/fx/rates/manual/template.csv")
    assert answer.text == "﻿currency,date,rate\r\n"


# --------------------------------------------------------------------------- #
# Fetch now
# --------------------------------------------------------------------------- #


def test_fetch_now_stores_the_rates_and_records_who_asked(client, world, no_network):
    h = world["house"]
    started = client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
    assert started.status_code == 202, started.text
    [row] = started.json()
    assert (row["source"], row["mode"], row["currencies"]) == ("ecb", "missing", ["GBP", "SEK"])
    assert row["requested_since"] == "2026-10-30", "the first fetch starts at the earliest transaction"

    assert len(no_network) == 1
    assert "D.GBP+SEK.EUR.SP00.A" in no_network[0]
    assert _count(client, FxRate) == 15

    status = client.get(f"/api/households/{h}/fx/status").json()
    last = status["tiles"]["last_fetch"]
    assert (last["outcome"], last["rates_added"], last["rates_unchanged"]) == ("ok", 15, 0)
    assert last["started_by"] == client.get("/api/me").json()["display_name"]
    assert status["running"] is False

    again = client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
    assert again.status_code == 202
    assert again.json()[0]["requested_since"] == "2026-11-04", "later fetches start at the last stored rate"
    status = client.get(f"/api/households/{h}/fx/status").json()
    assert (status["history"][0]["rates_added"], status["history"][0]["rates_unchanged"]) == (0, 15)
    assert _count(client, FxRate) == 15, "a second fetch of the same days adds nothing"


def test_a_second_press_while_one_runs_is_409_and_writes_nothing(client, world):
    h = world["house"]
    claim = fx_fetching._take_slot()
    try:
        answer = client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
    finally:
        fx_fetching._free_slot(claim)
    assert answer.status_code == 409
    assert answer.json()["code"] == "fx.fetch_running"
    assert _count(client, FxFetch) == 0


def test_a_failed_fetch_keeps_the_stored_rates_and_says_why(client, world, monkeypatch):
    h = world["house"]
    client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)

    def offline(url):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(fx_sources, "_get", offline)
    answer = client.post(
        f"/api/households/{h}/fx/fetch",
        json={"mode": "range", "since": "2026-10-29", "until": "2026-11-04"},
        headers=HEADERS,
    )
    assert answer.status_code == 202
    status = client.get(f"/api/households/{h}/fx/status").json()
    assert status["history"][0]["outcome"] == "failed"
    assert status["history"][0]["error"] == "could not reach data-api.ecb.europa.eu: no route to host"
    assert status["rates_status"]["last_fetch_failed"]["error"].startswith("could not reach")
    assert _count(client, FxRate) == 15, "the rates already stored stay"


def test_a_range_must_be_a_range_in_the_past(client, world):
    h = world["house"]
    backwards = client.post(
        f"/api/households/{h}/fx/fetch",
        json={"mode": "range", "since": "2026-11-04", "until": "2026-10-29"},
        headers=HEADERS,
    )
    assert backwards.json()["code"] == "fx.range_backwards"
    assert client.post(
        f"/api/households/{h}/fx/fetch", json={"mode": "range"}, headers=HEADERS
    ).json()["code"] == "fx.range_needs_dates"
    assert _count(client, FxFetch) == 0


def test_with_no_remote_source_in_the_chain_there_is_nothing_to_fetch(client, world, no_network):
    h = world["house"]
    client.put(
        f"/api/households/{h}/fx/settings",
        json={"chain": ["manual"], "stale_days": 7, "monthly_policy": "month_avg", "show_own_rates": True},
        headers=HEADERS,
    )
    answer = client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
    assert answer.json()["code"] == "fx.nothing_to_fetch"
    assert no_network == []
    status = client.get(f"/api/households/{h}/fx/status").json()
    assert status["would_fetch"] == []
    assert status["would_fetch_refused"] == "fx.nothing_to_fetch"


def test_a_fetch_left_running_by_a_stopped_process_is_failed_at_the_next_boot(client, world):
    h = world["house"]
    with Session(_engine(client)) as s:
        s.add(
            FxFetch(
                household_id=h, source=FxSource.ecb, mode=FxFetchMode.missing,
                requested_since=date(2026, 10, 1), requested_until=date(2026, 10, 2),
                currencies=["GBP"], outcome=FxFetchOutcome.running,
            )
        )
        s.commit()
    # Nothing in this process works on it, so it is not shown running (#444).
    before = client.get(f"/api/households/{h}/fx/status").json()
    assert before["running"] is False
    assert before["running_fetches"] == []
    assert (before["history"][0]["outcome"], before["history"][0]["error"]) == ("failed", fx_fetching.ABANDONED)

    assert fx_fetching.mark_interrupted(_engine(client)) == 1

    status = client.get(f"/api/households/{h}/fx/status").json()
    assert status["running"] is False
    assert status["history"][0]["outcome"] == "failed"
    assert status["history"][0]["error"] == fx_fetching.INTERRUPTED


def test_booting_marks_an_unfinished_fetch_and_fetches_nothing(monkeypatch, tmp_path):
    """The wiring, not the function: boot the real app, and the row is failed by
    the first request -- with no request to the ECB made on the way."""
    import importlib

    from fastapi.testclient import TestClient

    from app.models import Base, HouseholdMember, utcnow
    from tests.conftest import _bootstrap_user, _stamp_head

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'boot.sqlite3'}")
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path))
    import app.config as config

    importlib.reload(config)
    import app.db as db_module

    importlib.reload(db_module)
    Base.metadata.create_all(db_module.engine)
    _stamp_head(db_module.engine)

    asked: list[str] = []
    monkeypatch.setattr(fx_sources, "_get", lambda url: asked.append(url) or b"")

    with db_module.SessionLocal() as s:
        from app.models import Role

        user = _bootstrap_user(s, email="boot@example.com", name="Boot", role=Role.owner)
        with batch(s, kind=BatchKind.admin, actor_id=user.id):
            house = Household(name="Boot", base_currency="EUR")
            s.add(house)
            s.flush()
            s.add(HouseholdMember(household_id=house.id, user_id=user.id, added_by_id=user.id, added_at=utcnow()))
        s.commit()
        s.add(
            FxFetch(
                household_id=house.id, source=FxSource.ecb, mode=FxFetchMode.missing,
                requested_since=date(2026, 10, 1), requested_until=date(2026, 10, 2),
                currencies=["GBP"], outcome=FxFetchOutcome.running,
            )
        )
        s.commit()

    import app.main as main

    importlib.reload(main)
    with TestClient(main.app, base_url="https://testserver") as booted:
        assert booted.get("/api/health").status_code == 200

    with Session(db_module.engine) as s:
        row = s.execute(select(FxFetch)).scalar_one()
    assert (row.outcome, row.error) == (FxFetchOutcome.failed, fx_fetching.INTERRUPTED)
    assert asked == [], "booting fetched nothing"
    db_module.engine.dispose()


# --------------------------------------------------------------------------- #
# The dashboard
# --------------------------------------------------------------------------- #


def test_the_dashboard_counts_coverage_gaps_and_age_from_the_stored_rows(client, world):
    h = world["house"]
    before = client.get(f"/api/households/{h}/fx/status").json()
    assert before["tiles"]["last_fetch"] is None
    assert before["tiles"]["covered"] == 0 and before["tiles"]["total"] == 2
    assert [w["currencies"] for w in before["would_fetch"]] == [["GBP", "SEK"]]
    assert before["rates_status"]["per_currency"] == []
    assert before["rates_status"]["not_converted"] == ["GBP", "SEK"]

    client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
    status = client.get(f"/api/households/{h}/fx/status").json()
    rows = {row["currency"]: row for row in status["currencies"]}
    gbp = rows["GBP"]
    assert [a["name"] for a in gbp["accounts"]] == ["Pounds"]
    assert (gbp["first_rate"], gbp["last_rate"], gbp["rates"]) == ("2026-10-29", "2026-11-04", 5)
    assert gbp["transaction_dates"] == 2
    today = date.today()
    stale = (today - date(2026, 11, 4)).days > 7
    if stale:
        # The sample's last rate is old by the time this runs: the 3 Nov row
        # is still covered (a rate the day before), and the status says stale.
        assert gbp["status"] == "stale"
    else:
        assert gbp["status"] == "ok"
    assert gbp["coverage_percent"] == 100 and gbp["gaps"] == 0
    assert gbp["source_in_use"] == "ecb"
    assert gbp["age_days"] == (today - date(2026, 11, 4)).days
    assert {cell["month"]: cell["state"] for cell in gbp["months"]}["2026-10"] == "rates"
    assert status["tiles"]["covered"] == 2

    per = {c["currency"]: c for c in status["rates_status"]["per_currency"]}
    assert per["GBP"]["rate_date"] == "2026-11-04"
    assert status["rates_status"]["as_of"] == "2026-11-04"


def test_an_account_in_a_code_iso_never_assigned_is_listed_as_not_covered(client, world):
    h = world["house"]
    with Session(_engine(client)) as s:
        user = s.execute(select(User)).scalars().first()
        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=h):
            s.add(Account(household_id=h, name="Old typo", type=AccountType.savings, currency="GPB"))
        s.commit()
    status = client.get(f"/api/households/{h}/fx/status").json()
    assert {
        k: v for k, v in status["not_covered"][0].items() if k in ("currency", "reason")
    } == {"currency": "GPB", "reason": "not_iso"}
    gpb = next(row for row in status["currencies"] if row["currency"] == "GPB")
    assert (gpb["iso"], gpb["status"]) == (False, "not_covered")
    # Nothing else about it changes: the account is still there, in its code.
    accounts = client.get(f"/api/households/{h}/accounts").json()
    assert any(a["currency"] == "GPB" for a in accounts)


def test_each_currency_lists_its_accounts_with_bank_country_and_closed(client, world):
    """What the (i) beside a currency reads (#411): two accounts in SEK, one closed."""
    h = world["house"]
    kronor = world["accounts"]["SEK"]["id"]
    answer = client.patch(
        f"/api/accounts/{kronor}",
        json={"institution": "Nordbank", "country": "se", "closed": True},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    answer = client.post(
        f"/api/households/{h}/accounts",
        json={"name": "Buffer", "type": "savings", "currency": "SEK"},
        headers=HEADERS,
    )
    assert answer.status_code == 201, answer.text

    status = client.get(f"/api/households/{h}/fx/status").json()
    sek = next(row for row in status["currencies"] if row["currency"] == "SEK")
    by_name = {a["name"]: a for a in sek["accounts"]}
    assert list(by_name) == ["Buffer", "Kronor"]
    assert {k: by_name["Kronor"][k] for k in ("institution", "country", "country_name", "closed")} == {
        "institution": "Nordbank",
        "country": "SE",
        "country_name": "Sweden",
        "closed": True,
    }
    assert by_name["Kronor"]["flag"] == "\U0001F1F8\U0001F1EA"
    assert {k: by_name["Buffer"][k] for k in ("institution", "country", "country_name", "closed")} == {
        "institution": None,
        "country": None,
        "country_name": None,
        "closed": False,
    }
    # The other currency's list is its own: nothing of SEK's leaks into GBP.
    gbp = next(row for row in status["currencies"] if row["currency"] == "GBP")
    assert [(a["name"], a["closed"]) for a in gbp["accounts"]] == [("Pounds", False)]


def test_the_browsed_rates_carry_their_base_meaning_and_own_rate(client, world):
    h = world["house"]
    client.post(f"/api/households/{h}/fx/fetch", json={}, headers=HEADERS)
    answer = client.get(f"/api/households/{h}/fx/rates", params={"currency": "GBP", "since": "2026-11-04"}).json()
    assert answer["base_currency"] == "EUR"
    assert [(r["date"], r["rate"], r["rate_in_base"]) for r in answer["rates"]] == [
        ("2026-11-04", "0.8346", "0.8346")
    ]


# --------------------------------------------------------------------------- #
# Agents read, and only read
# --------------------------------------------------------------------------- #


@pytest.fixture()
def key(client, world):
    with Session(_engine(client), expire_on_commit=False) as own:
        user = own.execute(select(User)).scalars().first()
        house = own.get(Household, world["house"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=house.id):
            _row, token = key_service.issue(own, user=user, household=house, label="analyst", agent_name="Test")
        own.commit()
    client.cookies.clear()
    return {"authorization": f"Bearer {token}"}


def test_an_agent_key_reads_rates_and_status_and_cannot_fetch_or_change_settings(client, world, key, no_network):
    h = world["house"]
    base = f"/api/agent/v1/households/{h}/fx"
    status = client.get(f"{base}/status", headers=key)
    assert status.status_code == 200, status.text
    assert status.json()["chain"] == ["manual", "ecb"]
    rates = client.get(f"{base}/rates", headers=key)
    assert rates.status_code == 200
    assert rates.json()["rates"] == []

    assert client.post(f"{base}/fetch", json={}, headers=key).status_code in (404, 405)
    for method, path, body in (
        ("post", f"/api/households/{h}/fx/fetch", {}),
        ("put", f"/api/households/{h}/fx/settings", {"chain": [], "stale_days": 7, "monthly_policy": "spot", "show_own_rates": True}),
        ("post", f"/api/households/{h}/fx/rates/manual", {"currency": "GBP", "date": "2026-10-30", "rate": "1"}),
    ):
        answer = getattr(client, method)(path, json=body, headers=key | HEADERS)
        assert answer.status_code in (401, 403), f"{method} {path} answered {answer.status_code}"
    assert no_network == []
    assert _count(client, FxFetch) == 0
    assert _count(client, FxManualRate) == 0
