"""The register as a worklist: what still needs a category, oldest first.

Categorising is the one recurring chore in this app, and until now the only
route to the backlog was sorting by Category and scrolling to wherever the
blanks landed. Uncategorised existed as a *destination* -- the bulk bar offers
it as something to set -- and not as a filter. Issue #54.

The running-balance assertions here are the other half of the same change.
`register()` withholds the balance under "a filter that hides rows", which is
what its own comment says it does, and `cleared` was already outside that
check: narrowing to uncleared rows left the column switched on and summing a
subset. Adding a second such filter is what made the first one visible.
"""

from __future__ import annotations

from tests.conftest import HEADERS, _setup_owner


def _world(client) -> dict:
    """Two accounts, two currencies, two categories -- and a transfer.

    Two of everything, per the standing rule. The second currency is not
    decoration: the backlog is a filter, and a filter that quietly dropped one
    account's rows would look exactly like a filter that worked.
    """
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    checking = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking", "currency": "EUR"},
        headers=HEADERS,
    ).json()
    savings = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Sterling savings", "type": "savings", "currency": "GBP"},
        headers=HEADERS,
    ).json()

    tree = client.get(f"/api/households/{house['id']}/categories", headers=HEADERS).json()
    by_name = {one["name"]: one for group in tree for one in group["categories"]}

    return {
        "house": house["id"],
        "checking": checking["id"],
        "savings": savings["id"],
        "cat": by_name,
    }


def _add(client, house: str, account: str, day: str, amount: int, category: str | None = None):
    body = {"account_id": account, "date": day, "amount": amount}
    if category:
        body["category_id"] = category
    made = client.post(
        f"/api/households/{house}/transactions", json=body, headers=HEADERS
    )
    assert made.status_code == 201, made.text
    return made.json()


def _register(client, house: str, query: str = ""):
    answer = client.get(f"/api/households/{house}/transactions?{query}")
    assert answer.status_code == 200, answer.text
    return answer.json()


# --------------------------------------------------------------------------- #
# What it selects
# --------------------------------------------------------------------------- #


def test_the_backlog_is_only_the_rows_with_no_category(client):
    world = _world(client)
    house = world["house"]
    groceries = world["cat"]["Groceries"]["id"]

    done = _add(client, house, world["checking"], "2026-01-05", -1_000, groceries)
    todo_eur = _add(client, house, world["checking"], "2026-01-06", -2_000)
    todo_gbp = _add(client, house, world["savings"], "2026-01-07", -3_000)

    page = _register(client, house, "uncategorised=true")

    ids = {row["id"] for row in page["transactions"]}
    assert ids == {todo_eur["id"], todo_gbp["id"]}, "it selected the wrong rows"
    assert done["id"] not in ids, "a categorised row is not in the backlog"
    assert page["total"] == 2, "the count is what the toggle shows as the backlog size"


def test_a_transfer_is_never_in_the_backlog(client):
    """It has no category by design, so it could never leave the list.

    A worklist containing rows nobody can action is not a worklist -- it is a
    number that never reaches zero.
    """
    world = _world(client)
    house = world["house"]
    _add(client, house, world["checking"], "2026-01-01", 100_000)

    moved = client.post(
        f"/api/households/{house}/transfers",
        json={
            "from_account_id": world["checking"],
            "to_account_id": world["savings"],
            "date": "2026-01-15",
            "amount": 30_000,
            # EUR out, GBP in: this ledger never invents a rate.
            "to_amount": 25_000,
        },
        headers=HEADERS,
    )
    assert moved.status_code == 201, moved.text

    everything = _register(client, house)
    legs = [row for row in everything["transactions"] if row["transfer_account_id"]]
    assert len(legs) == 2, "the fixture should have both legs of one transfer"
    assert all(leg["category_id"] is None for leg in legs), "a transfer has no category"

    backlog = _register(client, house, "uncategorised=true")
    assert not [row for row in backlog["transactions"] if row["transfer_account_id"]], (
        "a transfer leg has no category by design and would sit here forever"
    )


def test_the_filter_is_off_unless_it_is_asked_for(client):
    """The register's own default is unchanged."""
    world = _world(client)
    house = world["house"]
    _add(client, house, world["checking"], "2026-01-05", -1_000, world["cat"]["Groceries"]["id"])
    _add(client, house, world["checking"], "2026-01-06", -2_000)

    assert _register(client, house)["total"] == 2
    assert _register(client, house, "uncategorised=false")["total"] == 2


def test_the_backlog_composes_with_the_other_filters(client):
    """It narrows what is already narrowed, rather than replacing it."""
    world = _world(client)
    house = world["house"]
    _add(client, house, world["checking"], "2026-01-06", -2_000)
    _add(client, house, world["savings"], "2026-01-07", -3_000)

    both = _register(client, house, "uncategorised=true")
    assert both["total"] == 2

    one = _register(client, house, f"uncategorised=true&account_id={world['checking']}")
    assert one["total"] == 1, "the account filter still applies"

    none = _register(client, house, "uncategorised=true&since=2026-06-01")
    assert none["total"] == 0, "the date range still applies"


def test_the_backlog_can_be_read_oldest_first(client):
    """The order the client switches to, asserted where it is produced.

    Newest-first is right for "what happened lately" and wrong for clearing a
    backlog: you start at the end and the list reshuffles as you work.
    """
    world = _world(client)
    house = world["house"]
    for day in ("2026-03-01", "2026-01-01", "2026-02-01"):
        _add(client, house, world["checking"], day, -1_000)

    page = _register(client, house, "uncategorised=true&sort=date&direction=asc")
    assert [row["date"] for row in page["transactions"]] == [
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
    ]


# --------------------------------------------------------------------------- #
# What it must not claim
# --------------------------------------------------------------------------- #


def test_the_backlog_withholds_the_running_balance(client):
    """It hides rows, so a sum down the page is not the account's balance."""
    world = _world(client)
    house, checking = world["house"], world["checking"]
    _add(client, house, checking, "2026-01-05", 100_000, world["cat"]["Groceries"]["id"])
    _add(client, house, checking, "2026-01-10", -25_000)

    whole = _register(client, house, f"account_id={checking}")
    assert whole["has_running_balance"] is True, "the control: unfiltered, it is computable"
    assert [row["running_balance"] for row in whole["transactions"]] == [75_000, 100_000]

    backlog = _register(client, house, f"account_id={checking}&uncategorised=true")
    assert backlog["has_running_balance"] is False
    assert all(row["running_balance"] is None for row in backlog["transactions"]), (
        "75,000 alone is not this account's balance, and looks exactly like it"
    )


def test_the_cleared_filter_withholds_it_too(client):
    """It always hid rows; it was never in the check. Fixed alongside #54."""
    world = _world(client)
    house, checking = world["house"], world["checking"]
    first = _add(client, house, checking, "2026-01-05", 100_000)
    _add(client, house, checking, "2026-01-10", -25_000)

    marked = client.patch(
        f"/api/transactions/{first['id']}",
        json={"cleared": "cleared"},
        headers=HEADERS,
    )
    assert marked.status_code == 200, marked.text

    page = _register(client, house, f"account_id={checking}&cleared=cleared")
    assert page["total"] == 1, "the fixture must actually be narrowed for this to mean anything"
    assert page["has_running_balance"] is False, (
        "it summed a subset down the page and called it the balance"
    )
    assert page["transactions"][0]["running_balance"] is None


# --------------------------------------------------------------------------- #
# One filter language, so an agent that learned it once knows it everywhere
# --------------------------------------------------------------------------- #


def test_the_manifest_publishes_the_word(client):
    """A filter the code has and the manifest does not is a filter no agent uses.

    The other direction is worse: a word published and not implemented is a lie
    an agent acts on. Asserted against the constant the endpoints actually
    take, not against a copy of it.
    """
    import inspect

    from app.api.routers import agent as agent_router

    assert "uncategorised" in agent_router._FILTERS

    for endpoint in (agent_router.summary, agent_router.timeseries):
        assert "uncategorised" in inspect.signature(endpoint).parameters, (
            f"{endpoint.__name__} is described by _FILTERS and does not take it"
        )


def test_one_filter_language_means_one_clause(session, household, accounts, owner):
    """`filtered()` is what the register and the agent aggregates share.

    Tested here rather than through two routes, because the point is that there
    is exactly one implementation: a second copy is how the register and the
    summary end up disagreeing about what a word means.
    """
    from datetime import date

    from app.audit.batch import batch
    from app.models import BatchKind, Category, CategoryGroup, Transaction
    from app.services import transactions as txn_service

    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        group = CategoryGroup(household_id=household.id, name="Everyday", sort_order=1)
        session.add(group)
        session.flush()
        food = Category(household_id=household.id, group_id=group.id, name="Groceries")
        session.add(food)
        session.flush()
        session.add_all(
            [
                Transaction(
                    household_id=household.id, account_id=checking.id,
                    date=date(2026, 1, 5), amount=-1_000, category_id=food.id,
                ),
                Transaction(
                    household_id=household.id, account_id=checking.id,
                    date=date(2026, 1, 6), amount=-2_000,
                ),
            ]
        )
    session.flush()

    everything = session.execute(txn_service.filtered(household.id)).scalars().all()
    backlog = session.execute(
        txn_service.filtered(household.id, uncategorised=True)
    ).scalars().all()

    assert len(everything) == 2, "the fixture must have both for this to mean anything"
    assert [row.amount for row in backlog] == [-2_000], "it selected the wrong row"
    assert all(row.category_id is None for row in backlog)

    # And the clause is additive, not a replacement for the rest of the language.
    narrowed = session.execute(
        txn_service.filtered(household.id, uncategorised=True, since=date(2026, 6, 1))
    ).scalars().all()
    assert narrowed == [], "the date range stopped applying"
