"""Arithmetic over the ledger, asserted on the figures and not on the 200.

`CLAUDE.md`: *a test that does not assert a changed value is not a test*. So
every sum below is compared against an integer computed from the fixture by
hand, and the currency rule is asserted by the **absence** of a key rather than
by the presence of a correct one -- an endpoint that grew a grand total later
would still return 200 with plausible numbers, and only the absence catches it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import BatchKind, Household, HouseholdMember, User
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _setup_owner


@pytest.fixture()
def ledger(client):
    """Two accounts, two currencies, and figures chosen so the sums are legible.

    EUR: -1250, -3000, +5000  -> -1250 + -3000 + 5000 =  750, three rows
    GBP: -2200                ->                        -2200, one row
    """
    world = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()

    eur = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Santander", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    gbp = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Monzo", "type": "checking", "currency": "GBP"},
        headers=HEADERS,
    ).json()

    rows = [
        (eur["id"], "2026-07-05", -1250, "Mercadona"),
        (eur["id"], "2026-08-05", -3000, "Mercadona"),
        (eur["id"], "2026-09-05", 5000, "Salary"),
        (gbp["id"], "2026-08-11", -2200, "Tesco"),
    ]
    for account_id, when, amount, payee in rows:
        made = client.post(
            f"/api/households/{house['id']}/transactions",
            json={
                "account_id": account_id, "date": when,
                "amount": amount, "payee_name": payee,
            },
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text

    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, world["user"]["id"])
        house_row = own.get(Household, house["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=house_row.id):
            _key, token = key_service.issue(
                own, user=user, household=house_row, label="the analyst"
            )
        own.commit()

    client.cookies.clear()
    return {"token": token, "house": house, "eur": eur, "gbp": gbp}


def _get(client, ledger, path: str, **params):
    return client.get(
        f"/api/agent/v1/households/{ledger['house']['id']}{path}",
        params=params,
        headers={"authorization": f"Bearer {ledger['token']}"},
    )


# --------------------------------------------------------------------------- #
# The rule this module exists to keep
# --------------------------------------------------------------------------- #


def test_two_currencies_come_back_in_two_buckets_and_are_never_added(client, ledger):
    """The failure that produced three wrong reports in the previous build."""
    body = _get(client, ledger, "/summary").json()

    assert set(body["by_currency"]) == {"EUR", "GBP"}
    assert body["by_currency"]["EUR"]["total_minor"] == 750
    assert body["by_currency"]["GBP"]["total_minor"] == -2200

    # And nothing anywhere is their sum. -1450 is the number a naive
    # implementation would produce, so it is the one to look for.
    assert "total_minor" not in body
    assert "total" not in body
    assert "-1450" not in _get(client, ledger, "/summary").text


def test_the_sums_are_the_integers_from_the_fixture(client, ledger):
    """Asserted on the value, not on the status code."""
    body = _get(client, ledger, "/summary", group_by="account").json()

    eur = {g["name"]: g for g in body["by_currency"]["EUR"]["groups"]}
    assert eur["Santander"]["sum_minor"] == -1250 + -3000 + 5000
    assert eur["Santander"]["count"] == 3

    gbp = {g["name"]: g for g in body["by_currency"]["GBP"]["groups"]}
    assert gbp["Monzo"]["sum_minor"] == -2200
    assert gbp["Monzo"]["count"] == 1


def test_every_figure_carries_a_string_beside_the_integer(client, ledger):
    """The integer is for arithmetic, the string is for the sentence."""
    body = _get(client, ledger, "/summary", group_by="account").json()
    eur = body["by_currency"]["EUR"]

    assert eur["total_minor"] == 750
    assert eur["total"] == "7.50"
    assert all(isinstance(g["sum"], str) for g in eur["groups"])


# --------------------------------------------------------------------------- #
# Grouping and filtering
# --------------------------------------------------------------------------- #


def test_grouping_by_payee_gathers_the_two_mercadona_rows(client, ledger):
    body = _get(client, ledger, "/summary", group_by="payee").json()
    eur = {g["name"]: g for g in body["by_currency"]["EUR"]["groups"]}

    assert eur["Mercadona"]["sum_minor"] == -4250
    assert eur["Mercadona"]["count"] == 2
    assert eur["Salary"]["sum_minor"] == 5000


def test_grouping_by_month_splits_them_apart(client, ledger):
    body = _get(client, ledger, "/summary", group_by="month").json()
    eur = {g["name"]: g["sum_minor"] for g in body["by_currency"]["EUR"]["groups"]}

    assert eur == {"2026-07": -1250, "2026-08": -3000, "2026-09": 5000}


def test_the_filter_vocabulary_is_the_registers(client, ledger):
    """Same names, same meanings, because it is literally the same function."""
    narrowed = _get(client, ledger, "/summary", group_by="account", since="2026-08-01").json()
    eur = {g["name"]: g for g in narrowed["by_currency"]["EUR"]["groups"]}

    assert eur["Santander"]["sum_minor"] == -3000 + 5000
    assert eur["Santander"]["count"] == 2

    to_one = _get(
        client, ledger, "/summary", group_by="account", account_id=ledger["gbp"]["id"]
    ).json()
    assert set(to_one["by_currency"]) == {"GBP"}, "one account means one currency"


def test_an_unknown_grouping_is_refused_with_the_list(client, ledger):
    refused = _get(client, ledger, "/summary", group_by="phase_of_the_moon")
    assert refused.status_code == 422
    assert "category" in refused.json()["detail"]


def test_uncategorised_rows_get_a_name_rather_than_a_null(client, ledger):
    """Null is a real and common answer; a caller should not have to special-case it."""
    body = _get(client, ledger, "/summary", group_by="category").json()
    names = {g["name"] for g in body["by_currency"]["EUR"]["groups"]}
    assert "Uncategorised" in names


# --------------------------------------------------------------------------- #
# Timeseries and balances
# --------------------------------------------------------------------------- #


def test_the_timeseries_buckets_by_month_in_order(client, ledger):
    body = _get(client, ledger, "/timeseries", bucket="month").json()
    eur = [(g["name"], g["sum_minor"]) for g in body["by_currency"]["EUR"]["groups"]]
    assert eur == [("2026-07", -1250), ("2026-08", -3000), ("2026-09", 5000)]


def test_a_week_that_straddles_new_year_is_one_bucket(client, ledger):
    """The whole Monday-to-Sunday week, on one side of the year boundary or both.

    `%Y-W%W` pairs a Monday-based week number with the CALENDAR year, which is
    not ISO week numbering -- so 29 Dec 2025 to 4 Jan 2026 came back as
    `2025-W52` holding Monday to Wednesday and `2026-W00` holding Thursday to
    Sunday. Two consecutive weeks at half their real spend, every January, and
    nothing said either was partial.
    """
    from datetime import date as Date

    from app.models import Transaction

    house = ledger["house"]["id"]
    eur = ledger["eur"]["id"]
    # Monday the 29th through Sunday the 4th: one week, four days of it in the
    # new year.
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        with batch(own, kind=BatchKind.manual, actor_id=_actor(own), household_id=house):
            for when, amount in [
                ("2025-12-29", -100),   # Mon
                ("2025-12-31", -200),   # Wed
                ("2026-01-01", -400),   # Thu
                ("2026-01-04", -800),   # Sun
                ("2026-01-05", -1600),  # the NEXT Monday, which must stay separate
            ]:
                own.add(Transaction(
                    household_id=house, account_id=eur,
                    date=Date.fromisoformat(when), amount=amount,
                ))
        own.commit()

    body = _get(client, ledger, "/timeseries", bucket="week", since="2025-12-29",
                until="2026-01-05").json()
    weeks = [(g["name"], g["sum_minor"]) for g in body["by_currency"]["EUR"]["groups"]]

    assert weeks == [("2025-12-29", -1500), ("2026-01-05", -1600)]


def test_balances_are_per_account_with_no_household_total(client, ledger):
    body = _get(client, ledger, "/balances").json()
    by_name = {a["name"]: a for a in body["accounts"]}

    assert by_name["Santander"]["balance_minor"] == 750
    assert by_name["Monzo"]["balance_minor"] == -2200
    assert "total" not in body and "total_minor" not in body


def test_a_balance_as_of_a_date_stops_there(client, ledger):
    body = _get(client, ledger, "/balances", as_of="2026-08-31").json()
    by_name = {a["name"]: a for a in body["accounts"]}
    assert by_name["Santander"]["balance_minor"] == -1250 + -3000, "September is excluded"


# --------------------------------------------------------------------------- #
# Delta reads
# --------------------------------------------------------------------------- #


def test_a_first_read_returns_everything_and_a_sequence_to_come_back_with(client, ledger):
    body = _get(client, ledger, "/transactions").json()
    assert len(body["changed"]) == 4
    assert body["server_seq"] > 0
    assert body["deleted"] == []


def test_a_second_read_returns_only_what_moved(client, ledger):
    """The payoff: a nightly agent reads the rows that changed, not the register."""
    first = _get(client, ledger, "/transactions").json()
    seq = first["server_seq"]

    assert _get(client, ledger, "/transactions", since_seq=seq).json()["changed"] == []

    # Move exactly one row.
    from sqlalchemy import select

    from app.models import Transaction

    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        row = own.execute(
            select(Transaction).where(Transaction.amount == -1250)
        ).scalar_one()
        with batch(
            own, kind=BatchKind.manual, actor_id=_actor(own), household_id=row.household_id
        ):
            row.memo = "changed"
        own.commit()

    again = _get(client, ledger, "/transactions", since_seq=seq).json()
    assert len(again["changed"]) == 1
    assert again["changed"][0]["memo"] == "changed"
    assert again["server_seq"] > seq


def test_a_truncated_page_does_not_step_the_cursor_over_what_it_withheld(client, ledger):
    """The failure this endpoint existed to avoid, in its own cursor.

    `server_seq` used to be the household's global maximum, computed without
    reference to `limit`. A client doing exactly what `DeltaOut` documents --
    "send it back next time" -- stepped over every row the page had truncated
    and never saw them again. Four rows in the fixture, read one at a time:
    before the fix this surfaced one and then an empty page.
    """
    seen: list[str] = []
    seq = 0
    for _ in range(10):
        page = _get(client, ledger, "/transactions", since_seq=seq, limit=1).json()
        seen.extend(row["id"] for row in page["changed"])
        seq = page["server_seq"]
        if not page["has_more"]:
            break

    assert len(seen) == 4, f"paging surfaced {len(seen)} of the 4 transactions"
    assert len(set(seen)) == 4, "and each exactly once"


def test_a_full_page_reports_the_last_row_it_handed_over_not_the_global_maximum(client, ledger):
    page = _get(client, ledger, "/transactions", since_seq=0, limit=2).json()

    assert page["has_more"] is True
    assert len(page["changed"]) == 2
    # The whole ledger is further along than this page reached.
    whole = _get(client, ledger, "/transactions", since_seq=0).json()
    assert page["server_seq"] < whole["server_seq"], "a truncated page must not claim the end"
    assert whole["has_more"] is False


def _actor(session) -> str:
    from sqlalchemy import select

    return session.execute(select(HouseholdMember.user_id)).scalars().first()


def test_a_deleted_row_is_a_tombstone_and_not_a_change(client, ledger):
    """Hard deletes make this simpler: `op = delete` IS the tombstone stream."""
    from sqlalchemy import select

    from app.models import Transaction

    first = _get(client, ledger, "/transactions").json()
    seq = first["server_seq"]

    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        row = own.execute(select(Transaction).where(Transaction.amount == -2200)).scalar_one()
        gone_id = row.id
        with batch(
            own, kind=BatchKind.manual, actor_id=_actor(own), household_id=row.household_id
        ):
            own.delete(row)
        own.commit()

    after = _get(client, ledger, "/transactions", since_seq=seq).json()
    assert gone_id in after["deleted"]
    assert gone_id not in {row["id"] for row in after["changed"]}


# --------------------------------------------------------------------------- #
# Reach
# --------------------------------------------------------------------------- #


def test_another_households_id_in_the_path_is_a_404(client, ledger):
    """The id is in the URL because a URL should say what it acts on -- and it
    is checked rather than trusted."""
    answer = client.get(
        "/api/agent/v1/households/some-other-household/summary",
        headers={"authorization": f"Bearer {ledger['token']}"},
    )
    assert answer.status_code == 404


def test_the_rows_each_answer_covered_are_recorded(client, ledger):
    """"GET /summary 200" says nothing; the row count is what makes it useful."""
    from sqlalchemy import select

    from app.models import AgentRequest

    _get(client, ledger, "/summary")
    with Session(client.app_module.db_engine) as own:
        line = own.execute(
            select(AgentRequest).order_by(AgentRequest.at.desc())
        ).scalars().first()
    assert line.rows == 4, "three EUR rows and one GBP"
