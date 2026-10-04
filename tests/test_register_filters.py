"""The register's amount, Cleared and Source filters (#123).

Two currencies throughout, because the amount lookup is the one filter here
whose meaning depends on the currency: "45.20" is 4520 minor units of euro and
no amount of yen at all, and a lookup that forgot that would find the wrong
rows in a household holding both.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from app.audit.batch import batch
from app.models import (
    BatchKind,
    ClearedState,
    RegisterSource,
    ReimbursementState,
    ReimbursementView,
    Transaction,
)
from app.money import magnitude_span
from app.services import transactions as txn_service
from app.services import transfers
from tests.conftest import HEADERS
from tests.test_api import _household_with_accounts

DAY = date(2026, 3, 24)


# --------------------------------------------------------------------------- #
# What a typed amount means
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("typed", "currency", "span"),
    [
        ("45.20", "EUR", (4520, 4520)),
        ("45.2", "EUR", (4520, 4520)),
        ("45", "EUR", (4500, 4599)),
        ("45", "JPY", (45, 45)),
        ("1.234", "BHD", (1234, 1234)),
        ("1", "BHD", (1000, 1999)),
        ("0.05", "GBP", (5, 5)),
    ],
)
def test_a_typed_amount_is_read_in_each_currencys_own_units(typed, currency, span):
    assert magnitude_span(typed, currency) == span


@pytest.mark.parametrize(
    ("typed", "currency"),
    [
        ("45.20", "JPY"),  # yen has no cents: not rounded to 45
        ("1.2345", "EUR"),  # more places than the currency has
        ("-45", "EUR"),  # the client takes the sign off; a sign here is not an amount
        ("45,20", "EUR"),  # nor is a comma -- the client normalises it
        ("abc", "EUR"),
        ("1.2.3", "EUR"),
        ("", "EUR"),
    ],
)
def test_what_a_currency_cannot_hold_matches_nothing_rather_than_something_close(typed, currency):
    assert magnitude_span(typed, currency) is None


# --------------------------------------------------------------------------- #
# The service's filter
# --------------------------------------------------------------------------- #


@pytest.fixture()
def ledger(session, owner, household, accounts):
    """Every source, both directions of money, two currencies."""
    eur, card, gbp = accounts["checking"], accounts["card"], accounts["pounds"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        made = {
            "typed_out": txn_service.create(session, account=eur, date=DAY, amount=-4520),
            "typed_in": txn_service.create(
                session, account=card, date=DAY, amount=4520, cleared="cleared"
            ),
            "pounds": txn_service.create(session, account=gbp, date=DAY, amount=-4599),
            "small": txn_service.create(session, account=eur, date=DAY, amount=-1200),
            "imported": txn_service.create(
                session, account=eur, date=DAY, amount=-8800, import_id="line-1",
                import_payee_original="SHOP 1", cleared="reconciled",
            ),
            "to_split": txn_service.create(session, account=gbp, date=DAY, amount=-3000),
        }
        out_leg, in_leg = txn_service.create_transfer(
            session, source=eur, destination=gbp, date=DAY, amount=10000, to_amount=8600
        )
        made["leg_out"], made["leg_in"] = out_leg, in_leg
        # An imported pair linked afterwards: a transfer, not an import.
        a = txn_service.create(
            session, account=eur, date=DAY, amount=-700, import_id="line-2",
            import_payee_original="TO CARD",
        )
        b = txn_service.create(
            session, account=card, date=DAY, amount=700, import_id="line-3",
            import_payee_original="FROM CURRENT",
        )
        transfers.link(session, a, b)
        made["linked_out"], made["linked_in"] = a, b
    with batch(session, kind=BatchKind.split, actor_id=owner.id, household_id=household.id):
        parts = txn_service.split(
            session, made.pop("to_split"),
            [txn_service.SplitPart(amount=-1000), txn_service.SplitPart(amount=-2000)],
        )
    made["part_1"], made["part_2"] = parts
    return made


def _ids(session, household, **filters) -> set[str]:
    stmt = txn_service.filtered(household.id, **filters)
    return {row.id for row in session.execute(stmt).scalars()}


def _names(ledger, ids: set[str]) -> set[str]:
    by_id = {row.id: name for name, row in ledger.items()}
    return {by_id[one] for one in ids}


def _lookup(session, household, typed):
    return txn_service.amount_lookup(
        txn_service.account_currencies(session, household.id), typed
    )


def test_an_amount_finds_money_out_and_money_in_alike(session, household, ledger):
    found = _ids(session, household, amount=_lookup(session, household, "45.20"))
    assert _names(ledger, found) == {"typed_out", "typed_in"}


def test_a_whole_amount_finds_every_figure_in_that_unit_in_every_currency(
    session, household, ledger
):
    found = _ids(session, household, amount=_lookup(session, household, "45"))
    assert _names(ledger, found) == {"typed_out", "typed_in", "pounds"}


def test_an_amount_that_is_nobodys_matches_no_rows_not_every_row(session, household, ledger):
    assert _ids(session, household, amount=_lookup(session, household, "0.001")) == set()
    assert _ids(session, household, amount=[]) == set()


def test_an_amount_reaches_both_sides_of_a_cross_currency_transfer_separately(
    session, household, ledger
):
    assert _names(ledger, _ids(session, household, amount=_lookup(session, household, "100"))) == {
        "leg_out"
    }
    assert _names(ledger, _ids(session, household, amount=_lookup(session, household, "86.00"))) == {
        "leg_in"
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (RegisterSource.transfer, {"leg_out", "leg_in", "linked_out", "linked_in"}),
        (RegisterSource.split, {"part_1", "part_2"}),
        (RegisterSource.imported, {"imported"}),
        (RegisterSource.manual, {"typed_out", "typed_in", "pounds", "small"}),
    ],
)
def test_the_source_filter_agrees_with_the_letter_in_the_column(
    session, household, ledger, source, expected
):
    assert _names(ledger, _ids(session, household, source=source)) == expected


def test_every_row_has_exactly_one_source(session, household, ledger):
    """The four filters partition the register: nothing in two, nothing in none."""
    seen: list[str] = []
    for source in RegisterSource:
        seen.extend(_ids(session, household, source=source))
    everything = {row.id for row in session.execute(select(Transaction)).scalars()}
    assert sorted(seen) == sorted(everything)


def test_the_cleared_filter_narrows_to_one_state(session, household, ledger):
    assert _names(ledger, _ids(session, household, cleared=ClearedState.cleared)) == {"typed_in"}
    assert _names(ledger, _ids(session, household, cleared=ClearedState.reconciled)) == {
        "imported"
    }


def test_filters_combine(session, household, ledger):
    found = _ids(
        session, household,
        amount=_lookup(session, household, "45"), source=RegisterSource.manual,
        cleared=ClearedState.uncleared,
    )
    assert _names(ledger, found) == {"typed_out", "pounds"}


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def test_the_register_takes_amount_and_source_and_withholds_the_balance(client):
    world = _household_with_accounts(client)
    house, checking, card = world["household"]["id"], world["checking"]["id"], world["card"]["id"]
    base = f"/api/households/{house}"
    for account, amount in ((checking, -4520), (checking, 4520), (checking, -1200), (card, -4510)):
        made = client.post(
            f"{base}/transactions",
            json={"account_id": account, "date": "2026-03-24", "amount": amount},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text

    page = client.get(
        f"{base}/transactions", params={"amount": "45.20", "account_id": checking}
    ).json()
    assert sorted(one["amount"] for one in page["transactions"]) == [-4520, 4520]
    assert page["total"] == 2
    # One account, date order -- but a filter hides rows, so no balance.
    assert page["has_running_balance"] is False

    whole = client.get(f"{base}/transactions", params={"amount": "45"}).json()
    assert sorted(one["amount"] for one in whole["transactions"]) == [-4520, -4510, 4520]

    typed = client.get(
        f"{base}/transactions", params={"source": "manual", "account_id": checking}
    ).json()
    assert typed["total"] == 3
    assert typed["has_running_balance"] is False
    assert client.get(f"{base}/transactions", params={"source": "transfer"}).json()["total"] == 0

    refused = client.get(f"{base}/transactions", params={"source": "somewhere"})
    assert refused.status_code == 422


# --------------------------------------------------------------------------- #
# Work expenses
# --------------------------------------------------------------------------- #


@pytest.fixture()
def work(session, owner, household, accounts):
    """Owed, repaid, written off -- one each, in two currencies -- the payment,
    and two ordinary rows either way round."""
    eur, card, gbp = accounts["checking"], accounts["card"], accounts["pounds"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        made = {
            "owed": txn_service.create(session, account=gbp, date=DAY, amount=-2340),
            "repaid": txn_service.create(session, account=card, date=DAY, amount=-24000),
            "off": txn_service.create(session, account=card, date=DAY, amount=-6000),
            "payment": txn_service.create(session, account=eur, date=DAY, amount=24000),
            "plain_out": txn_service.create(session, account=eur, date=DAY, amount=-24000),
            "plain_in": txn_service.create(session, account=eur, date=DAY, amount=6000),
        }
        txn_service.set_reimbursement(session, made["owed"], state=ReimbursementState.expected)
        txn_service.set_reimbursement(session, made["repaid"], settled_by=made["payment"])
        txn_service.set_reimbursement(session, made["off"], state=ReimbursementState.written_off)
    return made


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (ReimbursementView.work, {"owed", "repaid", "off", "payment"}),
        (ReimbursementView.owed, {"owed"}),
        (ReimbursementView.paid, {"repaid", "payment"}),
        (ReimbursementView.off, {"off"}),
    ],
)
def test_the_work_expense_views_and_which_bring_the_payments(
    session, household, work, view, expected
):
    assert _names(work, _ids(session, household, reimbursement=view)) == expected


def test_a_payment_whose_expenses_were_unlinked_leaves_the_views(
    session, owner, household, work
):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn_service.set_reimbursement(session, work["repaid"], settled_by=None)
    assert _names(work, _ids(session, household, reimbursement=ReimbursementView.work)) == {
        "owed", "repaid", "off",
    }
    assert _ids(session, household, reimbursement=ReimbursementView.paid) == set()
    assert _names(work, _ids(session, household, reimbursement=ReimbursementView.owed)) == {
        "owed", "repaid",
    }


def test_the_work_filter_is_one_households(session, household, other_household, work):
    assert _ids(session, other_household, reimbursement=ReimbursementView.work) == set()


def test_the_register_filters_work_expenses_counts_them_and_withholds_the_balance(client):
    """T27 and T28: `total` is the filtered count, and one account under a
    filter that hides rows has no running balance."""
    world = _household_with_accounts(client)
    house, checking, card = world["household"]["id"], world["checking"]["id"], world["card"]["id"]
    base = f"/api/households/{house}"

    def post(account, amount):
        made = client.post(
            f"{base}/transactions",
            json={"account_id": account, "date": "2026-09-10", "amount": amount},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text
        return made.json()["id"]

    hotel, dinner, taxi = post(card, -24000), post(card, -8450), post(checking, -2340)
    payment, _salary, _shop = post(checking, 32450), post(checking, 250000), post(checking, -1200)
    for expense in (hotel, dinner):
        client.patch(
            f"/api/transactions/{expense}/reimbursement",
            json={"settled_by_id": payment},
            headers=HEADERS,
        )
    client.patch(
        f"/api/transactions/{taxi}/reimbursement", json={"state": "expected"}, headers=HEADERS
    )

    def page(**params):
        return client.get(f"{base}/transactions", params=params).json()

    work = page(reimbursement="work")
    assert {row["id"] for row in work["transactions"]} == {hotel, dinner, taxi, payment}
    assert work["total"] == len(work["transactions"]) == 4

    owed = page(reimbursement="owed", account_id=checking)
    assert [row["id"] for row in owed["transactions"]] == [taxi]
    assert owed["total"] == 1
    assert owed["has_running_balance"] is False
    assert owed["transactions"][0]["running_balance"] is None

    paid = page(reimbursement="paid", account_id=checking)
    assert [row["id"] for row in paid["transactions"]] == [payment]
    assert paid["total"] == 1
    assert paid["has_running_balance"] is False

    assert page(reimbursement="off")["total"] == 0
    # The same account unfiltered does carry the balance: the filter is what
    # withheld it.
    assert page(account_id=checking)["has_running_balance"] is True

    assert client.get(f"{base}/transactions", params={"reimbursement": "any"}).status_code == 422
