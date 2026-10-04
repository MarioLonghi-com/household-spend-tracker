"""The counts behind the category and payee screens.

Every test here asserts a **number**, because every way this feature can be
wrong produces a 200 with a plausible figure in it:

- a count that includes another household's rows (the reason this module has two
  households with identically named categories and payees in both),
- a "distinct payees" figure that counts the *no payee* bucket as a payee,
- a total that drops the rows a breakdown truncated away,
- a count that quietly excludes one currency, because somebody reached for the
  reporting code that partitions by currency and kept the partition.

The last one is worth spelling out: these figures are counts of rows, so a
household holding EUR and GBP has one count covering both. Nothing here reads
`amount`, so there is no sum of two currencies to get wrong -- and the fixture
puts a GBP transaction in a category to prove the count does not lose it.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.audit.batch import batch
from app.models import (
    Account,
    AccountType,
    BatchKind,
    Category,
    CategoryGroup,
    Transaction,
)
from app.services import payees as payee_service
from app.services import stats as stats_service
from tests.conftest import HEADERS, _setup_owner  # noqa: F401

JAN = date(2026, 1, 10)


# --------------------------------------------------------------------------- #
# A fixture with two of everything, two households especially
# --------------------------------------------------------------------------- #


def _categories(
    session,
    household,
    actor,
    names: tuple[str, ...],
    groups: tuple[str, str] = ("Everyday", "Treats"),
) -> dict[str, Category]:
    """Two groups, so the tree has two levels with two of each.

    `groups` is a parameter because group names are unique per household, so a
    test that wants more categories has to say which new headings they sit
    under rather than quietly colliding with the ones already there.
    """
    with batch(session, kind=BatchKind.admin, actor_id=actor.id, household_id=household.id):
        first = CategoryGroup(household_id=household.id, name=groups[0])
        second = CategoryGroup(household_id=household.id, name=groups[1])
        session.add_all([first, second])
        session.flush()
        made = {
            name: Category(
                household_id=household.id,
                group_id=(first.id if at == 0 else second.id),
                name=name,
            )
            for at, name in enumerate(names)
        }
        session.add_all(list(made.values()))
    return made


def _payees(session, household, actor, names: tuple[str, ...]) -> dict[str, object]:
    with batch(session, kind=BatchKind.manual, actor_id=actor.id, household_id=household.id):
        return {
            name: payee_service.get_or_create(session, household.id, name) for name in names
        }


def _rows(session, household, actor, rows: list[tuple]) -> None:
    """`(account, payee, category)` triples, one batch for the lot."""
    with batch(session, kind=BatchKind.manual, actor_id=actor.id, household_id=household.id):
        for account, payee, category in rows:
            session.add(
                Transaction(
                    household_id=household.id,
                    account_id=account.id,
                    date=JAN,
                    amount=-1_000,
                    payee_id=payee.id if payee is not None else None,
                    category_id=category.id if category is not None else None,
                )
            )


@pytest.fixture()
def ledger(session, household, owner, accounts):
    """Two categories, two payees, and rows arranged so every figure differs.

    Deliberate shape:

    - Groceries: Carrefour 3 (one of them on the GBP account), Mercadona 2, and
      one row with no payee at all -- six transactions, two payees.
    - Eating out: Carrefour 1.
    - Carrefour also has one uncategorised row, so its transaction count is
      larger than the categories under it add up to unless the uncategorised
      bucket is carried.
    """
    made = _categories(session, household, owner, ("Groceries", "Eating out"))
    who = _payees(session, household, owner, ("Carrefour", "Mercadona"))
    groceries, eating_out = made["Groceries"], made["Eating out"]
    carrefour, mercadona = who["Carrefour"], who["Mercadona"]

    _rows(
        session,
        household,
        owner,
        [
            (accounts["checking"], carrefour, groceries),
            (accounts["card"], carrefour, groceries),
            # GBP, in the same category: a count spans currencies because it is
            # a count. If this row goes missing, somebody added a partition.
            (accounts["pounds"], carrefour, groceries),
            (accounts["checking"], mercadona, groceries),
            (accounts["checking"], mercadona, groceries),
            (accounts["checking"], None, groceries),
            (accounts["checking"], carrefour, eating_out),
            (accounts["checking"], carrefour, None),
        ],
    )
    return {"categories": made, "payees": who}


@pytest.fixture()
def elsewhere(session, other_household, member):
    """The second household, with the *same names* in it.

    Names, not ids, are what a leaking query would make look right on screen:
    "Groceries, 10 transactions" reads perfectly when four of them belong to
    somebody else.
    """
    with batch(session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id):
        account = Account(
            household_id=other_household.id,
            name="Their Current",
            type=AccountType.checking,
            currency="GBP",
        )
        session.add(account)
    made = _categories(session, other_household, member, ("Groceries", "Eating out"))
    who = _payees(session, other_household, member, ("Carrefour", "Mercadona"))
    _rows(
        session,
        other_household,
        member,
        [
            (account, who["Carrefour"], made["Groceries"]),
            (account, who["Carrefour"], made["Groceries"]),
            (account, who["Mercadona"], made["Groceries"]),
            (account, who["Mercadona"], made["Eating out"]),
        ],
    )
    return {"account": account, "categories": made, "payees": who}


# --------------------------------------------------------------------------- #
# Category stats
# --------------------------------------------------------------------------- #


def test_a_category_counts_its_transactions_and_its_payees(session, household, ledger):
    found = stats_service.category_stats(session, household.id)
    groceries = found[ledger["categories"]["Groceries"].id]

    assert groceries.transaction_count == 6, "three Carrefour, two Mercadona, one with no payee"
    assert groceries.payee_count == 2, "'No payee' is not a payee"

    eating_out = found[ledger["categories"]["Eating out"].id]
    assert eating_out.transaction_count == 1
    assert eating_out.payee_count == 1


def test_a_category_breaks_down_by_payee_busiest_first(session, household, ledger):
    found = stats_service.category_stats(session, household.id)
    groceries = found[ledger["categories"]["Groceries"].id]

    assert [(one.name, one.count) for one in groceries.payees] == [
        ("Carrefour", 3),
        ("Mercadona", 2),
        ("No payee", 1),
    ]
    # The whole list is here, so nothing is owed to a "+N more".
    assert groceries.more_payees == 0
    # The breakdown adds up to the figure on the row it explains.
    assert sum(one.count for one in groceries.payees) == groceries.transaction_count


def test_a_category_count_spans_the_currencies_in_it(session, household, ledger, accounts):
    """One of the Carrefour rows is on the GBP account.

    A count of transactions is not money, so it is not partitioned -- and the
    three Carrefour rows are three whether or not they share a currency.
    """
    found = stats_service.category_stats(session, household.id)
    groceries = found[ledger["categories"]["Groceries"].id]
    by_name = {one.name: one.count for one in groceries.payees}
    assert by_name["Carrefour"] == 3
    assert accounts["pounds"].currency == "GBP"


def test_a_category_nothing_is_filed_under_is_absent(session, household, owner, ledger):
    """Absent rather than zeroed: the caller has the tree already."""
    unused = _categories(session, household, owner, ("Fuel",), groups=("Car", "Odds"))["Fuel"]
    found = stats_service.category_stats(session, household.id)
    assert unused.id not in found


def test_a_long_payee_list_is_truncated_and_says_by_how_much(session, household, owner, accounts):
    """Ten payees in one category. The list is short; the count is not."""
    category = _categories(
        session, household, owner, ("Everything",), groups=("Catch-all", "Spare")
    )["Everything"]
    names = tuple(f"Shop {at:02d}" for at in range(10))
    who = _payees(session, household, owner, names)
    _rows(session, household, owner, [(accounts["checking"], who[n], category) for n in names])

    stat = stats_service.category_stats(session, household.id)[category.id]
    assert stat.transaction_count == 10, "the total is of every row, not of the shown ones"
    assert stat.payee_count == 10
    assert len(stat.payees) == stats_service.TOP_PAYEES == 8
    assert stat.more_payees == 2


def test_one_households_category_counts_carry_nothing_from_the_other(
    session, household, ledger, elsewhere
):
    """The bug this test exists to catch.

    Both households have a "Groceries", both have a "Carrefour", and the other
    one has four rows. Ours must still read six.
    """
    found = stats_service.category_stats(session, household.id)
    assert found[ledger["categories"]["Groceries"].id].transaction_count == 6
    assert elsewhere["categories"]["Groceries"].id not in found

    # And the other way round, so the scoping is not simply dropping rows.
    theirs = stats_service.category_stats(session, elsewhere["categories"]["Groceries"].household_id)
    assert theirs[elsewhere["categories"]["Groceries"].id].transaction_count == 3
    assert theirs[elsewhere["categories"]["Eating out"].id].transaction_count == 1
    assert ledger["categories"]["Groceries"].id not in theirs


# --------------------------------------------------------------------------- #
# Payee stats
# --------------------------------------------------------------------------- #


def test_a_payee_counts_its_transactions_and_its_categories(session, household, ledger):
    found = stats_service.payee_stats(session, household.id)
    carrefour = found[ledger["payees"]["Carrefour"].id]

    assert carrefour.transaction_count == 5, "three groceries, one eating out, one uncategorised"
    assert carrefour.category_count == 2, "'Uncategorised' is not a category"

    mercadona = found[ledger["payees"]["Mercadona"].id]
    assert mercadona.transaction_count == 2
    assert mercadona.category_count == 1


def test_a_payee_lists_every_category_it_is_under_busiest_first(session, household, ledger):
    found = stats_service.payee_stats(session, household.id)
    carrefour = found[ledger["payees"]["Carrefour"].id]

    assert [(one.name, one.count) for one in carrefour.categories] == [
        ("Groceries", 3),
        ("Eating out", 1),
        ("Uncategorised", 1),
    ]
    # Uncategorised is carried, so the lines add up to the figure beside them.
    assert sum(one.count for one in carrefour.categories) == carrefour.transaction_count
    # And it is a null key, so a client cannot navigate to it as a category.
    assert [one.key for one in carrefour.categories][-1] is None


def test_a_payees_category_list_is_not_truncated(session, household, owner, accounts, ledger):
    """The screen shows three inline and the rest behind the row, so the answer
    has to hold more than three."""
    extra = _categories(
        session, household, owner, ("Fuel", "Pharmacy", "Post"), groups=("Car", "Errands")
    )
    carrefour = ledger["payees"]["Carrefour"]
    _rows(
        session,
        household,
        owner,
        [(accounts["checking"], carrefour, one) for one in extra.values()],
    )

    stat = stats_service.payee_stats(session, household.id)[carrefour.id]
    assert stat.transaction_count == 8
    assert stat.category_count == 5, "Groceries, Eating out, Fuel, Pharmacy, Post"
    assert len(stat.categories) == 6, "and the uncategorised line as well"


def test_one_households_payee_counts_carry_nothing_from_the_other(
    session, household, ledger, elsewhere
):
    found = stats_service.payee_stats(session, household.id)
    assert found[ledger["payees"]["Carrefour"].id].transaction_count == 5
    assert elsewhere["payees"]["Carrefour"].id not in found

    theirs = stats_service.payee_stats(session, elsewhere["account"].household_id)
    assert theirs[elsewhere["payees"]["Carrefour"].id].transaction_count == 2
    assert theirs[elsewhere["payees"]["Mercadona"].id].transaction_count == 2
    assert ledger["payees"]["Carrefour"].id not in theirs


def test_a_payee_with_no_transactions_is_absent(session, household, owner, ledger):
    unused = _payees(session, household, owner, ("Never Used",))["Never Used"]
    assert unused.id not in stats_service.payee_stats(session, household.id)


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #


def _world(client) -> dict:
    """A household with two accounts, two categories, two payees, over HTTP.

    The categories are two of the ones a new household is seeded with rather
    than two made here: `POST /households` seeds the default tree, so creating
    an "Everyday" group answers 409 and creating a "Groceries" would be testing
    against a name the app already uses.
    """
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    checking = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    card = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Visa", "type": "credit_card"},
        headers=HEADERS,
    ).json()
    tree = client.get(f"/api/households/{house['id']}/categories", headers=HEADERS).json()
    seeded = {one["name"]: one for group in tree for one in group["categories"]}
    made = {name: seeded[name] for name in ("Groceries", "Eating Out")}

    def spend(account, payee_name, category_id):
        body = {
            "account_id": account["id"],
            "date": "2026-01-10",
            "amount": -1_000,
            "payee_name": payee_name,
        }
        # Explicit either way: leaving `category_id` out means "work it out from
        # the payee", which would have this fixture categorise itself.
        if category_id is None:
            body["uncategorised"] = True
        else:
            body["category_id"] = category_id
        made_row = client.post(
            f"/api/households/{house['id']}/transactions", json=body, headers=HEADERS
        )
        assert made_row.status_code == 201, made_row.text

    spend(checking, "Carrefour", made["Groceries"]["id"])
    spend(card, "Carrefour", made["Groceries"]["id"])
    spend(checking, "Mercadona", made["Groceries"]["id"])
    spend(checking, "Carrefour", made["Eating Out"]["id"])
    spend(checking, "Carrefour", None)
    return {"house": house["id"], "categories": made}


def test_the_category_stats_endpoint_answers_with_the_figures(client):
    world = _world(client)
    answer = client.get(f"/api/households/{world['house']}/stats/categories")
    assert answer.status_code == 200, answer.text

    by_id = {row["category_id"]: row for row in answer.json()["categories"]}
    groceries = by_id[world["categories"]["Groceries"]["id"]]
    assert groceries["transaction_count"] == 3
    assert groceries["payee_count"] == 2
    assert [(one["name"], one["transaction_count"]) for one in groceries["payees"]] == [
        ("Carrefour", 2),
        ("Mercadona", 1),
    ]
    assert groceries["more_payees"] == 0

    eating_out = by_id[world["categories"]["Eating Out"]["id"]]
    assert eating_out["transaction_count"] == 1
    assert eating_out["payee_count"] == 1


def test_the_payee_stats_endpoint_answers_with_the_figures(client):
    world = _world(client)
    answer = client.get(f"/api/households/{world['house']}/stats/payees")
    assert answer.status_code == 200, answer.text

    payees = client.get(f"/api/households/{world['house']}/payees").json()
    by_name = {one["name"]: one["id"] for one in payees}
    stats = {row["payee_id"]: row for row in answer.json()["payees"]}

    carrefour = stats[by_name["Carrefour"]]
    assert carrefour["transaction_count"] == 4
    assert carrefour["category_count"] == 2
    assert [(one["name"], one["transaction_count"]) for one in carrefour["categories"]] == [
        ("Groceries", 2),
        ("Eating Out", 1),
        ("Uncategorised", 1),
    ]

    assert stats[by_name["Mercadona"]]["transaction_count"] == 1
    assert stats[by_name["Mercadona"]]["category_count"] == 1


def test_a_non_member_is_told_the_stats_do_not_exist(client):
    """404 and not 403, like every other household-scoped route: a 403 would
    confirm the id is real."""
    world = _world(client)
    stranger = "0" * 32

    assert client.get(f"/api/households/{stranger}/stats/categories").status_code == 404
    assert client.get(f"/api/households/{stranger}/stats/payees").status_code == 404

    # The household the caller *is* in still answers, so the 404 above is about
    # membership rather than about the route being broken.
    assert client.get(f"/api/households/{world['house']}/stats/categories").status_code == 200
    assert client.get(f"/api/households/{world['house']}/stats/payees").status_code == 200
