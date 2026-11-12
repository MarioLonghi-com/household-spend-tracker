"""The day a household's weeks start on (#452).

Asserted on the figures: which days land in which weekly bucket, and what the
bucket is called. Six spends on six consecutive days across New Year, each a
different power of two, so every bucket's total names exactly the days in it.

    Wed 2025-12-31   -1
    Thu 2026-01-01   -2
    Fri 2026-01-02   -4
    Sat 2026-01-03   -8
    Sun 2026-01-04  -16
    Mon 2026-01-05  -32
"""

from __future__ import annotations

from datetime import date as Date

import pytest
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.errors import ValidationError
from app.models import (
    Account,
    AccountType,
    BatchKind,
    Household,
    Transaction,
    User,
    WeekStart,
)
from app.services import agent_keys as key_service
from app.services import households as household_service
from app.services import insights
from tests.conftest import HEADERS, _setup_owner

SPENDS = [
    ("2025-12-31", -1),
    ("2026-01-01", -2),
    ("2026-01-02", -4),
    ("2026-01-03", -8),
    ("2026-01-04", -16),
    ("2026-01-05", -32),
]

#: Each week labelled by the date of its own first day, and the first week of
#: every one straddles 1 January -- whole, not cut at the year.
EXPECTED = {
    WeekStart.monday: [("2025-12-29", -1 - 2 - 4 - 8 - 16), ("2026-01-05", -32)],
    WeekStart.sunday: [("2025-12-28", -1 - 2 - 4 - 8), ("2026-01-04", -16 - 32)],
    WeekStart.saturday: [("2025-12-27", -1 - 2 - 4), ("2026-01-03", -8 - 16 - 32)],
}


def test_every_value_has_its_own_weekday():
    """No enum value without a behaviour: each is a different day."""
    days = {day.sqlite_weekday for day in WeekStart}
    assert len(days) == len(WeekStart)
    assert set(EXPECTED) == set(WeekStart)


# --------------------------------------------------------------------------- #
# The arithmetic, for each start day
# --------------------------------------------------------------------------- #


def _spend_in(session: Session, house: Household, actor: User, currency: str) -> None:
    with batch(session, kind=BatchKind.manual, actor_id=actor.id, household_id=house.id):
        account = Account(
            household_id=house.id, name="Checking", type=AccountType.checking, currency=currency
        )
        session.add(account)
        session.flush()
        for when, amount in SPENDS:
            session.add(Transaction(
                household_id=house.id, account_id=account.id,
                date=Date.fromisoformat(when), amount=amount,
            ))
    session.commit()


@pytest.mark.parametrize("start", list(WeekStart), ids=lambda s: s.value)
def test_a_week_straddling_new_year_is_whole_for_every_start_day(
    session, owner, member, household, other_household, start
):
    """Two households, the same spends, one set to `start` and one left on
    Monday: the first gets its own weeks, the second keeps today's."""
    with batch(session, kind=BatchKind.admin, actor_id=member.id, household_id=household.id):
        household_service.update_household(session, household, week_start=start.value)
    session.commit()
    _spend_in(session, household, owner, "EUR")
    _spend_in(session, other_household, member, "GBP")

    def weeks(house: Household, currency: str) -> list[tuple[str, int]]:
        answer = insights.timeseries(
            session, house.id, bucket=insights.Bucket.week,
            week_start=WeekStart(house.week_start),
        )
        return [(g.name, g.sum_minor) for g in answer[currency].groups]

    assert household.week_start == start.value
    assert other_household.week_start == "monday"
    assert weeks(household, "EUR") == EXPECTED[start]
    assert weeks(other_household, "GBP") == EXPECTED[WeekStart.monday]


def test_days_and_months_ignore_the_week_start(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        household_service.update_household(session, household, week_start="saturday")
    session.commit()
    _spend_in(session, household, owner, "EUR")

    months = insights.timeseries(
        session, household.id, bucket=insights.Bucket.month, week_start=WeekStart.saturday
    )
    assert [(g.name, g.sum_minor) for g in months["EUR"].groups] == [
        ("2025-12", -1), ("2026-01", -62),
    ]


def test_a_day_that_is_not_offered_is_refused_with_a_code(session, owner, household):
    with (
        batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id),
        pytest.raises(ValidationError) as refused,
    ):
        household_service.update_household(session, household, week_start="friday")
    assert refused.value.code == "household.unknown_week_start"
    assert refused.value.params == {"week_start": "friday"}
    assert household.week_start == "monday"


# --------------------------------------------------------------------------- #
# Over HTTP: the household screen and the agent
# --------------------------------------------------------------------------- #


def _invite_member(client, house_id: str) -> None:
    """Invite a plain member into `house_id`, and leave the client signed in as them."""
    from tests.test_invitations import _accept, _invite

    created = _invite(client, role="member", households=[house_id])
    client.cookies.clear()
    result = _accept(client, created["token"], email="partner@gmail.com", name="Partner")
    assert result["user"]["role"] == "member"


def test_a_member_who_is_not_the_owner_changes_the_week_start(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    assert house["week_start"] == "monday", "a new household starts on Monday"

    _invite_member(client, house["id"])
    changed = client.patch(
        f"/api/households/{house['id']}", json={"week_start": "sunday"}, headers=HEADERS
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["week_start"] == "sunday"
    assert client.get(f"/api/households/{house['id']}").json()["week_start"] == "sunday"


def test_an_unknown_day_over_http_is_a_coded_refusal(client):
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()

    refused = client.patch(
        f"/api/households/{house['id']}", json={"week_start": "Funday"}, headers=HEADERS
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "household.unknown_week_start"
    assert client.get(f"/api/households/{house['id']}").json()["week_start"] == "monday"


def _agent_token(client, house_id: str, user_id: str) -> str:
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, user_id)
        house = own.get(Household, house_id)
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=house.id):
            _key, token = key_service.issue(own, user=user, household=house, label="weekly")
        own.commit()
    return token


def test_the_agent_sees_each_households_own_weeks(client):
    """The issue's 'done when': a Sunday household gets Sunday-to-Saturday weeks
    in the agent's weekly totals, and a Monday one keeps its figures."""
    world = _setup_owner(client)
    houses = {}
    for name, start in (("Sunday house", "sunday"), ("Monday house", None)):
        house = client.post("/api/households", json={"name": name}, headers=HEADERS).json()
        if start:
            client.patch(f"/api/households/{house['id']}", json={"week_start": start}, headers=HEADERS)
        account = client.post(
            f"/api/households/{house['id']}/accounts",
            json={"name": "Checking", "type": "checking", "currency": "EUR"},
            headers=HEADERS,
        ).json()
        for when, amount in SPENDS:
            made = client.post(
                f"/api/households/{house['id']}/transactions",
                json={"account_id": account["id"], "date": when, "amount": amount,
                      "payee_name": "Corner shop"},
                headers=HEADERS,
            )
            assert made.status_code == 201, made.text
        houses[name] = house["id"]

    tokens = {name: _agent_token(client, hid, world["user"]["id"]) for name, hid in houses.items()}
    client.cookies.clear()

    def agent_get(name: str, path: str, **params):
        return client.get(
            f"/api/agent/v1{path}".replace("{hid}", houses[name]),
            params=params,
            headers={"authorization": f"Bearer {tokens[name]}"},
        )

    def weeks(name: str) -> list[tuple[str, int]]:
        body = agent_get(name, "/households/{hid}/timeseries", bucket="week").json()
        return [(g["name"], g["sum_minor"]) for g in body["by_currency"]["EUR"]["groups"]]

    assert weeks("Sunday house") == EXPECTED[WeekStart.sunday]
    assert weeks("Monday house") == EXPECTED[WeekStart.monday]

    manifest = agent_get("Sunday house", "/manifest").json()
    assert manifest["household"]["week_start"] == "sunday"
    assert agent_get("Monday house", "/manifest").json()["household"]["week_start"] == "monday"
    timeseries = next(e for e in manifest["endpoints"] if e["path"].endswith("/timeseries"))
    assert "week_start" in timeseries["says"]
