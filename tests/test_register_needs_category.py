"""The "Needs a category" count rides on the register's own answer (#101).

It used to be a second request from the screen, to the same path with
`uncategorised=true&limit=1`, fired in the same tick as the register's own.
`access.log` drops the query string, so every load and every refresh showed
up as two identical GETs a few milliseconds apart. The count is now a field of
the one response: the rows needing a category under every *other* filter of
the request, with the category picker set aside -- which is exactly what the
second request used to ask.
"""

from __future__ import annotations

from tests.conftest import HEADERS
from tests.test_register_backlog import _add, _register
from tests.test_register_category_filter import _ledger


def _second_household(client) -> str:
    """Another household of the same user, with a blank row of its own."""
    other = client.post("/api/households", json={"name": "Theirs"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{other['id']}/accounts",
        json={"name": "Elsewhere", "type": "checking", "currency": "GBP"},
        headers=HEADERS,
    ).json()
    _add(client, other["id"], account["id"], "2026-01-10", -7_000)
    _add(client, other["id"], account["id"], "2026-01-11", -8_000)
    return other["id"]


def test_the_count_is_the_blank_rows_and_not_the_transfer(client):
    world, _, _ = _ledger(client)
    page = _register(client, world["house"])
    assert page["needs_category"] == 2


def test_the_count_sets_the_category_picker_aside(client):
    """Groceries ticked alone still says how many need a category."""
    world, groceries, _ = _ledger(client)
    page = _register(client, world["house"], f"category_id={groceries}")
    assert page["total"] == 2, "the rows are Groceries'"
    assert page["needs_category"] == 2, "the badge is the backlog's, not the rows'"


def test_the_count_follows_every_other_filter(client):
    world, _, _ = _ledger(client)
    house = world["house"]
    assert _register(client, house, f"account_id={world['checking']}")["needs_category"] == 1
    assert _register(client, house, f"account_id={world['savings']}")["needs_category"] == 1
    assert _register(client, house, "until=2026-01-08")["needs_category"] == 1
    assert _register(client, house, "amount=50")["needs_category"] == 1


def test_another_households_blank_rows_are_not_counted(client):
    world, _, _ = _ledger(client)
    other = _second_household(client)
    assert _register(client, world["house"])["needs_category"] == 2
    assert _register(client, other)["needs_category"] == 2
    assert _register(client, other)["total"] == 2


def test_categorising_a_row_takes_it_off_the_count(client):
    world, groceries, _ = _ledger(client)
    house = world["house"]
    blank = next(
        row
        for row in _register(client, house, "uncategorised=true")["transactions"]
        if row["amount"] == -4_000
    )
    before = _register(client, house)["needs_category"]

    moved = client.patch(
        f"/api/transactions/{blank['id']}", json={"category_id": groceries}, headers=HEADERS
    )
    assert moved.status_code == 200, moved.text

    assert before == 2
    assert _register(client, house)["needs_category"] == 1
