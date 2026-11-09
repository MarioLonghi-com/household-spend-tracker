"""The register in more than one currency.

The register draws one IN/OUT pair per currency, so which currency a row is in
stopped being decoration the moment the columns multiplied: it decides which
column the figure lands in. The client used to work it out from the accounts
list, which leaves closed accounts out -- so a row on a closed account was
drawn in the household's base currency, in the wrong column, with nothing on
screen to say so.

Every assertion here is about a value in the response, not about a 200.

Two of everything, as the fixtures rule says, and two currencies especially:
EUR and GBP, each with an account that has rows in it, plus a third account
that is closed and still holds a currency of its own.
"""

from __future__ import annotations

import pytest

from tests.conftest import HEADERS, _setup_owner  # noqa: F401


@pytest.fixture()
def world(client) -> dict:
    """A household holding two currencies, one of them on a closed account."""
    _setup_owner(client)
    house = client.post(
        "/api/households", json={"name": "Ours", "base_currency": "EUR"}, headers=HEADERS
    ).json()

    def account(name: str, currency: str) -> dict:
        return client.post(
            f"/api/households/{house['id']}/accounts",
            json={"name": name, "type": "checking", "currency": currency},
            headers=HEADERS,
        ).json()

    spain = account("Spain Checking", "EUR")
    london = account("London Savings", "GBP")
    # Closed, and therefore missing from the accounts list the register draws:
    # its rows are the ones that used to be attributed to the base currency.
    old = account("Old Zurich", "CHF")

    rows = [
        (spain, "2026-01-05", -4_250),
        (spain, "2026-01-06", 120_000),
        (london, "2026-01-07", -8_000),
        (london, "2026-01-08", 5_000),
        (old, "2026-01-09", -1_000),
    ]
    for acct, when, amount in rows:
        made = client.post(
            f"/api/households/{house['id']}/transactions",
            json={"account_id": acct["id"], "date": when, "amount": amount},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text

    closed = client.patch(
        f"/api/accounts/{old['id']}", json={"closed": True}, headers=HEADERS
    )
    assert closed.status_code == 200, closed.text

    return {"house": house["id"], "spain": spain, "london": london, "old": old}


def _register(client, house: str, **params) -> dict:
    answer = client.get(f"/api/households/{house}/transactions", params=params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_every_row_says_which_currency_its_amount_is_in(client, world):
    page = _register(client, world["house"])

    by_account = {row["account_id"]: row["currency"] for row in page["transactions"]}
    assert by_account[world["spain"]["id"]] == "EUR"
    assert by_account[world["london"]["id"]] == "GBP"


def test_a_closed_account_keeps_its_own_currency(client, world):
    """The bug this field exists for.

    `GET /accounts` omits closed accounts, so a client deducing currency from
    that list had nothing to match this row against and fell back to the
    household's base currency -- EUR, for a Swiss franc row.
    """
    page = _register(client, world["house"])
    row = next(r for r in page["transactions"] if r["account_id"] == world["old"]["id"])

    assert row["currency"] == "CHF", "not the household's EUR"
    assert row["amount"] == -1_000

    listed = client.get(f"/api/households/{world['house']}/accounts").json()
    assert world["old"]["id"] not in {a["id"] for a in listed}, (
        "the premise: the accounts list this row would have been matched against "
        "does not contain its account"
    )


def test_a_single_row_read_carries_its_currency_too(client, world):
    """The panel keeps what a PATCH gives back, so that answer needs it as well."""
    page = _register(client, world["house"], account_id=world["london"]["id"])
    one = page["transactions"][0]

    patched = client.patch(
        f"/api/transactions/{one['id']}", json={"memo": "changed"}, headers=HEADERS
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["currency"] == "GBP"
    assert patched.json()["memo"] == "changed"


# --------------------------------------------------------------------------- #
# The account filter, in the plural
# --------------------------------------------------------------------------- #


def test_the_account_filter_takes_several_accounts(client, world):
    """The grouped picker's whole point: "these two accounts" is one request."""
    page = _register(
        client, world["house"], account_id=[world["spain"]["id"], world["london"]["id"]]
    )

    assert page["total"] == 4, "two accounts' rows, and not the third's"
    assert {row["currency"] for row in page["transactions"]} == {"EUR", "GBP"}
    assert world["old"]["id"] not in {row["account_id"] for row in page["transactions"]}


def test_one_account_still_behaves_exactly_as_it_did(client, world):
    page = _register(client, world["house"], account_id=world["spain"]["id"])

    assert page["total"] == 2
    assert page["has_running_balance"] is True
    # Newest first on screen; the balance accumulates oldest-first.
    assert [row["running_balance"] for row in page["transactions"]] == [115_750, -4_250]


def test_two_accounts_withhold_the_running_balance(client, world):
    """Two accounts is "across accounts" however the ids arrived, and a running
    balance down a page mixing two of them -- in two currencies -- would be a
    column of numbers that adds nothing up."""
    page = _register(
        client, world["house"], account_id=[world["spain"]["id"], world["london"]["id"]]
    )

    assert page["has_running_balance"] is False
    assert all(row["running_balance"] is None for row in page["transactions"])


# --------------------------------------------------------------------------- #
# Sorting a money column that holds two currencies
# --------------------------------------------------------------------------- #


def test_the_amount_column_sorts_by_currency_first_then_figure(client, world):
    """CLAUDE.md's rule for money across mixed currencies.

    Without it the order interleaves the currencies -- a GBP 80 outflow between
    two EUR ones -- and the column reads as though the four figures were
    comparable, which is the one thing this ledger never claims.
    """
    page = _register(client, world["house"], sort="amount", direction="asc")
    pairs = [(row["currency"], row["amount"]) for row in page["transactions"]]

    assert pairs == [
        ("CHF", -1_000),
        ("EUR", -4_250),
        ("EUR", 120_000),
        ("GBP", -8_000),
        ("GBP", 5_000),
    ]

    # Turned around, the whole order turns -- currencies and figures together,
    # the way `sortRows` reverses a tuple in the client.
    back = _register(client, world["house"], sort="amount", direction="desc")
    assert [(r["currency"], r["amount"]) for r in back["transactions"]] == list(
        reversed(pairs)
    )


# --------------------------------------------------------------------------- #
# The currency toggle, narrowed on the server (#370)
# --------------------------------------------------------------------------- #


def _more_euros(client, world, count: int = 3) -> None:
    """Enough EUR rows that the currency sorting first fills a page alone."""
    for at in range(count):
        made = client.post(
            f"/api/households/{world['house']}/transactions",
            json={
                "account_id": world["spain"]["id"],
                "date": f"2026-02-0{at + 1}",
                "amount": -(10_000 + at),
            },
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text


@pytest.mark.parametrize(
    ("shown", "direction", "crowded_out", "expected"),
    [
        # Under asc CHF and EUR -- six rows -- come before the first GBP one,
        # so a first page of two used to hold no pound at all.
        ("GBP", "asc", True, [("GBP", -8_000), ("GBP", 5_000)]),
        ("GBP", "desc", False, [("GBP", 5_000), ("GBP", -8_000)]),
        # Under desc both GBP rows come first and fill the page.
        ("EUR", "desc", True, [("EUR", 120_000), ("EUR", -4_250)]),
        ("EUR", "asc", False, [("EUR", -10_002), ("EUR", -10_001)]),
    ],
)
def test_the_toggle_pages_over_the_currency_it_shows(
    client, world, shown, direction, crowded_out, expected
):
    """The bug: one currency shown, sorted by its amount, and the table empty.

    The toggle used to filter the rows already in hand, after paging, while
    the amount sort is currency first -- so the first page was all the other
    currency. Asked for on the server, the first page is this currency's rows
    in this currency's order.
    """
    _more_euros(client, world)
    unfiltered = _register(
        client, world["house"], sort="amount", direction=direction, limit=2
    )
    unfiltered_shown = [r for r in unfiltered["transactions"] if r["currency"] == shown]
    assert (unfiltered_shown == []) is crowded_out, (
        "the premise: without the filter, the first page is another currency's "
        "exactly when that currency sorts first"
    )

    page = _register(
        client, world["house"], sort="amount", direction=direction, limit=2, currency=shown
    )
    assert [(row["currency"], row["amount"]) for row in page["transactions"]] == expected
    held = {"GBP": 2, "EUR": 5}[shown]
    assert page["total"] == held
    # Eight rows in all; the rest are what the line under the table counts.
    assert page["hidden_by_currency"] == 8 - held
    assert page["capped"] is (held > 2)


def test_two_currencies_keep_both_and_leave_the_third_out(client, world):
    page = _register(
        client, world["house"], sort="amount", direction="asc", currency=["EUR", "GBP"]
    )

    assert [(row["currency"], row["amount"]) for row in page["transactions"]] == [
        ("EUR", -4_250),
        ("EUR", 120_000),
        ("GBP", -8_000),
        ("GBP", 5_000),
    ]
    assert page["total"] == 4
    assert page["hidden_by_currency"] == 1, "the closed CHF account's row"


def test_no_toggle_hides_nothing_and_says_so(client, world):
    page = _register(client, world["house"])

    assert page["total"] == 5
    assert page["hidden_by_currency"] == 0


def test_a_currency_nobody_holds_matches_no_row_and_counts_every_one(client, world):
    """Toggling to a currency with no rows hides everything -- the screen's
    "every row here is in a currency the toggle is hiding" -- and the count is
    what it says is waiting."""
    page = _register(client, world["house"], currency="USD")

    assert page["transactions"] == []
    assert page["total"] == 0
    assert page["hidden_by_currency"] == 5


def test_the_badge_counts_the_backlog_in_the_currencies_shown(client, world):
    """Every row in the fixture needs a category; the badge counts these rows."""
    assert _register(client, world["house"])["needs_category"] == 5
    assert _register(client, world["house"], currency="GBP")["needs_category"] == 2


def test_the_toggle_keeps_one_accounts_running_balance_when_it_shows_its_currency(
    client, world
):
    """One account in its own currency hides none of its rows, so its balance
    is still the account's; in another currency there is nothing to sum."""
    own = _register(client, world["house"], account_id=world["spain"]["id"], currency="EUR")
    assert own["has_running_balance"] is True
    assert [row["running_balance"] for row in own["transactions"]] == [115_750, -4_250]

    other = _register(client, world["house"], account_id=world["spain"]["id"], currency="GBP")
    assert other["transactions"] == []
    assert other["has_running_balance"] is False
    assert other["hidden_by_currency"] == 2


def test_the_toggle_is_one_households(client, world):
    """A second household holding pounds adds nothing to the first's GBP view."""
    second = client.post(
        "/api/households", json={"name": "Theirs", "base_currency": "GBP"}, headers=HEADERS
    ).json()
    theirs = client.post(
        f"/api/households/{second['id']}/accounts",
        json={"name": "Their Pounds", "type": "checking", "currency": "GBP"},
        headers=HEADERS,
    ).json()
    made = client.post(
        f"/api/households/{second['id']}/transactions",
        json={"account_id": theirs["id"], "date": "2026-01-10", "amount": -777},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text

    ours = _register(client, world["house"], currency="GBP")
    assert ours["total"] == 2
    assert {row["account_id"] for row in ours["transactions"]} == {world["london"]["id"]}
    assert _register(client, second["id"], currency="GBP")["total"] == 1
