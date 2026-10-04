"""Work expenses, and the payments that paid them back (spec Part 11).

Two currencies and two households throughout: a GBP taxi beside a EUR hotel,
because one currency is how three of discovery's reports added EUR to GBP, and
a household the owner is not in, because another household's payment is a 404
and not a refusal. Every test asserts the columns themselves -- a 200 or a
raised error on its own says nothing about what the row now holds.

The report's own tests (T22-T26, T30) live beside the report.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import Conflict, NoOpenBatch, NotFound, ValidationError
from app.models import (
    Account,
    AccountType,
    BatchKind,
    Change,
    ChangeOp,
    ClearedState,
    ReimbursementState,
    Transaction,
)
from app.services import describing
from app.services import transactions as txn_service
from tests.conftest import HEADERS

EXPECTED = ReimbursementState.expected
WRITTEN_OFF = ReimbursementState.written_off

HOTEL_DAY = date(2026, 9, 10)
PAID_DAY = date(2026, 9, 30)


@pytest.fixture()
def write(session, owner, household):
    def _open(kind=BatchKind.manual):
        return batch(session, kind=kind, actor_id=owner.id, household_id=household.id)

    return _open


@pytest.fixture()
def claim(session, accounts, write):
    """A hotel and a dinner on the card, a taxi in pounds, and a payment into
    Checking -- plus an ordinary purchase nobody will claim."""
    with write():
        made = {
            "hotel": txn_service.create(
                session, account=accounts["card"], date=HOTEL_DAY, amount=-24_000
            ),
            "dinner": txn_service.create(
                session, account=accounts["card"], date=HOTEL_DAY, amount=-8_450
            ),
            "taxi": txn_service.create(
                session, account=accounts["pounds"], date=HOTEL_DAY, amount=-2_340
            ),
            "payment": txn_service.create(
                session, account=accounts["checking"], date=PAID_DAY, amount=32_450
            ),
            "groceries": txn_service.create(
                session, account=accounts["checking"], date=HOTEL_DAY, amount=-5_000
            ),
        }
    return made


@pytest.fixture()
def elsewhere(session, member, other_household):
    """Money in, in a household the owner is not a member of."""
    with batch(session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id):
        account = Account(
            household_id=other_household.id, name="Theirs", type=AccountType.checking,
            currency="GBP",
        )
        session.add(account)
        session.flush()
        return txn_service.create(session, account=account, date=PAID_DAY, amount=32_450)


def _outstanding(session, household) -> int:
    """The figure the report calls outstanding, summed the way it will be."""
    return session.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.household_id == household.id,
            Transaction.reimbursement == EXPECTED,
            Transaction.reimbursed_by_id.is_(None),
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# The columns
# --------------------------------------------------------------------------- #


def test_t3_a_fresh_row_is_not_a_work_expense_and_not_repaid(session, claim):
    for row in claim.values():
        session.refresh(row)
        assert row.reimbursement is None
        assert row.reimbursed_by_id is None


# --------------------------------------------------------------------------- #
# The guards
# --------------------------------------------------------------------------- #


def test_t4_money_coming_in_cannot_be_flagged(session, claim, write):
    payment = claim["payment"]
    with pytest.raises(ValidationError, match="only money leaving an account"), write():
        txn_service.set_reimbursement(session, payment, state=EXPECTED)
    session.refresh(payment)
    assert payment.reimbursement is None


def test_t5_a_transfer_leg_cannot_be_flagged(session, accounts, write):
    with write():
        out_leg, in_leg = txn_service.create_transfer(
            session, source=accounts["checking"], destination=accounts["card"],
            date=HOTEL_DAY, amount=10_000,
        )
    for leg in (out_leg, in_leg):
        with pytest.raises(ValidationError, match="one leg of a transfer"), write():
            txn_service.set_reimbursement(session, leg, state=EXPECTED)
        session.refresh(leg)
        assert leg.reimbursement is None


def test_t6_another_households_payment_is_not_found(session, claim, elsewhere, write):
    hotel = claim["hotel"]
    with pytest.raises(NotFound, match="no such transaction"), write():
        txn_service.set_reimbursement(session, hotel, settled_by=elsewhere)
    session.refresh(hotel)
    assert hotel.reimbursed_by_id is None
    assert hotel.reimbursement is None


def test_t7_a_written_off_row_cannot_be_linked(session, claim, write):
    hotel = claim["hotel"]
    with write():
        txn_service.set_reimbursement(session, hotel, state=WRITTEN_OFF)
    with pytest.raises(Conflict, match="written off"), write():
        txn_service.set_reimbursement(session, hotel, settled_by=claim["payment"])
    session.refresh(hotel)
    assert hotel.reimbursed_by_id is None
    assert hotel.reimbursement is WRITTEN_OFF


def test_t7_nor_can_writing_off_leave_a_link_behind(session, claim, write):
    """The same impossible state, reached from the other side."""
    hotel, payment = claim["hotel"], claim["payment"]
    with write():
        txn_service.set_reimbursement(session, hotel, settled_by=payment)
    with pytest.raises(Conflict, match="paid back"), write():
        txn_service.set_reimbursement(session, hotel, state=WRITTEN_OFF)
    session.refresh(hotel)
    assert (hotel.reimbursement, hotel.reimbursed_by_id) == (EXPECTED, payment.id)

    # Taking the payment off in the same act is what makes it allowed.
    with write():
        txn_service.set_reimbursement(session, hotel, state=WRITTEN_OFF, settled_by=None)
    session.refresh(hotel)
    assert (hotel.reimbursement, hotel.reimbursed_by_id) == (WRITTEN_OFF, None)


def test_t7_linking_and_writing_off_in_one_call_is_refused(session, claim, write):
    hotel = claim["hotel"]
    with pytest.raises(Conflict), write():
        txn_service.set_reimbursement(
            session, hotel, state=WRITTEN_OFF, settled_by=claim["payment"]
        )
    session.refresh(hotel)
    assert (hotel.reimbursement, hotel.reimbursed_by_id) == (None, None)


def test_t8_clearing_the_state_clears_the_link(session, claim, write):
    hotel, payment = claim["hotel"], claim["payment"]
    with write():
        txn_service.set_reimbursement(session, hotel, settled_by=payment)
    session.refresh(hotel)
    assert (hotel.reimbursement, hotel.reimbursed_by_id) == (EXPECTED, payment.id)

    with write():
        txn_service.set_reimbursement(session, hotel, state=None)
    session.refresh(hotel)
    assert hotel.reimbursement is None
    assert hotel.reimbursed_by_id is None


def test_clearing_the_state_while_naming_a_payment_is_refused(session, claim, write):
    hotel = claim["hotel"]
    with pytest.raises(ValidationError, match="not a work expense"), write():
        txn_service.set_reimbursement(session, hotel, state=None, settled_by=claim["payment"])
    session.refresh(hotel)
    assert (hotel.reimbursement, hotel.reimbursed_by_id) == (None, None)


def test_t9_a_work_expense_cannot_be_what_paid_one_back(session, claim, write):
    hotel, dinner = claim["hotel"], claim["dinner"]
    with write():
        txn_service.set_reimbursement(session, dinner, state=EXPECTED)
    with pytest.raises(ValidationError, match="itself a work expense"), write():
        txn_service.set_reimbursement(session, hotel, settled_by=dinner)
    session.refresh(hotel)
    assert hotel.reimbursed_by_id is None


@pytest.mark.parametrize(
    ("payment_key", "sentence"),
    [
        ("hotel", "cannot reimburse itself"),
        ("groceries", "money arriving"),
    ],
)
def test_a_payment_must_be_money_in_and_not_the_row_itself(
    session, claim, write, payment_key, sentence
):
    hotel = claim["hotel"]
    with pytest.raises(ValidationError, match=sentence), write():
        txn_service.set_reimbursement(session, hotel, settled_by=claim[payment_key])
    session.refresh(hotel)
    assert hotel.reimbursed_by_id is None


def test_a_transfer_is_not_a_reimbursement(session, claim, accounts, write):
    with write():
        _, in_leg = txn_service.create_transfer(
            session, source=accounts["card"], destination=accounts["checking"],
            date=PAID_DAY, amount=32_450,
        )
    hotel = claim["hotel"]
    with pytest.raises(ValidationError, match="not a reimbursement"), write():
        txn_service.set_reimbursement(session, hotel, settled_by=in_leg)
    session.refresh(hotel)
    assert hotel.reimbursed_by_id is None


def test_t10_linking_an_unflagged_row_flags_it(session, claim, write):
    hotel, payment = claim["hotel"], claim["payment"]
    assert hotel.reimbursement is None
    with write():
        txn_service.set_reimbursement(session, hotel, settled_by=payment)
    session.refresh(hotel)
    assert hotel.reimbursement is EXPECTED
    assert hotel.reimbursed_by_id == payment.id


def test_an_advance_links_to_an_expense_dated_after_it(session, accounts, write):
    """No date rule anywhere: the payment may come first."""
    with write():
        advance = txn_service.create(
            session, account=accounts["checking"], date=date(2026, 8, 1), amount=50_000
        )
        later = txn_service.create(
            session, account=accounts["card"], date=date(2026, 8, 20), amount=-34_000
        )
        txn_service.set_reimbursement(session, later, settled_by=advance)
    session.refresh(later)
    assert later.reimbursed_by_id == advance.id


def test_a_gbp_expense_can_be_repaid_in_euros(session, claim, write):
    """Cross-currency is allowed; the report is what declines to net it."""
    taxi, payment = claim["taxi"], claim["payment"]
    with write():
        txn_service.set_reimbursement(session, taxi, settled_by=payment)
    session.refresh(taxi)
    assert taxi.reimbursed_by_id == payment.id


def test_taking_the_payment_off_leaves_the_row_a_work_expense(session, claim, write):
    hotel, payment = claim["hotel"], claim["payment"]
    with write():
        txn_service.set_reimbursement(session, hotel, settled_by=payment)
    with write():
        txn_service.set_reimbursement(session, hotel, settled_by=None)
    session.refresh(hotel)
    assert (hotel.reimbursement, hotel.reimbursed_by_id) == (EXPECTED, None)


# --------------------------------------------------------------------------- #
# The link, and what breaks it
# --------------------------------------------------------------------------- #


def test_t11_six_expenses_point_at_one_payment_and_make_one_claim(
    session, accounts, household, write
):
    with write():
        nights = [
            txn_service.create(
                session, account=accounts["card"], date=date(2026, 9, day), amount=-8_000
            )
            for day in range(1, 7)
        ]
        payment = txn_service.create(
            session, account=accounts["checking"], date=PAID_DAY, amount=48_000
        )
        txn_service.link_reimbursements(session, nights, payment)

    claims = session.execute(
        select(Transaction.reimbursed_by_id, func.count(), func.sum(Transaction.amount))
        .where(Transaction.household_id == household.id, Transaction.reimbursed_by_id.is_not(None))
        .group_by(Transaction.reimbursed_by_id)
    ).all()
    assert claims == [(payment.id, 6, -48_000)]
    session.refresh(payment)
    assert sorted(one.id for one in payment.reimburses) == sorted(one.id for one in nights)
    assert {one.reimbursement for one in nights} == {EXPECTED}


def test_linking_many_is_all_or_nothing_and_names_the_row(session, claim, write):
    hotel, dinner, payment = claim["hotel"], claim["dinner"], claim["payment"]
    with write():
        txn_service.set_reimbursement(session, dinner, state=WRITTEN_OFF)
    with pytest.raises(Conflict) as refused, write():
        txn_service.link_reimbursements(session, [hotel, dinner], payment)
    assert "€84.50 in Visa on 2026-09-10" in str(refused.value)
    assert "written off" in str(refused.value)
    session.refresh(hotel)
    session.refresh(dinner)
    assert (hotel.reimbursement, hotel.reimbursed_by_id) == (None, None)
    assert (dinner.reimbursement, dinner.reimbursed_by_id) == (WRITTEN_OFF, None)


def test_t12_deleting_the_payment_unlinks_every_expense_where_the_log_sees_it(
    session, claim, write
):
    """Trap #1: SQLite's own SET NULL would leave the log empty."""
    hotel, dinner, taxi, payment = (
        claim["hotel"], claim["dinner"], claim["taxi"], claim["payment"]
    )
    expenses = [hotel, dinner, taxi]
    with write():
        txn_service.link_reimbursements(session, expenses, payment)
    payment_id = payment.id
    with write() as removed:
        txn_service.delete(session, payment)
    session.expire_all()

    assert session.get(Transaction, payment_id) is None
    for expense in expenses:
        row = session.get(Transaction, expense.id)
        assert row.reimbursed_by_id is None
        assert row.reimbursement is EXPECTED, "back to outstanding, still a work expense"

    unlinked = session.execute(
        select(Change).where(Change.batch_id == removed.id, Change.op == ChangeOp.update)
    ).scalars().all()
    assert sorted(one.row_id for one in unlinked) == sorted(one.id for one in expenses)
    for change in unlinked:
        assert change.before["reimbursed_by_id"] == payment_id
        assert change.after["reimbursed_by_id"] is None


def test_t13_undoing_the_delete_brings_back_the_payment_and_every_link(
    session, claim, owner, household, write
):
    expenses = [claim["hotel"], claim["dinner"], claim["taxi"]]
    payment = claim["payment"]
    with write():
        txn_service.link_reimbursements(session, expenses, payment)
    payment_id = payment.id
    with write() as removed:
        txn_service.delete(session, payment)
    session.commit()

    with batch(session, kind=BatchKind.undo, actor_id=owner.id, household_id=household.id):
        undo_batch(session, removed.id, actor_id=owner.id)
    session.expire_all()

    back = session.get(Transaction, payment_id)
    assert back is not None
    assert back.amount == 32_450
    for expense in expenses:
        assert session.get(Transaction, expense.id).reimbursed_by_id == payment_id


def test_t14_deleting_an_expense_leaves_the_payment_alone(session, claim, write):
    hotel, dinner, payment = claim["hotel"], claim["dinner"], claim["payment"]
    with write():
        txn_service.link_reimbursements(session, [hotel, dinner], payment)
    with write():
        txn_service.delete(session, hotel)
    session.expire_all()
    still = session.get(Transaction, payment.id)
    assert still.amount == 32_450
    assert [one.id for one in still.reimburses] == [dinner.id]


# --------------------------------------------------------------------------- #
# The other write paths
# --------------------------------------------------------------------------- #


def test_t15_splitting_a_repaid_expense_carries_both_columns_to_every_part(
    session, claim, write
):
    hotel, payment = claim["hotel"], claim["payment"]
    with write():
        txn_service.set_reimbursement(session, hotel, settled_by=payment)
    with write(BatchKind.split):
        parts = txn_service.split(
            session, hotel,
            [txn_service.SplitPart(amount=-20_000), txn_service.SplitPart(amount=-4_000)],
        )
    for part in parts:
        session.refresh(part)
        assert part.reimbursement is EXPECTED
        assert part.reimbursed_by_id == payment.id


def test_t16_splitting_a_flagged_expense_leaves_outstanding_unchanged(
    session, claim, household, write
):
    hotel = claim["hotel"]
    with write():
        txn_service.set_reimbursement(session, hotel, state=EXPECTED)
    before = _outstanding(session, household)
    assert before == -24_000

    with write(BatchKind.split):
        txn_service.split(
            session, hotel,
            [txn_service.SplitPart(amount=-20_000), txn_service.SplitPart(amount=-4_000)],
        )
    assert _outstanding(session, household) == before


def test_t17_a_duplicate_is_a_work_expense_that_nothing_has_repaid(session, claim, write):
    hotel, payment = claim["hotel"], claim["payment"]
    with write():
        txn_service.set_reimbursement(session, hotel, settled_by=payment)
        copy = txn_service.duplicate(session, hotel, date=date(2026, 10, 10))
    session.refresh(copy)
    assert copy.reimbursement is EXPECTED
    assert copy.reimbursed_by_id is None


def test_t18_an_import_sets_neither_column_and_an_absorbed_twin_keeps_its_own(
    session, owner, household, accounts, write
):
    from tests.test_importing import SANTANDER, _commit, _stage

    checking = accounts["checking"]
    with write():
        # The €45.20 the statement will absorb, flagged by hand beforehand.
        typed = txn_service.create(
            session, account=checking, date=date(2026, 1, 4), amount=-4_520
        )
        txn_service.set_reimbursement(session, typed, state=EXPECTED)

    staged, _ = _stage(session, owner, household, checking, SANTANDER)
    result = _commit(session, owner, household, checking, staged)
    assert (result["created"], result["absorbed"]) == (2, 1)

    session.expire_all()
    rows = session.execute(
        select(Transaction).where(Transaction.account_id == checking.id)
    ).scalars().all()
    assert len(rows) == 3
    for row in rows:
        assert row.import_id is not None
        assert row.reimbursed_by_id is None
        if row.id == typed.id:
            assert row.reimbursement is EXPECTED, "the import took the person's flag off"
        else:
            assert row.reimbursement is None, "the import guessed a work expense"


def test_t19_a_reconciled_row_can_still_be_flagged_and_linked(session, accounts, claim, write):
    with write():
        locked = txn_service.create(
            session, account=accounts["card"], date=HOTEL_DAY, amount=-9_900,
            cleared=ClearedState.reconciled,
        )
    # The ordinary edit path refuses it, which is the point of the exception.
    with pytest.raises(Conflict, match="locked"), write():
        txn_service.update(session, locked, memo="hotel")
    with write():
        txn_service.set_reimbursement(session, locked, settled_by=claim["payment"])
    session.refresh(locked)
    assert (locked.reimbursement, locked.reimbursed_by_id) == (EXPECTED, claim["payment"].id)
    assert locked.cleared is ClearedState.reconciled


def test_t20_update_can_still_unlock_a_flagged_reconciled_row(session, accounts, write):
    with write():
        locked = txn_service.create(
            session, account=accounts["card"], date=HOTEL_DAY, amount=-9_900,
            cleared=ClearedState.reconciled,
        )
        txn_service.set_reimbursement(session, locked, state=EXPECTED)
    with write():
        txn_service.update(session, locked, cleared=ClearedState.cleared)
    session.refresh(locked)
    assert locked.cleared is ClearedState.cleared
    assert locked.reimbursement is EXPECTED


def test_t21_a_flag_outside_a_batch_is_refused(session, claim):
    hotel = claim["hotel"]
    with pytest.raises(NoOpenBatch):
        txn_service.set_reimbursement(session, hotel, state=EXPECTED)
    session.rollback()
    session.refresh(hotel)
    assert hotel.reimbursement is None


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def test_t31_flagging_reads_as_a_work_expense_not_a_column(session, claim, write):
    with write() as flagged:
        txn_service.set_reimbursement(session, claim["hotel"], state=EXPECTED)
    detail = describing.describe(session, flagged).detail
    assert "work expense not a work expense → expected" in detail, detail
    assert "reimbursement" not in detail

    with write() as off:
        txn_service.set_reimbursement(session, claim["hotel"], state=WRITTEN_OFF)
    detail = describing.describe(session, off).detail
    assert "expected → written off" in detail, detail
    assert "written_off" not in detail


def test_t32_linking_names_the_payments_amount_account_and_date(session, claim, write):
    payment = claim["payment"]
    with write() as linked:
        txn_service.set_reimbursement(session, claim["hotel"], settled_by=payment)
    detail = describing.describe(session, linked).detail
    assert "reimbursed by not yet reimbursed → €324.50 into Checking on 30 Sep 2026" in detail, (
        detail
    )
    assert payment.id not in detail


def test_a_removed_payment_reads_as_removed_not_as_an_id(session, claim, write):
    payment = claim["payment"]
    with write():
        txn_service.set_reimbursement(session, claim["hotel"], settled_by=payment)
    payment_id = payment.id
    with write() as removed:
        txn_service.delete(session, payment)
    names = describing.names_for(session, claim["hotel"].household_id)
    assert names.value("reimbursed_by_id", payment_id) == "a transaction since removed"
    changes = session.execute(
        select(Change).where(Change.batch_id == removed.id)
    ).scalars().all()
    lines = describing.describe_changes(session, claim["hotel"].household_id, changes)
    said = [one for one in lines.values() if "reimbursed by" in one]
    assert said == [
        "-€240.00 · in Visa: reimbursed by a transaction since removed → not yet reimbursed"
    ], lines


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


@pytest.fixture()
def world(client):
    """A household with Checking, Visa and a GBP account, over the real API."""
    from tests.test_api import _household_with_accounts

    made = _household_with_accounts(client)
    house = made["household"]["id"]
    made["pounds"] = client.post(
        f"/api/households/{house}/accounts",
        json={"name": "UK", "type": "savings", "currency": "GBP"},
        headers=HEADERS,
    ).json()
    made["base"] = f"/api/households/{house}"
    return made


def _post(client, world, account: str, amount: int, day: str = "2026-09-10", **extra) -> dict:
    made = client.post(
        f"{world['base']}/transactions",
        json={"account_id": world[account]["id"], "date": day, "amount": amount, **extra},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    return made.json()


def _patch(client, txn_id: str, **body):
    return client.patch(f"/api/transactions/{txn_id}/reimbursement", json=body, headers=HEADERS)


def _rows(client, world, **params) -> dict[str, dict]:
    page = client.get(f"{world['base']}/transactions", params=params).json()
    return {row["id"]: row for row in page["transactions"]}


def _work(client, world, txn_id: str) -> tuple:
    row = _rows(client, world)[txn_id]
    return row["reimbursement"], row["reimbursed_by_id"]


def test_t29_the_row_and_the_patch_reply_carry_both_fields(client, world):
    """Values, not keys: an index appended in the wrong place in `_row_dicts`
    would swap two fields and still have every key."""
    hotel = _post(client, world, "card", -24_000)
    payment = _post(client, world, "checking", 32_450, "2026-09-30")
    assert (hotel["reimbursement"], hotel["reimbursed_by_id"]) == (None, None)

    replied = _patch(client, hotel["id"], settled_by_id=payment["id"])
    assert replied.status_code == 200, replied.text
    assert (replied.json()["reimbursement"], replied.json()["reimbursed_by_id"]) == (
        "expected", payment["id"],
    )
    row = _rows(client, world)[hotel["id"]]
    assert row["reimbursement"] == "expected"
    assert row["reimbursed_by_id"] == payment["id"]
    # The columns either side are where they were.
    assert row["split_id"] is None and row["import_id"] is None
    assert row["amount"] == -24_000 and row["currency"] == "EUR"


def test_the_patch_flags_writes_off_unlinks_and_clears(client, world):
    hotel = _post(client, world, "card", -24_000)
    payment = _post(client, world, "checking", 24_000, "2026-09-30")

    assert _patch(client, hotel["id"], state="expected").json()["reimbursement"] == "expected"
    assert _work(client, world, hotel["id"]) == ("expected", None)

    _patch(client, hotel["id"], settled_by_id=payment["id"])
    assert _work(client, world, hotel["id"]) == ("expected", payment["id"])

    # Absent means leave alone: an empty body changes nothing.
    assert _patch(client, hotel["id"]).status_code == 200
    assert _work(client, world, hotel["id"]) == ("expected", payment["id"])

    _patch(client, hotel["id"], clear_settlement=True)
    assert _work(client, world, hotel["id"]) == ("expected", None)

    _patch(client, hotel["id"], state="written_off")
    assert _work(client, world, hotel["id"]) == ("written_off", None)

    _patch(client, hotel["id"], clear_state=True)
    assert _work(client, world, hotel["id"]) == (None, None)


def test_the_patch_refuses_with_a_sentence_and_changes_nothing(client, world):
    income = _post(client, world, "checking", 5_000)
    refused = _patch(client, income["id"], state="expected")
    assert refused.status_code == 422
    assert refused.json()["detail"].startswith("only money leaving an account")
    assert _work(client, world, income["id"]) == (None, None)

    hotel = _post(client, world, "card", -24_000)
    _patch(client, hotel["id"], state="written_off")
    conflict = _patch(client, hotel["id"], settled_by_id=income["id"])
    assert conflict.status_code == 409
    assert "written off" in conflict.json()["detail"]
    assert _work(client, world, hotel["id"]) == ("written_off", None)


def test_another_households_payment_is_404_even_to_a_member_of_both(client, world):
    hotel = _post(client, world, "card", -24_000)
    second = client.post("/api/households", json={"name": "Second"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{second['id']}/accounts",
        json={"name": "Elsewhere", "type": "checking"},
        headers=HEADERS,
    ).json()
    theirs = client.post(
        f"/api/households/{second['id']}/transactions",
        json={"account_id": account["id"], "date": "2026-09-30", "amount": 24_000},
        headers=HEADERS,
    ).json()

    missing = _patch(client, hotel["id"], settled_by_id=theirs["id"])
    assert missing.status_code == 404
    assert missing.json()["detail"] == "no such transaction"
    assert _work(client, world, hotel["id"]) == (None, None)

    linked = client.post(
        f"{world['base']}/transactions/reimbursements/link",
        json={"expense_ids": [hotel["id"]], "settlement_id": theirs["id"]},
        headers=HEADERS,
    )
    assert linked.status_code == 404
    assert _work(client, world, hotel["id"]) == (None, None)


def test_a_reconciled_row_is_flagged_over_http(client, world):
    locked = _post(client, world, "card", -9_900, cleared="reconciled")
    replied = _patch(client, locked["id"], state="expected")
    assert replied.status_code == 200, replied.text
    assert _work(client, world, locked["id"]) == ("expected", None)
    assert _rows(client, world)[locked["id"]]["cleared"] == "reconciled"


def test_linking_many_to_one_payment_is_one_act(client, world):
    hotel = _post(client, world, "card", -24_000)
    dinner = _post(client, world, "card", -8_450)
    payment = _post(client, world, "checking", 32_450, "2026-09-30")

    linked = client.post(
        f"{world['base']}/transactions/reimbursements/link",
        json={"expense_ids": [hotel["id"], dinner["id"]], "settlement_id": payment["id"]},
        headers=HEADERS,
    )
    assert linked.status_code == 200, linked.text
    assert {(one["id"], one["reimbursed_by_id"]) for one in linked.json()} == {
        (hotel["id"], payment["id"]), (dinner["id"], payment["id"]),
    }
    for one in (hotel, dinner):
        assert _work(client, world, one["id"]) == ("expected", payment["id"])


def test_linking_many_is_all_or_nothing_over_http(client, world):
    hotel = _post(client, world, "card", -24_000)
    refund = _post(client, world, "card", 1_500)
    payment = _post(client, world, "checking", 32_450, "2026-09-30")

    refused = client.post(
        f"{world['base']}/transactions/reimbursements/link",
        json={"expense_ids": [hotel["id"], refund["id"]], "settlement_id": payment["id"]},
        headers=HEADERS,
    )
    assert refused.status_code == 422
    assert refused.json()["detail"].startswith("€15.00 in Visa on 2026-09-10: only money leaving")
    assert _work(client, world, hotel["id"]) == (None, None)
    assert _work(client, world, refund["id"]) == (None, None)

    empty = client.post(
        f"{world['base']}/transactions/reimbursements/link",
        json={"expense_ids": [], "settlement_id": payment["id"]},
        headers=HEADERS,
    )
    assert empty.status_code == 422


def test_bulk_flags_what_it_can_and_counts_what_it_skipped(client, world):
    hotel = _post(client, world, "card", -24_000)
    locked = _post(client, world, "card", -9_900, cleared="reconciled")
    income = _post(client, world, "checking", 5_000)
    legs = client.post(
        f"{world['base']}/transfers",
        json={
            "from_account_id": world["checking"]["id"],
            "to_account_id": world["card"]["id"],
            "date": "2026-09-12",
            "amount": 10_000,
        },
        headers=HEADERS,
    ).json()
    ids = [hotel["id"], locked["id"], income["id"], legs["from"]["id"], legs["to"]["id"]]

    done = client.post(
        f"{world['base']}/transactions/bulk",
        json={"transaction_ids": ids, "reimbursement": "expected"},
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    assert done.json()["skipped_reimbursement"] == 3
    rows = _rows(client, world)
    assert {one: rows[one]["reimbursement"] for one in ids} == {
        hotel["id"]: "expected",
        locked["id"]: "expected",
        income["id"]: None,
        legs["from"]["id"]: None,
        legs["to"]["id"]: None,
    }

    cleared = client.post(
        f"{world['base']}/transactions/bulk",
        json={"transaction_ids": ids, "clear_reimbursement": True},
        headers=HEADERS,
    )
    assert cleared.json()["skipped_reimbursement"] == 0
    rows = _rows(client, world)
    assert {rows[one]["reimbursement"] for one in ids} == {None}


def test_bulk_refuses_to_write_off_a_repaid_row_and_names_it(client, world):
    hotel = _post(client, world, "card", -24_000)
    dinner = _post(client, world, "card", -8_450)
    payment = _post(client, world, "checking", 24_000, "2026-09-30")
    _patch(client, hotel["id"], settled_by_id=payment["id"])

    refused = client.post(
        f"{world['base']}/transactions/bulk",
        json={"transaction_ids": [dinner["id"], hotel["id"]], "reimbursement": "written_off"},
        headers=HEADERS,
    )
    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("-€240.00 in Visa on 2026-09-10: this has been paid back")
    assert _work(client, world, hotel["id"]) == ("expected", payment["id"])
    assert _work(client, world, dinner["id"]) == (None, None), "half the selection was written"


# --------------------------------------------------------------------------- #
# Edits that would break a link, refused rather than left to rot
# --------------------------------------------------------------------------- #


def test_splitting_a_payment_is_refused_and_every_link_survives(session, claim, write):
    hotel, dinner, payment = claim["hotel"], claim["dinner"], claim["payment"]
    with write():
        for expense in (hotel, dinner):
            txn_service.set_reimbursement(session, expense, settled_by=payment)
    with (
        pytest.raises(ValidationError, match="Take them off it before splitting"),
        write(BatchKind.split),
    ):
        txn_service.split(
            session, payment,
            [txn_service.SplitPart(amount=30_000), txn_service.SplitPart(amount=2_450)],
        )
    session.refresh(hotel)
    session.refresh(dinner)
    assert session.get(Transaction, payment.id).amount == 32_450
    assert hotel.reimbursed_by_id == payment.id
    assert dinner.reimbursed_by_id == payment.id


def test_a_work_expense_cannot_be_split_into_a_part_that_is_money_in(session, claim, write):
    hotel = claim["hotel"]
    with write():
        txn_service.set_reimbursement(session, hotel, state=EXPECTED)
    with pytest.raises(ValidationError, match="money going out"), write(BatchKind.split):
        txn_service.split(
            session, hotel,
            [txn_service.SplitPart(amount=-25_000), txn_service.SplitPart(amount=1_000)],
        )
    assert session.get(Transaction, hotel.id).amount == -24_000


def test_an_edit_cannot_turn_a_work_expense_into_money_in(session, claim, write):
    hotel = claim["hotel"]
    with write():
        txn_service.set_reimbursement(session, hotel, state=EXPECTED)
    with pytest.raises(ValidationError, match="stay money going out"), write():
        txn_service.update(session, hotel, amount=24_000)
    session.refresh(hotel)
    assert hotel.amount == -24_000
    assert hotel.reimbursement is EXPECTED


def test_an_edit_cannot_turn_a_payment_into_money_out(session, claim, write):
    hotel, payment = claim["hotel"], claim["payment"]
    with write():
        txn_service.set_reimbursement(session, hotel, settled_by=payment)
    with pytest.raises(ValidationError, match="stay money coming in"), write():
        txn_service.update(session, payment, amount=-32_450)
    session.refresh(payment)
    session.refresh(hotel)
    assert payment.amount == 32_450
    assert hotel.reimbursed_by_id == payment.id


def test_an_edit_that_keeps_the_sign_is_still_allowed(session, claim, write):
    hotel, payment = claim["hotel"], claim["payment"]
    with write():
        txn_service.set_reimbursement(session, hotel, settled_by=payment)
    with write():
        txn_service.update(session, hotel, amount=-25_000)
        txn_service.update(session, payment, amount=33_000)
    session.refresh(hotel)
    session.refresh(payment)
    assert (hotel.amount, payment.amount) == (-25_000, 33_000)
    assert hotel.reimbursed_by_id == payment.id
