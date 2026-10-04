"""A transfer leg has no category, through every door (#124).

Linking already cleared the category, because a transfer is not spending. What
it did not do was stop one being set again afterwards -- from the panel, the
register's cell, the bulk "Set the category..." or the API -- and a categorised
leg then counted as spending in every report.

Every test here asserts what ended up in the column, not just the status: a
422 that still wrote the category would pass a status check.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.batch import batch
from app.errors import ValidationError
from app.models import BatchKind, Transaction
from app.services import categories as category_service
from app.services import transactions as txn_service
from app.services import transfers
from tests.conftest import HEADERS
from tests.test_api import _with_categories

DAY = date(2026, 3, 24)


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


@pytest.fixture()
def pair(session, owner, household, accounts):
    """A same-currency transfer (EUR to EUR) and a cross-currency one (EUR to GBP)."""
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        same = txn_service.create_transfer(
            session, source=accounts["checking"], destination=accounts["card"],
            date=DAY, amount=5_000,
        )
        across = txn_service.create_transfer(
            session, source=accounts["checking"], destination=accounts["pounds"],
            date=DAY, amount=10_000, to_amount=8_600,
        )
    return {"same": same, "across": across}


@pytest.fixture()
def two_categories(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        return [
            category_service.ensure(session, household.id, "Groceries", group_name="Everyday"),
            category_service.ensure(session, household.id, "Fuel", group_name="Transport"),
        ]


def test_the_service_refuses_a_category_on_either_leg_of_either_transfer(
    session, owner, household, pair, two_categories
):
    for kind in ("same", "across"):
        for leg in pair[kind]:
            with pytest.raises(ValidationError, match="a transfer has no category"), batch(
                session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
            ):
                txn_service.update(session, leg, category=two_categories[0])
            session.refresh(leg)
            assert leg.category_id is None, f"{kind} leg was categorised anyway"


def test_emptying_a_legs_category_is_still_allowed(session, owner, household, pair):
    """A leg carrying a category from before this rule has to be fixable."""
    leg = pair["same"][0]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn_service.update(session, leg, category=None, memo="moved to the card")
    session.refresh(leg)
    assert leg.category_id is None
    assert leg.memo == "moved to the card"


def test_unlinking_makes_the_category_editable_again(
    session, owner, household, accounts, two_categories
):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        out = txn_service.create(session, account=accounts["checking"], date=DAY, amount=-2_500)
        into = txn_service.create(session, account=accounts["card"], date=DAY, amount=2_500)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        transfers.link(session, out, into)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        transfers.unlink(session, out)

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn_service.update(session, out, category=two_categories[0])
        txn_service.update(session, into, category=two_categories[1])
    session.refresh(out)
    session.refresh(into)
    assert out.category_id == two_categories[0].id
    assert into.category_id == two_categories[1].id


# --------------------------------------------------------------------------- #
# Over HTTP: the PATCH and the bulk edit
# --------------------------------------------------------------------------- #


def _linked_world(client):
    """Two accounts, two ordinary rows, and one linked transfer between them."""
    world = _with_categories(client)
    house, checking, card = world["house"], world["checking"]["id"], world["card"]["id"]
    base = f"/api/households/{house}"

    def row(account, amount, payee):
        made = client.post(
            f"{base}/transactions",
            json={"account_id": account, "date": "2026-03-24", "amount": amount,
                  "payee_name": payee},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text
        return made.json()["id"]

    ordinary = [row(checking, -1_200, "Shop A"), row(card, -3_400, "Shop B")]
    out, into = row(checking, -5_000, "Move"), row(card, 5_000, "Move in")
    linked = client.post(
        f"{base}/transfers/link",
        json={"pairs": [{"first_id": out, "second_id": into}]},
        headers=HEADERS,
    )
    assert linked.json() == {"linked": 1}, linked.text
    return {**world, "base": base, "ordinary": ordinary, "legs": [out, into]}


def _category_of(client, base, txn_id):
    rows = client.get(f"{base}/transactions", headers=HEADERS).json()["transactions"]
    return next(one["category_id"] for one in rows if one["id"] == txn_id)


def test_the_patch_answers_422_on_a_transfer_leg_and_writes_nothing(client):
    world = _linked_world(client)
    groceries = world["cat"]["Groceries"]["id"]

    for leg in world["legs"]:
        refused = client.patch(
            f"/api/transactions/{leg}", json={"category_id": groceries}, headers=HEADERS
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == "a transfer has no category"
        assert _category_of(client, world["base"], leg) is None


def test_the_patch_still_edits_a_legs_memo(client):
    """The refusal is about the category, not about the row."""
    world = _linked_world(client)
    leg = world["legs"][0]
    edited = client.patch(
        f"/api/transactions/{leg}", json={"memo": "rent top-up", "clear_category": True},
        headers=HEADERS,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["memo"] == "rent top-up"
    assert edited.json()["category_id"] is None


def test_a_bulk_category_skips_the_legs_categorises_the_rest_and_counts_them(client):
    world = _linked_world(client)
    groceries = world["cat"]["Groceries"]["id"]
    everything = world["ordinary"] + world["legs"]

    answer = client.post(
        f"{world['base']}/transactions/bulk",
        json={"transaction_ids": everything, "category_id": groceries},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["skipped_transfer_legs"] == 2

    for txn in world["ordinary"]:
        assert _category_of(client, world["base"], txn) == groceries
    for leg in world["legs"]:
        assert _category_of(client, world["base"], leg) is None


def test_a_bulk_edit_still_clears_the_legs_it_skipped_the_category_on(client):
    """Skipping the category is not skipping the row: the rest of the edit lands."""
    world = _linked_world(client)
    groceries = world["cat"]["Groceries"]["id"]

    answer = client.post(
        f"{world['base']}/transactions/bulk",
        json={"transaction_ids": world["legs"], "category_id": groceries, "cleared": "cleared"},
        headers=HEADERS,
    ).json()
    assert answer["skipped_transfer_legs"] == 2
    assert {one["cleared"] for one in answer["transactions"]} == {"cleared"}
    assert {one["category_id"] for one in answer["transactions"]} == {None}


def test_a_bulk_category_over_a_locked_leg_does_not_sink_the_selection(client):
    """A leg with nothing left to change is not touched, so its lock is not tested."""
    world = _linked_world(client)
    groceries = world["cat"]["Groceries"]["id"]
    locked = client.patch(
        f"/api/transactions/{world['legs'][0]}", json={"cleared": "reconciled"}, headers=HEADERS
    )
    assert locked.status_code == 200, locked.text

    answer = client.post(
        f"{world['base']}/transactions/bulk",
        json={"transaction_ids": world["ordinary"] + [world["legs"][0]],
              "category_id": groceries},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["skipped_transfer_legs"] == 1
    for txn in world["ordinary"]:
        assert _category_of(client, world["base"], txn) == groceries


def test_a_bulk_that_only_empties_categories_skips_nothing(client):
    world = _linked_world(client)
    answer = client.post(
        f"{world['base']}/transactions/bulk",
        json={"transaction_ids": world["ordinary"] + world["legs"], "clear_category": True},
        headers=HEADERS,
    ).json()
    assert answer["skipped_transfer_legs"] == 0
    assert {one["category_id"] for one in answer["transactions"]} == {None}


def test_after_unlinking_the_patch_sets_a_category_again(client):
    world = _linked_world(client)
    groceries = world["cat"]["Groceries"]["id"]
    leg = world["legs"][1]

    gone = client.post(f"/api/transactions/{leg}/unlink", headers=HEADERS)
    assert gone.status_code == 204, gone.text

    answer = client.patch(
        f"/api/transactions/{leg}", json={"category_id": groceries}, headers=HEADERS
    )
    assert answer.status_code == 200, answer.text
    assert _category_of(client, world["base"], leg) == groceries


def test_no_transfer_leg_in_the_ledger_ends_up_categorised(client):
    """The rule stated as a fact about the table, after every door was tried."""
    world = _linked_world(client)
    groceries = world["cat"]["Groceries"]["id"]
    for leg in world["legs"]:
        client.patch(f"/api/transactions/{leg}", json={"category_id": groceries}, headers=HEADERS)
    client.post(
        f"{world['base']}/transactions/bulk",
        json={"transaction_ids": world["legs"], "category_id": groceries},
        headers=HEADERS,
    )

    with Session(client.app_module.db_engine) as own:
        categorised_legs = own.execute(
            select(Transaction.id).where(
                Transaction.transfer_account_id.is_not(None),
                Transaction.category_id.is_not(None),
            )
        ).all()
    assert categorised_legs == []
