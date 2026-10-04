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
