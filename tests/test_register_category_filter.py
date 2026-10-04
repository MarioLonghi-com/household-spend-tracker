"""The register's category picker (#188).

It replaced the "Needs a category" checkbox. The backlog is now the picker's
first tick, and every tick *adds* rows: "Needs a category" plus Groceries is
the rows needing a category or in Groceries, the way ticking a second account
brings its rows in. `categorised` is every categorised row, which is how the
picker says "every category, not the backlog" without listing every id.
"""

from __future__ import annotations

from tests.conftest import HEADERS
from tests.test_register_backlog import _add, _register, _world


def _amounts_of(page) -> list[int]:
    return sorted(row["amount"] for row in page["transactions"])


def _with_a_transfer(client, world) -> None:
    moved = client.post(
        f"/api/households/{world['house']}/transfers",
        json={
            "from_account_id": world["checking"],
            "to_account_id": world["savings"],
            "date": "2026-01-15",
            "amount": 30_000,
            "to_amount": 25_000,
        },
        headers=HEADERS,
    )
    assert moved.status_code == 201, moved.text


def _ledger(client):
    """Groceries in both currencies, one Transport, two rows needing a category."""
    world = _world(client)
    house = world["house"]
    groceries = world["cat"]["Groceries"]["id"]
    transport = world["cat"]["Transport"]["id"]
    _add(client, house, world["checking"], "2026-01-05", -1_000, groceries)
    _add(client, house, world["savings"], "2026-01-06", -2_000, groceries)
    _add(client, house, world["checking"], "2026-01-07", -3_000, transport)
    _add(client, house, world["checking"], "2026-01-08", -4_000)
    _add(client, house, world["savings"], "2026-01-09", -5_000)
    _with_a_transfer(client, world)
    return world, groceries, transport


def test_several_categories_bring_in_each_ones_rows(client):
    world, groceries, transport = _ledger(client)
    page = _register(client, world["house"], f"category_id={groceries}&category_id={transport}")
    assert _amounts_of(page) == [-3_000, -2_000, -1_000]
    assert page["total"] == 3


def test_the_backlog_and_a_category_add_up_rather_than_narrow(client):
    """Either, not both: no row is uncategorised *and* in Groceries."""
    world, groceries, _ = _ledger(client)
    page = _register(client, world["house"], f"uncategorised=true&category_id={groceries}")
    assert _amounts_of(page) == [-5_000, -4_000, -2_000, -1_000]
    assert not [row for row in page["transactions"] if row["transfer_account_id"]], (
        "the backlog's tick still leaves transfers out"
    )


def test_categorised_is_every_row_with_a_category(client):
    """Every category ticked and the backlog not: no blanks, no transfers."""
    world, _, _ = _ledger(client)
    page = _register(client, world["house"], "categorised=true")
    assert _amounts_of(page) == [-3_000, -2_000, -1_000]


def test_the_category_filter_composes_with_the_account_filter(client):
    world, groceries, _ = _ledger(client)
    page = _register(
        client,
        world["house"],
        f"account_id={world['checking']}&uncategorised=true&category_id={groceries}",
    )
    assert _amounts_of(page) == [-4_000, -1_000]


def test_a_category_filter_withholds_the_running_balance(client):
    world, groceries, _ = _ledger(client)
    checking = world["checking"]
    assert _register(client, world["house"], f"account_id={checking}")["has_running_balance"]
    for query in (f"category_id={groceries}", "categorised=true"):
        page = _register(client, world["house"], f"account_id={checking}&{query}")
        assert page["has_running_balance"] is False, query
