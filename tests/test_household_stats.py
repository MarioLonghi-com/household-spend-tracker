"""What a household holds, counted.

Every number on the Household page comes from one aggregate query, and the
failure mode of an aggregate query is that it looks right whatever it counted.
So the fixture here has **two households**, both furnished, and every assertion
is a figure rather than a status: a count that forgot its `where` would still
answer 200 with a perfectly plausible number.

The second household is deliberately the larger one in receipts and the
different one in currency, so a leak shows up as a wrong value rather than as a
coincidence.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.audit.batch import batch
from app.errors import NotFound
from app.models import (
    Account,
    AccountType,
    BatchKind,
    Category,
    CategoryGroup,
    ClearedState,
    MatchType,
    Payee,
    PayeeRule,
    Receipt,
    Transaction,
)
from app.services import households as household_service

JAN = date(2026, 1, 10)
FEB = date(2026, 2, 20)


def _payee(session, household_id: str, name: str) -> Payee:
    payee = Payee(household_id=household_id, name=name, name_folded=name.lower())
    session.add(payee)
    return payee


#: A receipt is identified by the hash of its bytes, and `(transaction_id,
#: content_sha256)` is unique -- so each fixture receipt needs a distinct one.
_SHAS = iter(f"{n:064x}" for n in range(1, 100))


def _receipt(session, household_id: str, *, transaction_id: str | None = None) -> Receipt:
    sha = next(_SHAS)
    receipt = Receipt(
        household_id=household_id,
        transaction_id=transaction_id,
        content_sha256=sha,
        blob_sha256=sha,
        media_type="image/jpeg",
        byte_size=1024,
    )
    session.add(receipt)
    return receipt


def _spend(session, account: Account, amount: int, when: date, *, cleared=ClearedState.uncleared):
    txn = Transaction(
        household_id=account.household_id,
        account_id=account.id,
        date=when,
        amount=amount,
        cleared=cleared,
    )
    session.add(txn)
    return txn


@pytest.fixture()
def furnished(session, owner, member, household, other_household, accounts):
    """Two households, both with things in them.

    `household` (the owner's) gets three accounts in two currencies -- that is
    the `accounts` fixture -- four transactions, two payees, one rule, two
    receipts of which one is loose, and one country on one account.

    `other_household` (the member's, which the owner is not in) gets its own
    account, its own transactions and **three** receipts, so that a count that
    reads the whole table comes back 5 instead of 2.
    """
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        accounts["checking"].country = "ES"
        accounts["pounds"].country = "GB"
        accounts["card"].closed = True

        _spend(session, accounts["checking"], -1_250, JAN, cleared=ClearedState.cleared)
        _spend(session, accounts["checking"], -3_00, FEB)
        _spend(session, accounts["card"], -9_99, FEB)
        _spend(session, accounts["pounds"], -4_50, JAN)

        mercadona = _payee(session, household.id, "Mercadona")
        _payee(session, household.id, "Naturgy")
        session.flush()
        session.add(
            PayeeRule(
                household_id=household.id,
                match_type=MatchType.contains,
                pattern="mercadona",
                payee_id=mercadona.id,
                enabled=False,
            )
        )
        attached = _spend(session, accounts["checking"], -7_77, FEB)
        session.flush()
        _receipt(session, household.id, transaction_id=attached.id)
        _receipt(session, household.id)

    with batch(
        session, kind=BatchKind.manual, actor_id=member.id, household_id=other_household.id
    ):
        theirs = Account(
            household_id=other_household.id,
            name="Their Current",
            type=AccountType.checking,
            currency="GBP",
            country="IE",
        )
        session.add(theirs)
        session.flush()
        _spend(session, theirs, -1_00, JAN)
        _spend(session, theirs, -2_00, FEB)
        _payee(session, other_household.id, "Somebody Else Ltd")
        for _ in range(3):
            _receipt(session, other_household.id)

        group = CategoryGroup(household_id=other_household.id, name="Theirs")
        session.add(group)
        session.flush()
        session.add(Category(household_id=other_household.id, group_id=group.id, name="Their Rent"))

    session.flush()
    return {"mine": household, "theirs": other_household}


# --------------------------------------------------------------------------- #
# The numbers
# --------------------------------------------------------------------------- #


def test_it_counts_this_household_and_not_the_other(session, furnished):
    """The figures, one by one. Five transactions here, two next door."""
    figures = household_service.stats(session, furnished["mine"].id)

    assert figures.members == 2
    assert figures.accounts == 3
    assert figures.accounts_closed == 1
    assert figures.transactions == 5
    # Four of the five; one was entered cleared.
    assert figures.transactions_uncleared == 4
    assert figures.payees == 2
    assert figures.payee_rules == 1
    assert figures.payee_rules_enabled == 0
    assert figures.receipts == 2
    assert figures.receipts_unattached == 1
    assert figures.reconciliations == 0


def test_the_other_household_has_its_own_numbers(session, furnished):
    """The same call on the neighbour. Both answers cannot be the whole table."""
    figures = household_service.stats(session, furnished["theirs"].id)

    assert figures.members == 1
    assert figures.accounts == 1
    assert figures.transactions == 2
    assert figures.receipts == 3
    assert figures.receipts_unattached == 3
    assert figures.payees == 1
    assert figures.payee_rules == 0


def test_a_currency_is_counted_and_never_added_up(session, furnished):
    """Two currencies here, each with its own counts and no total anywhere.

    EUR holds two accounts (one of them the closed card) and four transactions;
    GBP holds one account and one transaction. The neighbour's GBP account and
    its two transactions are not in either figure.
    """
    figures = household_service.stats(session, furnished["mine"].id)

    assert [one.currency for one in figures.currencies] == ["EUR", "GBP"]
    assert {one.currency: one.accounts for one in figures.currencies} == {"EUR": 2, "GBP": 1}
    assert {one.currency: one.transactions for one in figures.currencies} == {"EUR": 4, "GBP": 1}

    # The rule this module exists under: no field anywhere is a sum of money,
    # per currency or across them.
    assert not [name for name in figures.__slots__ if "minor" in name or "total" in name]


def test_countries_are_the_ones_its_own_accounts_name(session, furnished):
    """ES and GB. The neighbour's IE account is not this household's country."""
    figures = household_service.stats(session, furnished["mine"].id)
    assert figures.countries == ["ES", "GB"]


def test_the_ledger_span_is_its_own_first_and_last_dates(session, furnished):
    figures = household_service.stats(session, furnished["mine"].id)
    assert figures.first_transaction == JAN
    assert figures.last_transaction == FEB


def test_an_empty_household_counts_zero_rather_than_failing(session, owner):
    """A household made a minute ago. Every count is zero, nothing is None.

    Except the categories: `create_household` seeds a default tree, so a new
    household has something to categorise with -- and this asserts that rather
    than a zero it would fail on the day the seed landed.
    """
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        fresh = household_service.create_household(session, name="Brand New", creator=owner)

    figures = household_service.stats(session, fresh.id)
    assert figures.accounts == 0
    assert figures.transactions == 0
    assert figures.receipts == 0
    assert figures.payees == 0
    assert figures.currencies == []
    assert figures.countries == []
    assert figures.first_transaction is None
    assert figures.last_transaction is None
    assert figures.members == 1
    assert figures.categories > 0
    assert figures.category_groups > 0


def test_categories_are_counted_with_the_archived_ones_marked(session, owner, furnished):
    """The seeded tree, plus one archived, and none of the neighbour's."""
    mine = furnished["mine"].id
    before = household_service.stats(session, mine)
    assert before.categories_archived == 0

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=mine):
        group = CategoryGroup(household_id=mine, name="Mine")
        session.add(group)
        session.flush()
        session.add(
            Category(household_id=mine, group_id=group.id, name="Old Thing", archived=True)
        )

    after = household_service.stats(session, mine)
    assert after.categories == before.categories + 1
    assert after.categories_archived == 1
    assert after.category_groups == before.category_groups + 1


# --------------------------------------------------------------------------- #
# Over HTTP, where the 404 rule lives
# --------------------------------------------------------------------------- #


def test_a_household_you_are_not_in_is_a_404_not_a_403(session, owner, other_household):
    """Through the dependency's own service call, which is what the route uses.

    `deps.current_household` is `household_service.get_for`, so the refusal the
    route gives for `/households/{id}/stats` is this one -- and it must not
    distinguish a household that is real from one that never was, or the size of
    somebody else's ledger becomes discoverable by asking.
    """
    with pytest.raises(NotFound, match="no such household"):
        household_service.get_for(session, other_household.id, owner)
    with pytest.raises(NotFound, match="no such household"):
        household_service.get_for(session, "f" * 32, owner)


def test_the_route_answers_with_the_figures(client):
    """End to end: the wizard's owner, a household, an account, a transaction."""
    from tests.conftest import HEADERS, _setup_owner

    _setup_owner(client)
    made = client.post(
        "/api/households", json={"name": "Stats House", "base_currency": "EUR"}, headers=HEADERS
    )
    assert made.status_code == 201, made.text
    house = made.json()["id"]

    account = client.post(
        f"/api/households/{house}/accounts",
        json={"name": "Checking", "type": "checking", "currency": "EUR", "country": "ES"},
        headers=HEADERS,
    )
    assert account.status_code == 201, account.text

    added = client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": account.json()["id"], "date": "2026-01-10", "amount": -1234},
        headers=HEADERS,
    )
    assert added.status_code == 201, added.text

    answer = client.get(f"/api/households/{house}/stats")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["accounts"] == 1
    assert body["transactions"] == 1
    assert body["members"] == 1
    assert body["currencies"] == [{"currency": "EUR", "accounts": 1, "transactions": 1}]
    assert [one["code"] for one in body["countries"]] == ["ES"]
    assert body["countries"][0]["name"] == "Spain"
    assert body["first_transaction"] == "2026-01-10"

    # And nothing in the payload is a figure of money, per currency or otherwise.
    assert not [key for key in body if "minor" in key or key.endswith("total")]


def test_the_route_is_a_404_for_a_household_that_is_not_yours(client):
    from tests.conftest import _setup_owner

    _setup_owner(client)
    missing = client.get(f"/api/households/{'f' * 32}/stats")
    assert missing.status_code == 404
