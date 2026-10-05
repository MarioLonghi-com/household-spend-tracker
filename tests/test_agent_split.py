"""A key dividing a row into the parts it was made of. #7.

A partial work claim -- a shared booking, a share of a bill -- is recorded by
splitting the row and keeping the work flag on one part. These tests hold the
agent door to the register's rules (through `transactions.split`), to one
batch and one undo, and to the decision that only a key a person trusted with
`may_commit` may do it, because a split replaces the row.
"""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select

from app.audit.batch import batch
from app.audit.guard import AuditedSession
from app.audit.undo import undo_batch
from app.models import (
    AgentScope,
    Batch,
    BatchKind,
    ClearedState,
    Household,
    Receipt,
    ReimbursementState,
    Transaction,
    User,
)
from app.services import agent_keys as key_service
from app.services import transactions as txn_service
from app.services import transfers as transfer_service
from tests import test_agent_links as _links
from tests.conftest import HEADERS
from tests.receipt_fixtures import as_bytes, receipt_image
from tests.test_agent_links import V1, _db, _get, _row

#: The links fixture: two households, three accounts in two currencies, and
#: an ordinary read key and write key.
world = _links.world


@pytest.fixture()
def trusted(world):
    """A third key, the one a person has trusted to apply changes unreviewed."""
    with _db(world) as own:
        user = own.get(User, world["user_id"])
        house = own.get(Household, world["house"]["id"])
        with batch(own, kind=BatchKind.admin, actor_id=user.id, household_id=house.id):
            _key, token = key_service.issue(
                own, user=user, household=house, label="bookkeeper",
                scope=AgentScope.write, may_commit=True,
            )
        own.commit()
    return token


def _split(world, token, splits, household=None):
    return world["client"].post(
        f"{V1}/households/{household or world['house']['id']}/transactions/split",
        json={"splits": splits},
        headers={"authorization": f"Bearer {token}", **HEADERS},
    )


def _parts(world, original_date, account):
    """Every row on that account and date, as (amount, category, flag)."""
    with _db(world) as own:
        rows = own.execute(
            select(Transaction).where(
                Transaction.account_id == world["accounts"][account],
                Transaction.date == original_date,
            )
        ).scalars().all()
        return sorted((r.amount, r.category_id, r.reimbursement) for r in rows)


def _category(world, name):
    tree = world["client"].get(
        f"/api/households/{world['house']['id']}/categories", headers=HEADERS
    ).json()
    return next(one["id"] for group in tree for one in group["categories"] if one["name"] == name)


def _flag(world, txn_id, state, settled_by=None):
    with _db(world) as own:
        with batch(own, kind=BatchKind.manual, actor_id=world["user_id"],
                   household_id=world["house"]["id"]):
            txn = own.get(Transaction, txn_id)
            txn_service.set_reimbursement(
                own, txn, state=state,
                **({"settled_by": own.get(Transaction, settled_by)} if settled_by else {}),
            )
        own.commit()


# --------------------------------------------------------------------------- #
# The door
# --------------------------------------------------------------------------- #


def test_an_ordinary_write_key_is_refused_and_the_row_is_untouched(world):
    row = _row(world, "santander", 3, -4000, "Coworking")
    refused = _split(world, world["tokens"]["write"], [{
        "transaction_id": row,
        "parts": [{"amount_minor": -2500}, {"amount_minor": -1500}],
    }])
    assert refused.status_code == 403, refused.text
    assert "trusted" in refused.json()["detail"], "the refusal says what to do instead"
    assert _get(world, row).amount == -4000


def test_a_read_key_is_refused(world, trusted):
    row = _row(world, "santander", 3, -4000, "Coworking")
    assert _split(world, world["tokens"]["read"], [{
        "transaction_id": row, "parts": [{"amount_minor": -1}, {"amount_minor": -3999}],
    }]).status_code == 403
    assert _get(world, row).amount == -4000


# --------------------------------------------------------------------------- #
# Doing it
# --------------------------------------------------------------------------- #


def test_two_rows_in_two_currencies_split_into_their_parts(world, trusted):
    """A decimal string read in the row's own currency, and minor units."""
    phone = _category(world, "Phone")
    eating = _category(world, "Eating Out")
    bill = _row(world, "santander", 3, -9805, "Telephone company")
    taxis = _row(world, "monzo", 4, -3083, "Ride share")

    answer = _split(world, trusted, [
        {"transaction_id": bill, "parts": [
            {"amount": "-85.29", "category_id": phone, "memo": "work share"},
            {"amount": "-12.76", "category_id": phone},
        ]},
        {"transaction_id": taxis, "parts": [
            {"amount_minor": -2408, "category_id": eating},
            {"amount_minor": -675},
        ]},
    ])
    assert answer.status_code == 200, answer.text
    did = answer.json()
    assert [one["transaction_id"] for one in did["split"]] == [bill, taxis]
    assert did["refused"] == [] and did["not_found"] == []

    assert _get(world, bill) is None, "a split replaces the row"
    assert _parts(world, _date(3), "santander") == sorted([(-8529, phone, None), (-1276, phone, None)])
    assert _parts(world, _date(4), "monzo") == sorted([(-2408, eating, None), (-675, None, None)])
    with _db(world) as own:
        first = own.get(Transaction, did["split"][0]["parts"][0])
        assert first.memo == "work share"
        assert first.split_id is not None
        assert own.get(Transaction, did["split"][0]["parts"][1]).split_id == first.split_id


def test_a_partial_claim_keeps_the_flag_on_the_work_part_only(world, trusted):
    shared = _row(world, "santander", 5, -54104, "Airline")
    _flag(world, shared, ReimbursementState.expected)

    did = _split(world, trusted, [{"transaction_id": shared, "parts": [
        {"amount": "-270.52"},
        {"amount": "-270.52", "reimbursement": "clear"},
    ]}]).json()

    work, personal = did["split"][0]["parts"]
    assert _get(world, work).reimbursement is ReimbursementState.expected
    assert _get(world, personal).reimbursement is None


def test_clearing_a_part_of_a_repaid_row_is_refused_and_the_rest_still_split(world, trusted):
    """Taking the flag off a part of a repaid row would take the link apart."""
    claim = _row(world, "santander", 6, -20000, "Hotel")
    repayment = _row(world, "santander", 7, 20000, "Employer")
    _flag(world, claim, ReimbursementState.expected, settled_by=repayment)
    other = _row(world, "monzo", 8, -1000, "Lunch")

    did = _split(world, trusted, [
        {"transaction_id": claim, "parts": [
            {"amount_minor": -15000}, {"amount_minor": -5000, "reimbursement": "clear"},
        ]},
        {"transaction_id": other, "parts": [{"amount_minor": -600}, {"amount_minor": -400}]},
    ]).json()

    assert [one["transaction_id"] for one in did["refused"]] == [claim]
    assert "paid back" in did["refused"][0]["reason"]
    assert _get(world, claim).reimbursed_by_id == repayment, "the link is where it was"
    assert [one["transaction_id"] for one in did["split"]] == [other]


def test_rows_the_register_would_refuse_are_named_with_its_reason(world, trusted):
    """Does not add up, a transfer leg, reconciled, too many decimals: each
    named, nothing half-done, and the good row still splits."""
    short = _row(world, "santander", 9, -1000, "Shop")
    out_leg = _row(world, "santander", 10, -5000, "To savings")
    in_leg = _row(world, "revolut", 10, 5000, "From current")
    locked = _row(world, "monzo", 11, -3000, "Bar")
    precise = _row(world, "santander", 12, -1000, "Kiosk")
    fine = _row(world, "monzo", 13, -2000, "Market")
    with _db(world) as own:
        with batch(own, kind=BatchKind.manual, actor_id=world["user_id"],
                   household_id=world["house"]["id"]):
            transfer_service.link(own, own.get(Transaction, out_leg), own.get(Transaction, in_leg))
            own.get(Transaction, locked).cleared = ClearedState.reconciled
        own.commit()

    did = _split(world, trusted, [
        {"transaction_id": short, "parts": [{"amount_minor": -600}, {"amount_minor": -300}]},
        {"transaction_id": out_leg, "parts": [{"amount_minor": -2500}, {"amount_minor": -2500}]},
        {"transaction_id": locked, "parts": [{"amount_minor": -1500}, {"amount_minor": -1500}]},
        {"transaction_id": precise, "parts": [{"amount": "-5.005"}, {"amount": "-4.995"}]},
        {"transaction_id": fine, "parts": [{"amount_minor": -1200}, {"amount_minor": -800}]},
        {"transaction_id": "not-a-row", "parts": [{"amount_minor": -1}, {"amount_minor": -1}]},
    ]).json()

    assert sorted(one["transaction_id"] for one in did["refused"]) == sorted(
        [short, out_leg, locked, precise]
    )
    assert all(one["reason"] for one in did["refused"]), "each says why"
    assert did["not_found"] == ["not-a-row"]
    assert [one["transaction_id"] for one in did["split"]] == [fine]
    for untouched, amount in ((short, -1000), (out_leg, -5000), (locked, -3000), (precise, -1000)):
        assert _get(world, untouched).amount == amount


def test_the_receipt_goes_on_every_part(world, trusted):
    row = _row(world, "santander", 14, -4000, "Supermarket")
    raw = as_bytes(receipt_image((70, 90)))
    stored = world["client"].post(
        f"{V1}/households/{world['house']['id']}/receipts",
        json={"filename": "till.jpg", "content_base64": base64.b64encode(raw).decode(),
              "transaction_id": row},
        headers={"authorization": f"Bearer {world['tokens']['write']}", **HEADERS},
    )
    assert stored.status_code == 201, stored.text

    did = _split(world, trusted, [{"transaction_id": row, "parts": [
        {"amount_minor": -3000}, {"amount_minor": -1000},
    ]}]).json()

    with _db(world) as own:
        on = {
            r.transaction_id for r in own.execute(select(Receipt)).scalars()
            if r.transaction_id
        }
    assert on == set(did["split"][0]["parts"])


def test_one_undo_puts_every_original_row_back(world, trusted):
    first = _row(world, "santander", 15, -9000, "Hardware")
    second = _row(world, "monzo", 16, -3000, "Books")

    did = _split(world, trusted, [
        {"transaction_id": first, "parts": [{"amount_minor": -6000}, {"amount_minor": -3000}]},
        {"transaction_id": second, "parts": [{"amount_minor": -1000}, {"amount_minor": -2000}]},
    ]).json()
    with _db(world) as own:
        made = own.get(Batch, did["batch_id"])
        assert made.agent_key_id is not None, "History says a program did it"

    engine = world["client"].app_module.db_engine
    with AuditedSession(bind=engine, expire_on_commit=False) as own:
        undo_batch(own, did["batch_id"], actor_id=world["user_id"])
        own.commit()

    assert _get(world, first).amount == -9000
    assert _get(world, second).amount == -3000
    for one in did["split"]:
        for part in one["parts"]:
            assert _get(world, part) is None, "the parts went with the undo"


def test_another_household_is_not_found_by_id_and_404_by_path(world, trusted):
    theirs = _row(world, "away", 17, -900, "Not yours", where="away")
    did = _split(world, trusted, [{"transaction_id": theirs, "parts": [
        {"amount_minor": -450}, {"amount_minor": -450},
    ]}]).json()
    assert did["not_found"] == [theirs]
    assert _get(world, theirs).amount == -900

    assert _split(world, trusted, [{"transaction_id": theirs, "parts": [
        {"amount_minor": -450}, {"amount_minor": -450},
    ]}], household=world["away"]["id"]).status_code == 404


def test_an_invented_category_refuses_the_whole_request(world, trusted):
    row = _row(world, "santander", 18, -1000, "Shop")
    answer = _split(world, trusted, [{"transaction_id": row, "parts": [
        {"amount_minor": -500, "category_id": "invented"}, {"amount_minor": -500},
    ]}])
    assert answer.status_code in (400, 422), answer.text
    assert _get(world, row).amount == -1000


def test_a_json_float_is_not_money(world, trusted):
    row = _row(world, "santander", 19, -1000, "Shop")
    answer = _split(world, trusted, [{"transaction_id": row, "parts": [
        {"amount": -5.0}, {"amount_minor": -500},
    ]}])
    assert answer.status_code == 422, answer.text
    assert _get(world, row).amount == -1000


def _date(day):
    from datetime import date

    return date(2026, 7, day)
