"""The opening balance and its date, read and changed from the account (#10).

Neither is a column. Both live on the opening-balance row -- a reconciled
transaction whose payee carries `SystemPayee.opening_balance` -- so every test
below asserts on that row, and on a balance computed from it, rather than on
the PATCH's 200. The date is the part that matters most and shows least: it
decides from which day the starting figure counts, which only
`balances?as_of=` on a day between the old date and the new one can see.

Two households, three accounts in three currencies, one of them yen, whose
minor unit is the yen itself -- so a figure that was scaled by a hundred
anywhere on the way through would be a different number, not a rounding.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.models import (
    AccountType,
    BatchKind,
    ClearedState,
    Household,
    HouseholdMember,
    Payee,
    Role,
    SystemPayee,
    Transaction,
    User,
)
from app.models.base import utcnow
from app.services import accounts as account_service
from app.services import agent_keys as key_service
from tests.conftest import HEADERS, _bootstrap_user, _setup_owner


@pytest.fixture()
def world(client):
    """EUR and JPY opened with a figure, GBP opened empty; one later row each.

    EUR: 1234.56 on 1 March, then -20.00 on 10 April
    JPY: 50000 on 1 March, then -1500 on 12 April
    GBP: nothing to start with, then -7.50 on 2 May
    """
    owner = _setup_owner(client)
    house = client.post(
        "/api/households", json={"name": "Home", "base_currency": "EUR"}, headers=HEADERS
    ).json()

    def account(name: str, currency: str, opening: int) -> dict:
        made = client.post(
            f"/api/households/{house['id']}/accounts",
            json={
                "name": name,
                "type": "checking",
                "currency": currency,
                "opening_balance": opening,
                "opening_date": "2026-03-01",
            },
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text
        return made.json()

    eur = account("Current", "EUR", 1234_56)
    jpy = account("Yen wallet", "JPY", 50_000)
    gbp = account("Pounds", "GBP", 0)

    for target, when, amount in (
        (eur, "2026-04-10", -20_00),
        (jpy, "2026-04-12", -1_500),
        (gbp, "2026-05-02", -7_50),
    ):
        made = client.post(
            f"/api/households/{house['id']}/transactions",
            json={"account_id": target["id"], "date": when, "amount": amount, "memo": "groceries"},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text

    # The balances route is the agent's, so the test holds a key for it.
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        user = own.get(User, owner["user"]["id"])
        house_row = own.get(Household, house["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=house_row.id):
            _key, token = key_service.issue(own, user=user, household=house_row, label="checks")
        own.commit()

    return {"house": house, "eur": eur, "jpy": jpy, "gbp": gbp, "token": token}


def _accounts(client, world) -> dict[str, dict]:
    listed = client.get(f"/api/households/{world['house']['id']}/accounts").json()
    return {one["currency"]: one for one in listed}


def _balance_on(client, world, currency: str, as_of: str) -> int:
    body = client.get(
        f"/api/agent/v1/households/{world['house']['id']}/balances",
        params={"as_of": as_of},
        headers={"authorization": f"Bearer {world['token']}"},
    ).json()
    return next(one["balance_minor"] for one in body["accounts"] if one["currency"] == currency)


def _row(client, transaction_id: str) -> Transaction | None:
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        row = own.get(Transaction, transaction_id)
        if row is not None:
            row.payee  # noqa: B018 - loaded while the session is open
        return row


def _patch(client, world, currency: str, **body):
    return client.patch(f"/api/accounts/{world[currency.lower()]['id']}", json=body, headers=HEADERS)


def _undo_latest(client, world) -> None:
    house = world["house"]["id"]
    batches = client.get(f"/api/households/{house}/batches?include_single_edits=true").json()
    latest = next(one for one in batches if one["kind"] == "manual" and one["status"] == "applied")
    undone = client.post(
        f"/api/households/{house}/batches/{latest['id']}/undo", json={}, headers=HEADERS
    )
    assert undone.status_code == 200, undone.text


# --------------------------------------------------------------------------- #
# Read
# --------------------------------------------------------------------------- #


def test_the_list_reads_the_opening_balance_off_its_row(client, world):
    accounts = _accounts(client, world)

    assert accounts["EUR"]["opening_balance"] == 1234_56
    assert accounts["EUR"]["opening_date"] == "2026-03-01"
    assert accounts["JPY"]["opening_balance"] == 50_000
    assert accounts["JPY"]["opening_date"] == "2026-03-01"
    # Opened empty: no row, so nothing to date and nothing to link to.
    assert accounts["GBP"]["opening_balance"] == 0
    assert accounts["GBP"]["opening_date"] is None
    assert accounts["GBP"]["opening_transaction_id"] is None

    # The id names the real row, in the right account, with the system mark.
    for currency, amount in (("EUR", 1234_56), ("JPY", 50_000)):
        row = _row(client, accounts[currency]["opening_transaction_id"])
        assert row.account_id == accounts[currency]["id"]
        assert row.amount == amount
        assert row.payee.system is SystemPayee.opening_balance

    # And the single-account route says the same as the list.
    one = client.get(f"/api/accounts/{world['jpy']['id']}").json()
    assert one["opening_transaction_id"] == accounts["JPY"]["opening_transaction_id"]


def test_the_opening_row_is_found_by_its_mark_not_its_name(client, world):
    """Renaming the payee changes what a person reads, not what the row is."""
    eur = _accounts(client, world)["EUR"]
    row = _row(client, eur["opening_transaction_id"])
    with Session(client.app_module.db_engine) as own:
        with batch(
            own, kind=BatchKind.manual, actor_id=_owner_id(own), household_id=world["house"]["id"]
        ):
            payee = own.get(Payee, row.payee_id)
            payee.name = "Starting figure"
            payee.name_folded = "starting figure"
        own.commit()

    assert _accounts(client, world)["EUR"]["opening_transaction_id"] == row.id
    assert _accounts(client, world)["EUR"]["opening_balance"] == 1234_56


def test_with_two_opening_rows_the_earliest_is_the_opening_balance(client, world):
    """Nothing stops a second; the earlier one is where the history starts."""
    eur = _accounts(client, world)["EUR"]
    original = _row(client, eur["opening_transaction_id"])
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        with batch(
            own, kind=BatchKind.manual, actor_id=_owner_id(own),
            household_id=world["house"]["id"],
        ):
            earlier = Transaction(
                household_id=world["house"]["id"],
                account_id=eur["id"],
                date=date(2026, 2, 1),
                amount=5_00,
                payee_id=original.payee_id,
                cleared=ClearedState.cleared,
            )
            own.add(earlier)
        own.commit()

    eur = _accounts(client, world)["EUR"]
    assert eur["opening_transaction_id"] == earlier.id
    assert eur["opening_date"] == "2026-02-01"
    assert eur["opening_balance"] == 5_00

    # An edit reaches that one and leaves the other where it was.
    assert _patch(client, world, "EUR", opening_balance=6_00).status_code == 200
    assert _row(client, earlier.id).amount == 6_00
    assert _row(client, original.id).amount == 1234_56


def _owner_id(session: Session) -> str:
    return session.execute(select(User.id).where(User.role == Role.owner)).scalar_one()


# --------------------------------------------------------------------------- #
# Write
# --------------------------------------------------------------------------- #


def test_moving_the_date_moves_the_day_the_figure_starts_counting(client, world):
    before = _accounts(client, world)
    # 15 March sits between the old opening date and the new one.
    assert _balance_on(client, world, "EUR", "2026-03-15") == 1234_56

    moved = _patch(client, world, "EUR", opening_date="2026-03-20")
    assert moved.status_code == 200, moved.text
    assert moved.json()["opening_date"] == "2026-03-20"

    row = _row(client, before["EUR"]["opening_transaction_id"])
    assert row.date == date(2026, 3, 20)
    assert row.amount == 1234_56, "a date alone leaves the figure alone"
    assert row.cleared is ClearedState.reconciled
    assert row.payee.system is SystemPayee.opening_balance

    assert _balance_on(client, world, "EUR", "2026-03-15") == 0
    assert _balance_on(client, world, "EUR", "2026-03-20") == 1234_56
    # The other account, in the other currency, did not move.
    assert _balance_on(client, world, "JPY", "2026-03-15") == 50_000


def test_changing_the_figure_changes_the_row_and_the_balance(client, world):
    before = _accounts(client, world)

    changed = _patch(client, world, "JPY", opening_balance=75_000)
    assert changed.status_code == 200, changed.text
    assert changed.json()["opening_balance"] == 75_000
    assert changed.json()["balance"] == 75_000 - 1_500

    row = _row(client, before["JPY"]["opening_transaction_id"])
    assert row.amount == 75_000
    assert row.date == date(2026, 3, 1), "a figure alone leaves the date alone"
    assert row.cleared is ClearedState.reconciled
    assert row.payee.system is SystemPayee.opening_balance
    assert _accounts(client, world)["EUR"]["opening_balance"] == 1234_56


def test_one_undo_puts_the_figure_the_date_and_the_name_back(client, world):
    before = _accounts(client, world)["EUR"]

    changed = _patch(
        client, world, "EUR", name="Current (old bank)",
        opening_balance=999_99, opening_date="2026-02-14",
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["opening_balance"] == 999_99

    _undo_latest(client, world)

    after = _accounts(client, world)["EUR"]
    assert after["name"] == "Current"
    assert after["opening_balance"] == 1234_56
    assert after["opening_date"] == "2026-03-01"
    row = _row(client, before["opening_transaction_id"])
    assert (row.amount, row.date, row.cleared) == (1234_56, date(2026, 3, 1), ClearedState.reconciled)


def test_a_zero_figure_removes_the_row_and_undo_brings_it_back(client, world):
    before = _accounts(client, world)["EUR"]

    emptied = _patch(client, world, "EUR", opening_balance=0)
    assert emptied.status_code == 200, emptied.text
    assert emptied.json()["opening_date"] is None
    assert emptied.json()["balance"] == -20_00
    assert _row(client, before["opening_transaction_id"]) is None

    _undo_latest(client, world)

    row = _row(client, before["opening_transaction_id"])
    assert (row.amount, row.date, row.cleared) == (1234_56, date(2026, 3, 1), ClearedState.reconciled)
    assert row.payee.system is SystemPayee.opening_balance


def test_a_date_in_the_future_is_refused_and_nothing_moves(client, world):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    refused = _patch(client, world, "EUR", opening_date=tomorrow, name="Renamed")
    assert refused.status_code == 422
    assert "future" in refused.json()["detail"]

    after = _accounts(client, world)["EUR"]
    assert after["opening_date"] == "2026-03-01"
    assert after["name"] == "Current", "the batch is all or nothing"


def test_a_date_alone_on_an_account_opened_empty_is_refused(client, world):
    refused = _patch(client, world, "GBP", opening_date="2026-04-01")

    assert refused.status_code == 422
    assert refused.json()["detail"] == account_service.NO_OPENING_TO_DATE
    assert _accounts(client, world)["GBP"]["opening_transaction_id"] is None


def test_a_figure_on_an_account_opened_empty_writes_the_row(client, world):
    """Dated at the account's earliest row when no date is sent."""
    made = _patch(client, world, "GBP", opening_balance=300_00)
    assert made.status_code == 200, made.text
    body = made.json()

    assert body["opening_balance"] == 300_00
    assert body["opening_date"] == "2026-05-02"
    assert body["balance"] == 300_00 - 7_50
    row = _row(client, body["opening_transaction_id"])
    assert row.cleared is ClearedState.reconciled
    assert row.payee.system is SystemPayee.opening_balance
    assert body["warnings"] == []


def test_a_figure_and_a_date_on_an_account_opened_empty_uses_the_date(client, world):
    made = _patch(client, world, "GBP", opening_balance=-45_00, opening_date="2026-01-31")
    assert made.status_code == 200, made.text

    assert made.json()["opening_date"] == "2026-01-31"
    assert _balance_on(client, world, "GBP", "2026-02-01") == -45_00
    # And the other household account in another currency is untouched.
    assert _balance_on(client, world, "EUR", "2026-02-01") == 0


def test_another_households_account_is_a_404_and_is_left_alone(client, world):
    with Session(client.app_module.db_engine, expire_on_commit=False) as own:
        stranger = _bootstrap_user(own, email="neighbour@example.com", name="Neighbour", role=Role.member)
        with batch(own, kind=BatchKind.admin, actor_id=stranger.id):
            theirs = Household(name="Next door", base_currency="GBP")
            own.add(theirs)
            own.flush()
            own.add(
                HouseholdMember(
                    household_id=theirs.id, user_id=stranger.id,
                    added_by_id=stranger.id, added_at=utcnow(),
                )
            )
            their_account = account_service.create_account(
                own, household=theirs, name="Theirs", type=AccountType.checking,
                opening_balance=10_00, opening_date=date(2026, 3, 1),
            )
        own.commit()
        account_id = their_account.id

    refused = client.patch(
        f"/api/accounts/{account_id}", json={"opening_balance": 1}, headers=HEADERS
    )
    assert refused.status_code == 404

    with Session(client.app_module.db_engine) as own:
        assert account_service.opening(own, account_id).amount == 10_00


# --------------------------------------------------------------------------- #
# The warning
# --------------------------------------------------------------------------- #


def test_a_date_after_the_earliest_row_is_saved_with_a_warning(client, world):
    moved = _patch(client, world, "EUR", opening_date="2026-04-20")

    assert moved.status_code == 200, moved.text
    assert moved.json()["opening_date"] == "2026-04-20"
    assert moved.json()["warnings"] == [
        account_service.OPENS_AFTER_ITS_ROWS.format(oldest="2026-04-10", opening="2026-04-20")
    ]
    # Said again wherever the account is read, since it is a fact about it.
    assert len(_accounts(client, world)["EUR"]["warnings"]) == 1
    assert _accounts(client, world)["JPY"]["warnings"] == []

    # And gone once the date is back before every other row.
    back = _patch(client, world, "EUR", opening_date="2026-04-01")
    assert back.json()["warnings"] == []


# --------------------------------------------------------------------------- #
# What the list costs
# --------------------------------------------------------------------------- #


def test_every_accounts_opening_balance_is_one_query(engine, session, owner, household, accounts):
    """The Accounts screen draws them all at once, so one query, not one each."""
    from tests.test_ledger_performance import statements

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        account_service.set_opening(
            session, accounts["checking"], amount=150_00, when=date(2026, 3, 1)
        )
        account_service.set_opening(
            session, accounts["pounds"], amount=-80_00, when=date(2026, 3, 2)
        )
    session.commit()

    with statements(engine) as seen:
        found = account_service.opening_for_household(session, household.id)

    assert len(seen) == 1, seen
    assert {key: (one.amount, one.date) for key, one in found.items()} == {
        accounts["checking"].id: (150_00, date(2026, 3, 1)),
        accounts["pounds"].id: (-80_00, date(2026, 3, 2)),
    }
