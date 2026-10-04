"""Dividing one transaction into the parts it was really made of.

The rule that matters is that a split adds up. One that does not is not a
split -- it is an edit and a new transaction sharing a name, and it moves the
account's balance while looking like a reclassification.

The other thing under test is the shape: the parts *replace* the original
rather than hanging off it. The previous build kept a parent row that was
itself a transaction, so every sum in the system had to remember to exclude it.
Here nothing does, and these tests are what say so.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import Conflict, ValidationError
from app.models import BatchKind, ClearedState, Transaction
from app.services import accounts as account_service
from app.services import categories as category_service
from app.services import payees as payee_service
from app.services import transactions as txn_service

JAN = date(2026, 1, 15)


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


def _balance(session, account) -> int:
    return account_service.balances(session, account.id)["balance"]


@pytest.fixture()
def shopping(session, accounts, household, write):
    """A single supermarket trip: €100, half food and half a kettle."""
    with write():
        payee = payee_service.get_or_create(session, household.id, "Carrefour")
        txn = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-10_000,
            payee=payee, memo="weekly shop",
        )
    return txn


def _parts(*amounts, categories=()):
    cats = list(categories) + [None] * (len(amounts) - len(categories))
    return [txn_service.SplitPart(amount=a, category=c) for a, c in zip(amounts, cats, strict=True)]


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def test_a_split_that_adds_up_replaces_the_original(session, accounts, shopping, tree, write):
    before = _balance(session, accounts["checking"])

    with write(BatchKind.split):
        made = txn_service.split(
            session, shopping,
            _parts(-6_000, -4_000, categories=[tree["Groceries"], tree["Household"]]),
        )

    assert len(made) == 2
    assert {one.amount for one in made} == {-6_000, -4_000}
    assert _balance(session, accounts["checking"]) == before, "a split moved the balance"
    assert session.get(Transaction, shopping.id) is None, "the original is still there"


def test_a_split_that_does_not_add_up_is_refused(session, accounts, shopping, write):
    before = _balance(session, accounts["checking"])

    with write(BatchKind.split), pytest.raises(Conflict, match="has to add up"):
        txn_service.split(session, shopping, _parts(-6_000, -3_000))

    assert _balance(session, accounts["checking"]) == before
    assert session.get(Transaction, shopping.id) is not None, "a refused split removed the row"


def test_the_parts_belong_to_each_other(session, shopping, write):
    with write(BatchKind.split):
        made = txn_service.split(session, shopping, _parts(-6_000, -4_000))

    groups = {one.split_id for one in made}
    assert len(groups) == 1 and None not in groups
    assert len(txn_service.parts_of(session, made[0].split_id)) == 2


def test_the_parts_carry_what_the_original_carried(session, shopping, write):
    with write(BatchKind.split):
        made = txn_service.split(session, shopping, _parts(-6_000, -4_000))

    for one in made:
        assert one.date == JAN
        assert one.payee_id == shopping.payee_id
        assert one.account_id == shopping.account_id
        assert one.memo == "weekly shop", "the memo was dropped on the floor"


def test_a_part_can_say_something_of_its_own(session, shopping, tree, write):
    with write(BatchKind.split):
        made = txn_service.split(session, shopping, [
            txn_service.SplitPart(amount=-6_000, category=tree["Groceries"]),
            txn_service.SplitPart(amount=-4_000, category=tree["Household"], memo="kettle"),
        ])

    by_amount = {one.amount: one for one in made}
    assert by_amount[-4_000].memo == "kettle"
    assert by_amount[-6_000].memo == "weekly shop", "the other part lost the original's memo"
    assert by_amount[-6_000].category_id == tree["Groceries"].id
    assert by_amount[-4_000].category_id == tree["Household"].id


@pytest.mark.parametrize("count", [1, 6])
def test_a_split_is_between_two_and_five_parts(session, shopping, write, count):
    with write(BatchKind.split), pytest.raises(ValidationError, match="between 2 and 5"):
        txn_service.split(session, shopping, _parts(*([-10_000 // count] * count)))


def test_a_zero_part_is_refused(session, shopping, write):
    """It adds up and means nothing -- a row for no money is not a part."""
    with write(BatchKind.split), pytest.raises(ValidationError, match="cannot be zero"):
        txn_service.split(session, shopping, _parts(-10_000, 0))


def test_a_locked_row_cannot_be_split(session, accounts, shopping, write):
    with write():
        txn_service.update(session, shopping, cleared=ClearedState.reconciled)

    with write(BatchKind.split), pytest.raises(Conflict, match="locked"):
        txn_service.split(session, shopping, _parts(-6_000, -4_000))


def test_a_transfer_leg_cannot_be_split(session, accounts, household, write):
    """One movement of money recorded twice; splitting one side desynchronises."""
    with write():
        out_leg, _ = txn_service.create_transfer(
            session, source=accounts["checking"], destination=accounts["card"],
            date=JAN, amount=5_000,
        )

    with write(BatchKind.split), pytest.raises(ValidationError, match="leg of a transfer"):
        txn_service.split(session, out_leg, _parts(-3_000, -2_000))


def test_five_parts_is_allowed(session, shopping, write):
    with write(BatchKind.split):
        made = txn_service.split(session, shopping, _parts(-2_000, -2_000, -2_000, -2_000, -2_000))
    assert len(made) == 5


def test_an_income_row_splits_the_same_way(session, accounts, household, write, tree):
    """Signs are not special-cased, so a positive amount must work too."""
    with write():
        pay = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=210_000
        )
    before = _balance(session, accounts["checking"])

    with write(BatchKind.split):
        made = txn_service.split(session, pay, _parts(200_000, 10_000))

    assert sorted(one.amount for one in made) == [10_000, 200_000]
    assert _balance(session, accounts["checking"]) == before


# --------------------------------------------------------------------------- #
# Getting out of it
# --------------------------------------------------------------------------- #


def test_undoing_a_split_puts_the_original_back(session, accounts, shopping, household, owner):
    """One act, one undo -- the parts go and the row they replaced returns."""
    original_id, original_amount = shopping.id, shopping.amount
    before = _balance(session, accounts["checking"])

    with batch(
        session, kind=BatchKind.split, actor_id=owner.id, household_id=household.id
    ) as open_batch:
        made = txn_service.split(session, shopping, _parts(-6_000, -4_000))
    part_ids = [one.id for one in made]
    batch_id = open_batch.id

    with batch(session, kind=BatchKind.undo, actor_id=owner.id, household_id=household.id):
        undo_batch(session, batch_id, actor_id=owner.id)

    session.expire_all()
    restored = session.get(Transaction, original_id)
    assert restored is not None, "the original did not come back"
    assert restored.amount == original_amount
    for part_id in part_ids:
        assert session.get(Transaction, part_id) is None, "a part survived the undo"
    assert _balance(session, accounts["checking"]) == before


def test_a_part_can_be_edited_like_any_other_row(session, shopping, tree, write):
    """They are transactions, not children of one -- nothing treats them apart."""
    with write(BatchKind.split):
        made = txn_service.split(session, shopping, _parts(-6_000, -4_000))

    with write():
        txn_service.update(session, made[0], category=tree["Travel"], memo="actually a trip")

    assert made[0].category_id == tree["Travel"].id
    assert made[0].memo == "actually a trip"


def test_a_part_can_itself_be_split(session, accounts, shopping, write):
    before = _balance(session, accounts["checking"])
    with write(BatchKind.split):
        made = txn_service.split(session, shopping, _parts(-6_000, -4_000))
    with write(BatchKind.split):
        again = txn_service.split(session, made[0], _parts(-3_500, -2_500))

    assert len(again) == 2
    assert again[0].split_id != made[1].split_id, "the second split reused the first's group"
    assert _balance(session, accounts["checking"]) == before


# --------------------------------------------------------------------------- #
# What History says about it
# --------------------------------------------------------------------------- #


def test_history_names_the_transaction_that_was_split(session, accounts, shopping, tree, write):
    """A split is the one act whose subject is gone by the time you read about it.

    `services/transactions.split` deletes the original and inserts the parts, so
    the generic sentence for a multi-row batch called it "3 transactions
    changed" -- true, and naming none of them. Somebody scrolling History for
    the purchase they divided last week has nothing to recognise.

    The delete's before-image is the original, and it is complete, so the
    sentence can name it.
    """
    from app.services import describing

    with write(BatchKind.split) as made:
        txn_service.split(
            session,
            shopping,
            [
                txn_service.SplitPart(amount=-6_000, category=tree["Groceries"]),
                txn_service.SplitPart(amount=-4_000, category=tree["Household"]),
            ],
        )

    words = describing.describe(session, made)

    # The headline was the raw enum value, because BatchKind.split was the one
    # kind missing from HEADLINES.
    assert words.headline == "Split"

    # The original, named: who it was paid to, how much, and when.
    assert "Carrefour" in words.detail
    assert "100.00" in words.detail
    assert str(JAN) in words.detail

    # And what it became, including the categories that make a split worth doing.
    assert "60.00" in words.detail
    assert "40.00" in words.detail
    assert "Groceries" in words.detail
    assert "Household" in words.detail


def test_the_split_sentence_survives_a_shape_it_does_not_recognise(session, household):
    """It reads the batch's shape, so it has to say something sane without one.

    A sentence that invents detail is worse than one that is brief -- this is an
    audit log.
    """
    from app.models import Batch
    from app.services import describing

    names = describing._Names(session, household.id)
    empty = Batch(kind=BatchKind.split, actor_id="x", household_id=household.id)

    assert describing._detail(empty, [], names) == "Nothing was changed."


def test_a_split_part_says_where_it_came_from(session, accounts, shopping, tree, write):
    """The part's own history opened with "Added transaction", which is the
    one thing it was not.

    A split part is an ordinary insert in the log. It arrived because something
    else was divided, and that something else was deleted in the same batch --
    so it is not in a history filtered to the part's own id, and the sentence
    has to carry it.
    """
    from sqlalchemy import select

    from app.models import Change
    from app.services import describing

    with write(BatchKind.split):
        parts = txn_service.split(
            session,
            shopping,
            [
                txn_service.SplitPart(amount=-6_000, category=tree["Groceries"]),
                txn_service.SplitPart(amount=-4_000, category=tree["Household"]),
            ],
        )

    part = parts[0]
    changes = list(
        session.execute(
            select(Change).where(
                Change.table_name == "transactions", Change.row_id == part.id
            )
        ).scalars()
    )
    assert changes, "the part's insert is not in the log"

    said = describing.describe_changes(session, part.household_id, changes)
    sentence = " ".join(said.values())

    assert "by splitting" in sentence
    # And it names the original, which no longer exists to be looked up.
    assert "Carrefour" in sentence
    assert "100.00" in sentence


def test_an_ordinary_insert_does_not_claim_to_be_a_split(session, accounts, household, write):
    """`_split_origins` only looks at batches that are actually splits."""
    from sqlalchemy import select

    from app.models import Change
    from app.services import describing

    with write():
        payee = payee_service.get_or_create(session, household.id, "Corner Shop")
        plain = txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-500, payee=payee
        )

    changes = list(
        session.execute(
            select(Change).where(
                Change.table_name == "transactions", Change.row_id == plain.id
            )
        ).scalars()
    )
    said = " ".join(describing.describe_changes(session, household.id, changes).values())
    assert "by splitting" not in said
