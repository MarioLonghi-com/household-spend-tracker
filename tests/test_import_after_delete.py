"""A statement line whose row was deleted comes back on the next import (#91).

Decided 2026-10-07: deletes are hard, the dedupe is against rows that exist
(`importing._import_ids_in`), and the statement is the record -- so a line
whose row was deleted is new again. Undo in History is the other way back.

Two accounts in two currencies, each imported, each with one row deleted, each
file sent again: the deleted line returns with the identity it had, and the
rows that never went are not doubled.
"""

from __future__ import annotations

from tests.conftest import HEADERS
from tests.test_import_queue import CARD_FILE, _register, _upload, _world

POUNDS_FILE = CARD_FILE.replace("<CURDEF>EUR</CURDEF>", "<CURDEF>GBP</CURDEF>").replace(
    "CARD|11223", "CARD|44556"
).replace("QQ000", "PP000")


def _commit(client, house: str, batch_id: str) -> dict:
    done = client.post(f"/api/households/{house}/imports/{batch_id}/commit", json={}, headers=HEADERS)
    assert done.status_code == 200, done.text
    return done.json()


def _by_import_id(client, house: str, account: str) -> dict[str, dict]:
    return {row["import_id"]: row for row in _register(client, house, account)}


def test_a_deleted_line_comes_back_with_the_same_import_id_and_nothing_doubles(client):
    world = _world(client)
    house = world["house"]
    pounds = client.post(
        f"/api/households/{house}/accounts",
        json={"name": "Pounds card", "type": "credit_card", "currency": "GBP"},
        headers=HEADERS,
    ).json()

    before: dict[str, dict[str, dict]] = {}
    for account, raw in ((world["card"], CARD_FILE), (pounds["id"], POUNDS_FILE)):
        staged = _upload(client, house, account, raw)
        assert staged.status_code == 201, staged.text
        assert _commit(client, house, staged.json()["batch_id"])["created"] == 3
        before[account] = _by_import_id(client, house, account)
        assert len(before[account]) == 3 and None not in before[account]

    gone = {}
    for account in (world["card"], pounds["id"]):
        import_id, row = sorted(before[account].items())[1]
        assert client.delete(f"/api/transactions/{row['id']}", headers=HEADERS).status_code == 204
        gone[account] = (import_id, row)
        assert len(_register(client, house, account)) == 2

    for account, raw in ((world["card"], CARD_FILE), (pounds["id"], POUNDS_FILE)):
        # The very same file is refused while its first import stands ...
        assert _upload(client, house, account, raw).status_code == 409
        # ... and sent again on purpose, only the deleted line is new.
        again = _upload(client, house, account, raw, force=True)
        assert again.status_code == 201, again.text
        outcomes = sorted(line["outcome"] for line in again.json()["lines"])
        assert outcomes == ["created", "duplicate_skipped", "duplicate_skipped"]
        assert _commit(client, house, again.json()["batch_id"])["created"] == 1

    for account in (world["card"], pounds["id"]):
        now = _by_import_id(client, house, account)
        assert set(now) == set(before[account]), "the same three lines, none doubled"
        import_id, old = gone[account]
        back = now[import_id]
        assert back["id"] != old["id"], "a new row, carrying the line's identity"
        assert (back["amount"], back["date"]) == (old["amount"], old["date"])
    assert {row["amount"] for row in _register(client, house, pounds["id"])} == {-4250, -1800, -940}
