"""A split statement line is not imported again (#331).

A split replaces the imported row with its parts, and the parts cannot share
the row's `import_id`. Before #331 they kept none of its keys, so the dedupe
saw the line as new: sending the same statement again -- which overlapping
monthly downloads do as a matter of course -- brought the split money in a
second time, and the balance moved by the whole amount.

Two accounts in two currencies, each imported, one row in each split, each file
sent again: nothing is created, and every balance is what it was.
"""

from __future__ import annotations

from tests.conftest import HEADERS
from tests.test_import_after_delete import POUNDS_FILE, _commit
from tests.test_import_queue import CARD_FILE, _register, _upload, _world


def _balance(client, house: str, account: str) -> int:
    return sum(row["amount"] for row in _register(client, house, account))


def test_a_split_line_is_skipped_when_the_statement_comes_again(client):
    world = _world(client)
    house = world["house"]
    pounds = client.post(
        f"/api/households/{house}/accounts",
        json={"name": "Pounds card", "type": "credit_card", "currency": "GBP"},
        headers=HEADERS,
    ).json()
    files = ((world["card"], CARD_FILE), (pounds["id"], POUNDS_FILE))

    for account, raw in files:
        staged = _upload(client, house, account, raw)
        assert staged.status_code == 201, staged.text
        assert _commit(client, house, staged.json()["batch_id"])["created"] == 3

    before = {account: _balance(client, house, account) for account, _ in files}
    assert before[world["card"]] != 0 and before[pounds["id"]] != 0

    for account, _ in files:
        row = min(_register(client, house, account), key=lambda r: r["amount"])
        first = row["amount"] // 2
        split = client.post(
            f"/api/transactions/{row['id']}/split",
            json={"parts": [{"amount": first}, {"amount": row["amount"] - first}]},
            headers=HEADERS,
        )
        assert split.status_code == 200, split.text
        assert len(_register(client, house, account)) == 4
        assert _balance(client, house, account) == before[account]

    for account, raw in files:
        again = _upload(client, house, account, raw, force=True)
        assert again.status_code == 201, again.text
        outcomes = [line["outcome"] for line in again.json()["lines"]]
        assert outcomes == ["duplicate_skipped"] * 3, outcomes
        assert _commit(client, house, again.json()["batch_id"])["created"] == 0

    for account, _ in files:
        assert len(_register(client, house, account)) == 4, "two whole rows and two parts"
        assert _balance(client, house, account) == before[account], "the split money once"


def test_the_parts_carry_the_line_they_came_from(client):
    """And only the line's keys: a part is still not an imported row of its own."""
    world = _world(client)
    house = world["house"]
    staged = _upload(client, house, world["card"], CARD_FILE)
    _commit(client, house, staged.json()["batch_id"])
    row = min(_register(client, house, world["card"]), key=lambda r: r["amount"])

    parts = client.post(
        f"/api/transactions/{row['id']}/split",
        json={"parts": [{"amount": row["amount"] + 100}, {"amount": -100}]},
        headers=HEADERS,
    ).json()

    assert [part["import_id"] for part in parts] == [None, None]
    from app.db import session_scope
    from app.models import Transaction

    with session_scope() as session:
        keys = [session.get(Transaction, part["id"]).import_alt_ids for part in parts]
    assert keys == [[row["import_id"]], [row["import_id"]]]
