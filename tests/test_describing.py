"""The audit log, in sentences.

These matter more than they look. History is what the undo button hangs off,
and undo rewrites rows somebody may have spent an evening getting right. A
record reading "manual · applied" gives no basis for that decision, so every
assertion here is about the sentence naming what actually happened.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.models import BatchKind, Categorisation, ClearedState
from app.services import categories as category_service
from app.services import describing, reconciling
from app.services import payees as payee_service
from app.services import transactions as txn_service

JAN = date(2026, 1, 15)
FEB = date(2026, 2, 20)


@pytest.fixture()
def write(session, owner, household):
    def _open(kind=BatchKind.manual):
        return batch(session, kind=kind, actor_id=owner.id, household_id=household.id)

    return _open


@pytest.fixture()
def tree(session, household, write):
    with write(BatchKind.admin):
        category_service.seed_defaults(session, household.id)
    return {one.name: one for one in category_service.list_categories(session, household.id)}


# --------------------------------------------------------------------------- #
# A single edit
# --------------------------------------------------------------------------- #


def test_adding_a_transaction_names_what_was_added(session, accounts, write):
    with write() as made:
        payee = payee_service.get_or_create(session, accounts["checking"].household_id, "Repsol")
        txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-7_978, payee=payee
        )

    words = describing.describe(session, made)
    assert words.headline == "Edit"
    assert "Added" in words.detail
    assert "79.78" in words.detail, words.detail
    assert "Repsol" in words.detail
    assert "Checking" in words.detail


def test_deleting_says_removed_not_added(session, accounts, write):
    with write():
        txn = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-1_000
        )
    with write() as removed:
        txn_service.delete(session, txn)

    detail = describing.describe(session, removed).detail
    assert "Removed" in detail, detail
    assert "Added" not in detail


def test_a_changed_field_is_reported_from_and_to(session, accounts, tree, write):
    """The whole point: not "a transaction changed" but which field, and how."""
    with write():
        txn = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-7_978, category=None
        )
    with write() as edited:
        txn_service.update(session, txn, category=tree["Transport"])

    detail = describing.describe(session, edited).detail
    assert "category" in detail
    assert "uncategorised" in detail, detail
    assert "Everyday: Transport" in detail, detail


def test_an_emptied_field_is_named_rather_than_called_nothing(
    session, accounts, tree, write
):
    """"nothing" is true and useless; "uncategorised" is a state you recognise."""
    with write():
        txn = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-1_000,
            category=tree["Groceries"],
        )
    with write() as cleared:
        txn_service.update(session, txn, category=None)

    detail = describing.describe(session, cleared).detail
    assert "uncategorised" in detail, detail


def test_the_amount_reads_in_the_accounts_own_currency(session, accounts, write):
    """The pounds account must not have its rows described in euro."""
    with write() as made:
        txn_service.create(session, account=accounts["pounds"], date=JAN, amount=-4_250)

    detail = describing.describe(session, made).detail
    assert "£" in detail, detail


def test_who_did_it_is_a_name_not_an_id(session, accounts, write, owner):
    with write() as made:
        txn_service.create(session, account=accounts["checking"], date=JAN, amount=-100)

    assert describing.describe(session, made).actor == owner.display_name


# --------------------------------------------------------------------------- #
# Acts too big to spell out
# --------------------------------------------------------------------------- #


def test_many_changes_are_counted_by_what_they_touched(session, accounts, write):
    with write(BatchKind.bulk_update) as bulk:
        for _ in range(4):
            txn_service.create(session, account=accounts["checking"], date=JAN, amount=-100)

    detail = describing.describe(session, bulk).detail
    assert "4 transactions" in detail, detail
    assert "added" in detail, detail


def test_seeding_categories_says_what_arrived(session, household, write):
    with write(BatchKind.admin) as seeded:
        category_service.seed_defaults(session, household.id)

    detail = describing.describe(session, seeded).detail
    assert "categories" in detail
    assert "category groups" in detail
    assert "added" in detail


# --------------------------------------------------------------------------- #
# The kinds with something specific to say
# --------------------------------------------------------------------------- #


def test_a_reconciliation_names_the_account_the_balance_and_the_date(
    session, accounts, write
):
    rows = []
    with write():
        for amount in (-1_000, -2_500, 8_000):
            rows.append(
                txn_service.create(
                    session, account=accounts["checking"], date=JAN, amount=amount
                )
            )

    with write(BatchKind.reconciled) as done:
        reconciling.reconcile(
            session,
            accounts["checking"],
            statement_date=FEB,
            statement_balance=4_500,
            transaction_ids=[one.id for one in rows],
            batch_id=done.id,
        )

    words = describing.describe(session, done)
    assert words.headline == "Reconciliation"
    assert "Checking" in words.detail
    assert "45.00" in words.detail, words.detail
    assert "2026-02-20" in words.detail
    assert "3 transactions locked" in words.detail, words.detail


def test_an_undo_says_what_it_reversed(session, accounts, household, owner, write):
    """"4 transactions changed" is what happened and not why."""
    with write() as original:
        txn_service.create(session, account=accounts["checking"], date=JAN, amount=-7_978)
    original_id = original.id

    with batch(
        session, kind=BatchKind.undo, actor_id=owner.id, household_id=household.id
    ) as undone:
        undo_batch(session, original_id, actor_id=owner.id)

    words = describing.describe(session, undone)
    assert words.headline == "Undo"
    assert "Reversed" in words.detail, words.detail
    assert "79.78" in words.detail, words.detail


def test_an_empty_batch_says_so_rather_than_inventing_a_sentence(
    session, household, owner
):
    with batch(
        session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id
    ) as nothing:
        pass

    assert describing.describe(session, nothing).detail == "Nothing was changed."


# --------------------------------------------------------------------------- #
# The lines the confirmation reads
# --------------------------------------------------------------------------- #


def test_the_lines_are_one_per_changed_row(session, accounts, write):
    with write(BatchKind.bulk_update) as bulk:
        for amount in (-100, -200, -300):
            txn_service.create(session, account=accounts["checking"], date=JAN, amount=amount)

    words = describing.describe(session, bulk, with_lines=True)
    assert len(words.lines) == 3
    assert all("Added transaction" in line for line in words.lines)
    assert any("3.00" in line for line in words.lines), words.lines


def test_the_lines_are_only_built_when_asked_for(session, accounts, write):
    """The list screen shows a hundred batches and needs none of them."""
    with write() as made:
        txn_service.create(session, account=accounts["checking"], date=JAN, amount=-100)

    assert describing.describe(session, made).lines == []
    assert describing.describe(session, made, with_lines=True).lines != []


def test_a_payee_rule_change_is_described(session, household, tree, write):
    with write() as made:
        payee = payee_service.get_or_create(session, household.id, "Netflix")
    with write(BatchKind.admin) as ruled:
        category_service.set_rule(
            session, payee, mode=Categorisation.fixed, category_id=tree["Subscriptions"].id
        )

    detail = describing.describe(session, ruled).detail
    assert "Netflix" in detail, detail
    assert "categorisation" in detail or "default category" in detail, detail
    assert describing.describe(session, made).detail.startswith("Added")


def test_a_cleared_state_change_reads_in_the_words_the_register_uses(
    session, accounts, write
):
    with write():
        txn = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-100
        )
    with write() as changed:
        txn_service.update(session, txn, cleared=ClearedState.cleared)

    detail = describing.describe(session, changed).detail
    assert "state" in detail
    assert "cleared" in detail, detail


# --------------------------------------------------------------------------- #
# Structure beside the words, for a client in another language (#57)
# --------------------------------------------------------------------------- #


def test_the_headline_comes_with_a_key_and_keeps_its_english(session, accounts, tree, write):
    with write() as made:
        txn_service.create(session, account=accounts["checking"], date=JAN, amount=-1_000)
    with write(BatchKind.bulk_update) as bulk:
        for one in (accounts["checking"], accounts["pounds"]):
            txn_service.create(session, account=one, date=FEB, amount=-2_000)

    edit = describing.describe(session, made)
    assert (edit.headline_key, edit.headline) == ("manual", "Edit")
    many = describing.describe(session, bulk)
    assert (many.headline_key, many.headline) == ("bulk_update", "Bulk edit")


def test_a_changed_column_and_its_table_travel_as_keys(session, accounts, tree, write):
    with write():
        txn = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-7_978, category=None
        )
    with write() as edited:
        txn_service.update(session, txn, category=tree["Transport"], memo="Fuel")

    changes = describing._Changes(session, edited.id, None).all()
    [row] = describing.detail_of(session, accounts["checking"].household_id, changes)
    assert (row.table_key, row.table) == ("transactions", "transaction")
    named = {one.column: one.field for one in row.fields}
    # The English word is unchanged; the key is the column it names.
    assert named["category_id"] == "category"
    assert named["memo"] == "memo"


def test_the_client_words_every_key_in_the_servers_english():
    """`client/src/lib/historyWords.ts` has a message for every key the server
    sends, and its English is the server's word, letter for letter -- that
    English is what every translation starts from."""
    import json
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "client/src/lib/historyWords.ts").read_text(
        encoding="utf-8"
    )

    def section(name: str) -> dict[str, str]:
        body = text.split(f"export const {name}")[1].split("\n};")[0]
        return {
            json.loads(key) if key.startswith('"') else key: json.loads(message)
            for key, message in re.findall(
                r'get ("[^"]+"|\w+)\(\) \{\s*return t\(\{ message: ("(?:[^"\\]|\\.)*")', body
            )
        }

    headlines = {kind.value: words for kind, words in describing.HEADLINES.items()}
    headlines |= describing.SPECIAL_HEADLINES | describing.ONE_TIME_HEADLINES
    assert section("HISTORY_HEADLINES") == headlines
    assert section("HISTORY_FIELDS") == describing.FIELD_WORDS
    assert section("HISTORY_TABLES") == {table: one for table, (one, _) in describing.NOUNS.items()}


# --------------------------------------------------------------------------- #
# Sentences and values as structure (#266)
# --------------------------------------------------------------------------- #

ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


def _nodes(node):
    """Every phrase and value inside one, depth first."""
    if isinstance(node, dict):
        yield node
        children = node["params"].values() if "params" in node else node.get("items", [])
        for child in children:
            yield from _nodes(child)


def _assert_raw(node) -> None:
    """Money as minor units with a currency, dates as ISO, never formatted text."""
    import re

    for one in _nodes(node):
        if "params" in one:
            assert one["key"] in describing.SENTENCES, one
            continue
        if one["type"] == "money":
            assert type(one["amount"]) is int and re.fullmatch(r"[A-Z]{3}", one["currency"]), one
        if one["type"] == "date":
            assert re.match(ISO_DATE, one["value"]), one
        if one["type"] in ("name", "text"):
            assert not re.search(r"[€£]\s?\d", one["value"]), f"a formatted amount went out as text: {one}"


def _edited_in_two_currencies(session, accounts, tree, write):
    with write():
        bakery = payee_service.get_or_create(session, accounts["checking"].household_id, "Bakery")
        euros = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-1_250, payee=bakery
        )
        pounds = txn_service.create(session, account=accounts["pounds"], date=JAN, amount=-4_000)
    with write() as euro_edit:
        txn_service.update(session, euros, amount=-1_300, category=tree["Groceries"], cleared=ClearedState.cleared)
    with write() as pound_edit:
        txn_service.update(session, pounds, amount=-4_500, memo="Train")
    return euro_edit, pound_edit


def test_a_line_goes_out_as_a_phrase_with_raw_values_in_each_accounts_currency(
    session, accounts, tree, write
):
    euro_edit, pound_edit = _edited_in_two_currencies(session, accounts, tree, write)

    euros = describing.describe(session, euro_edit, with_lines=True)
    pounds = describing.describe(session, pound_edit, with_lines=True)

    # English is the phrase rendered, and is what it always said.
    assert euros.detail == (
        "-€13.00 · Bakery · in Checking: amount -€12.50 → -€13.00, "
        "category uncategorised → Everyday: Groceries, state uncleared → cleared"
    )
    assert pounds.detail == "-£45.00 · in UK Savings: amount -£40.00 → -£45.00, memo no memo → Train"
    assert describing.english(euros.detail_phrase) == euros.detail
    assert [describing.english(one) for one in pounds.line_phrases] == pounds.lines

    phrase = euros.detail_phrase
    assert phrase["key"] == "history.line.fields"
    amount, category, cleared = (one["params"] for one in phrase["params"]["fields"]["items"])
    assert (amount["was"], amount["now"]) == (
        {"type": "money", "amount": -1_250, "currency": "EUR"},
        {"type": "money", "amount": -1_300, "currency": "EUR"},
    )
    assert category["was"] == {"type": "word", "set": "empty", "key": "category_id"}
    assert category["now"] == {"type": "category", "group": "Everyday", "name": "Groceries"}
    assert cleared["now"] == {"type": "enum", "column": "cleared", "value": "cleared"}
    pound_amount = pounds.detail_phrase["params"]["fields"]["items"][0]["params"]["now"]
    assert pound_amount == {"type": "money", "amount": -4_500, "currency": "GBP"}
    _assert_raw(phrase)
    _assert_raw(pounds.detail_phrase)


def test_each_changed_field_carries_its_values_raw(session, accounts, tree, write):
    euro_edit, _ = _edited_in_two_currencies(session, accounts, tree, write)
    changes = describing._Changes(session, euro_edit.id, None).all()
    [row] = describing.detail_of(session, accounts["checking"].household_id, changes)

    by_column = {one.column: one for one in row.fields}
    assert (by_column["amount"].was, by_column["amount"].now) == ("-€12.50", "-€13.00")
    assert by_column["amount"].now_value == {"type": "money", "amount": -1_300, "currency": "EUR"}
    assert row.summary == describing.english(row.summary_phrase)
    for one in row.fields:
        assert describing.english(one.was_value) == one.was
        assert describing.english(one.now_value) == one.now


def test_a_snapshot_dates_as_iso_and_has_no_from(session, accounts, write):
    with write() as made:
        txn_service.create(session, account=accounts["pounds"], date=FEB, amount=-990, memo="Tea")
    changes = describing._Changes(session, made.id, None).all()
    [row] = describing.detail_of(session, accounts["pounds"].household_id, changes)

    by_column = {one.column: one for one in row.snapshot}
    assert by_column["date"].now_value == {"type": "date", "value": "2026-02-20"}
    assert by_column["amount"].now_value == {"type": "money", "amount": -990, "currency": "GBP"}
    assert all(one.was == "" and one.was_value is None for one in row.snapshot)
    assert row.summary == "Added transaction -£9.90 · in UK Savings"
    assert row.summary_phrase["key"] == "history.line.added"


def test_counts_reconciliations_and_undos_go_out_as_structure(
    session, accounts, household, owner, write
):
    with write(BatchKind.bulk_update) as bulk:
        rows = [
            txn_service.create(session, account=accounts["checking"], date=JAN, amount=amount)
            for amount in (-1_000, 5_500)
        ]
    with write(BatchKind.reconciled) as done:
        reconciling.reconcile(
            session,
            accounts["checking"],
            statement_date=FEB,
            statement_balance=4_500,
            transaction_ids=[one.id for one in rows],
            batch_id=done.id,
        )
    with batch(session, kind=BatchKind.undo, actor_id=owner.id, household_id=household.id) as undone:
        undo_batch(session, done.id, actor_id=owner.id)

    many = describing.describe(session, bulk)
    assert many.detail == "2 transactions added."
    assert many.detail_phrase == {
        "key": "history.detail.tally",
        "params": {
            "tally": {
                "key": "history.tally.added",
                "params": {
                    "counts": {
                        "type": "list",
                        "items": [{"type": "count", "table": "transactions", "count": 2}],
                        "sep": ", ",
                    }
                },
            }
        },
    }

    proved = describing.describe(session, done).detail_phrase
    assert proved["key"] == "history.detail.reconciled"
    assert proved["params"]["date"] == {"type": "date", "value": "2026-02-20"}
    assert proved["params"]["balance"] == {"type": "money", "amount": 4_500, "currency": "EUR"}
    assert proved["params"]["count"] == 2

    reversed_ = describing.describe(session, undone)
    assert reversed_.detail.startswith("Reversed: reconciliation — Checking proved against")
    assert reversed_.detail_phrase["params"]["headline"] == {
        "type": "word", "set": "headline", "key": "reconcile", "lower": True,
    }
    assert reversed_.detail_phrase["params"]["detail"] == proved
    _assert_raw(reversed_.detail_phrase)


@pytest.mark.parametrize(
    ("template", "params", "said"),
    [
        ("history.import.created", {"count": 1}, "1 new transaction"),
        ("history.import.created", {"count": 2}, "2 new transactions"),
        ("history.detail.one_time.transactions", {"imported": 3, "linked": 0}, "3 transactions."),
        ("history.detail.one_time.transactions", {"imported": 1, "linked": 1}, "1 transaction, 1 transfer linked."),
        ("history.detail.one_time.changes_from", {"changes": 4, "linked": 2, "where": {"type": "name", "value": "budget.csv"}}, "4 changes, 2 transfers linked from budget.csv."),
    ],
)
def test_the_english_renderer_reads_the_plurals_as_icu_does(template, params, said):
    assert describing.english(describing.phrase(template, **params)) == said


def test_history_over_http_carries_the_structure_beside_unchanged_english(client):
    from tests.conftest import HEADERS
    from tests.test_api import _household_with_accounts

    world = _household_with_accounts(client)
    house = world["household"]["id"]
    for account, amount in ((world["checking"], -4_250), (world["card"], -1_999)):
        made = client.post(
            f"/api/households/{house}/transactions",
            json={"account_id": account["id"], "date": "2026-01-15", "amount": amount, "payee_name": "Carrefour"},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text

    batches = client.get(f"/api/households/{house}/batches?include_single_edits=true").json()
    assert batches and all(one["detail_phrase"] for one in batches)
    for one in batches:
        _assert_raw(one["detail_phrase"])
    newest = batches[0]
    assert newest["detail"] == "Added transaction -€19.99 · Carrefour · in Visa"

    detail = client.get(f"/api/households/{house}/batches/{newest['id']}").json()
    assert len(detail["line_phrases"]) == len(detail["lines"]) == detail["change_count"]
    assert detail["changed_rows"][0]["summary_phrase"]["key"] == "history.line.added"
    snapshot = {one["column"]: one for one in detail["changed_rows"][0]["snapshot"]}
    assert snapshot["amount"]["now_value"] == {"type": "money", "amount": -1_999, "currency": "EUR"}

    txn_id = next(
        one["row_id"] for one in detail["changed_rows"] if one["table_key"] == "transactions"
    )
    history = client.get(f"/api/households/{house}/changes?table=transactions&row_id={txn_id}").json()
    assert history[0]["summary"] == "Added transaction -€19.99 · Carrefour · in Visa"
    assert history[0]["summary_phrase"]["key"] == "history.line.added"


def _client_section(name: str) -> dict[str, str]:
    """`historyWords.ts`: a `msg({ id, message })` table, as id -> English."""
    import json
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / "client/src/lib/historyWords.ts").read_text(
        encoding="utf-8"
    )
    body = text.split(f"export const {name}")[1].split("\n};")[0]
    found = {}
    for block in re.finditer(r'id: ("[^"]+"),\s*message:\s*((?:"(?:[^"\\]|\\.)*"\s*)+)', body):
        found[json.loads(block.group(1))] = "".join(
            json.loads(part) for part in re.findall(r'"(?:[^"\\]|\\.)*"', block.group(2))
        )
    return found


@pytest.mark.repo_wide
def test_the_client_has_every_sentence_and_word_in_the_servers_english():
    """What every translation of History starts from is the server's English."""
    assert _client_section("HISTORY_SENTENCES") == describing.SENTENCES
    assert _client_section("HISTORY_COUNTS") == {
        f"history.count.{table}": f"{{count, plural, one {{# {one}}} other {{# {many}}}}}"
        for table, (one, many) in describing.NOUNS.items()
    }
    words = {
        (f"history.{word_set}.{key}" if key else f"history.{word_set}"): english
        for word_set, keyed in describing.WORDS.items()
        for key, english in keyed.items()
    }
    assert _client_section("HISTORY_WORDS") == words
    assert _client_section("HISTORY_ENUMS") == {
        f"history.enum.{column}.{value}": english
        for column, values in describing.ENUM_WORDS.items()
        for value, english in values.items()
    }
