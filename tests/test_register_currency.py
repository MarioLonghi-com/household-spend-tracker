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
