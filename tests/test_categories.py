"""Categories, and deciding one for you.

The CRUD half is ordinary. The half worth testing hard is `decide`: it runs on
every imported row, so a wrong answer is not one correction but three hundred,
and it is the kind of wrong that looks plausible on screen.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.audit.batch import batch
from app.errors import Conflict, NotFound, ValidationError
from app.models import BatchKind, Categorisation
from app.services import categories as category_service
from app.services import payees as payee_service
from app.services import transactions as txn_service

JAN = date(2026, 1, 10)
FEB = date(2026, 2, 10)
MAR = date(2026, 3, 10)


@pytest.fixture()
def write(session, owner, household):
    def _open(kind=BatchKind.manual):
        return batch(session, kind=kind, actor_id=owner.id, household_id=household.id)

    return _open


@pytest.fixture()
def tree(session, household, write):
    """The starter tree, and a lookup by name."""
    with write(BatchKind.admin):
        category_service.seed_defaults(session, household.id)
    return {
        one.name: one for one in category_service.list_categories(session, household.id)
    }


def _payee(session, household, name: str, write):
    """Payees are audited, so making one needs a batch open like anything else."""
    with write():
        return payee_service.get_or_create(session, household.id, name)


def _spend(session, account, *, payee, category=None, when=JAN, amount=-1_000):
    return txn_service.create(
        session, account=account, date=when, amount=amount, payee=payee, category=category
    )


# --------------------------------------------------------------------------- #
# The list
# --------------------------------------------------------------------------- #


def test_the_starter_tree_is_classification_only(session, household, tree):
    """No budget machinery. That is a standing decision, not an oversight."""
    names = set(tree)
    assert "Groceries" in names
    assert "Subscriptions" in names

    for banned in ("Credit Card Payments", "FX Difference", "Internal"):
        assert banned not in names, f"{banned} is budget machinery and must not be seeded"

    # The bucket holding money that has not been assigned anywhere yet is the
    # one piece of budget machinery that cannot be named in advance -- every
    # budget calls it something different. It always sits in an inflow group,
    # and there is no inflow group here: income is a category like any other,
    # because nothing assigns it onward.
    assert not any(name.startswith("Inflow") for name in names)


def test_a_category_reads_with_its_group(session, household, tree):
    assert tree["Subscriptions"].full_name == "Quality of Life: Subscriptions"


def test_two_categories_cannot_share_a_name(session, household, tree, write):
    groups = category_service.list_groups(session, household.id)
    with write(BatchKind.admin), pytest.raises(Conflict, match="already a category"):
        category_service.create_category(
            session, household.id, group_id=groups[0].id, name="Groceries"
        )


def test_a_category_in_use_cannot_be_deleted(session, household, accounts, tree, write):
    """Deleting it would blank the category on every row that had it."""
    with write():
        _spend(session, accounts["checking"], payee=None, category=tree["Groceries"])

    with write(BatchKind.admin), pytest.raises(Conflict, match="Archive it instead"):
        category_service.delete_category(session, tree["Groceries"])


def test_an_unused_category_can_be_deleted(session, household, tree, write):
    with write(BatchKind.admin):
        category_service.delete_category(session, tree["Hobbies"])
    assert "Hobbies" not in {
        one.name for one in category_service.list_categories(session, household.id)
    }


def test_deleting_a_category_clears_payee_defaults_and_undo_puts_them_back(
    session, owner, household, tree, write
):
    """#97. The default is ON DELETE SET NULL with nothing on Category pointing
    back, so the database cleared it behind the audit hook: undo restored the
    category and left the payee "fixed" to nothing, quietly categorising
    nothing from then on."""
    from app.audit.undo import undo_batch
    from app.models import Payee

    hobbies = tree["Hobbies"]
    kept = tree["Groceries"]
    shop = _payee(session, household, "Hobby Shop", write)
    grocer = _payee(session, household, "Mercadona", write)
    with write(BatchKind.admin):
        category_service.set_rule(session, shop, mode="fixed", category_id=hobbies.id)
        category_service.set_rule(session, grocer, mode="fixed", category_id=kept.id)

    with write(BatchKind.admin) as removal:
        category_service.delete_category(session, hobbies)
    session.commit()
    session.expire_all()
    assert session.get(Payee, shop.id).default_category_id is None
    assert session.get(Payee, grocer.id).default_category_id == kept.id, "only its own payees"

    undo_batch(session, removal.id, actor_id=owner.id)
    session.commit()
    session.expire_all()
    restored = session.get(Payee, shop.id)
    assert restored.default_category_id == hobbies.id
    assert category_service.decide(session, restored).name == "Hobbies"


def test_archiving_hides_it_without_touching_what_it_categorised(
    session, household, accounts, tree, write
):
    with write():
        txn = _spend(session, accounts["checking"], payee=None, category=tree["Groceries"])

    with write(BatchKind.admin):
        category_service.update_category(session, tree["Groceries"], archived=True)

    listed = {one.name for one in category_service.list_categories(session, household.id)}
    assert "Groceries" not in listed, "an archived category is still offered"
    assert txn.category_id == tree["Groceries"].id, "archiving rewrote a transaction"


def test_a_group_holding_categories_cannot_be_deleted(session, household, tree, write):
    group = next(
        g for g in category_service.list_groups(session, household.id) if g.name == "Everyday"
    )
    with write(BatchKind.admin), pytest.raises(Conflict, match="can only be deleted when there are no categories under it"):
        category_service.delete_group(session, group)


def test_a_category_from_another_household_is_not_reachable(session, household, tree, write):
    with pytest.raises(NotFound):
        category_service.get_for_household(session, tree["Groceries"].id, "some-other-household")


# --------------------------------------------------------------------------- #
# Deciding one for you: history
# --------------------------------------------------------------------------- #


def test_a_payee_with_no_history_decides_nothing(session, household, tree, write):
    payee = _payee(session, household, "Brand New Shop", write)
    assert category_service.decide(session, payee) is None


def test_the_most_recent_category_is_used_when_there_is_only_one(
    session, household, accounts, tree, write
):
    payee = _payee(session, household, "Mercadona", write)
    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Groceries"])

    assert category_service.decide(session, payee) is tree["Groceries"]


def test_the_most_common_of_the_last_three_wins(session, household, accounts, tree, write):
    """One stray correction must not redirect a payee."""
    payee = _payee(session, household, "Repsol", write)
    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Transport"], when=JAN)
        _spend(session, accounts["checking"], payee=payee, category=tree["Transport"], when=FEB)
        # A one-off: somebody bought a sandwich at the petrol station.
        _spend(session, accounts["checking"], payee=payee, category=tree["Eating Out"], when=MAR)

    assert category_service.decide(session, payee) is tree["Transport"]


def test_a_deliberate_change_takes_effect_on_the_second_one(
    session, household, accounts, tree, write
):
    """Most-common must not mean never-changes.

    Two in a row of something new outvotes one of the old inside a window of
    three, which is what makes correcting a payee twice actually work.
    """
    payee = _payee(session, household, "Amazon", write)
    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Household"], when=JAN)
        _spend(session, accounts["checking"], payee=payee, category=tree["Hobbies"], when=FEB)

    # One each: the tie goes to the most recent, so the change lands.
    assert category_service.decide(session, payee) is tree["Hobbies"]

    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Hobbies"], when=MAR)
    assert category_service.decide(session, payee) is tree["Hobbies"]


def test_only_the_last_three_count(session, household, accounts, tree, write):
    """Otherwise a payee's ancient history outvotes what you do now."""
    payee = _payee(session, household, "Carrefour", write)
    with write():
        for day in (1, 2, 3, 4, 5):
            _spend(
                session, accounts["checking"], payee=payee,
                category=tree["Household"], when=date(2026, 1, day),
            )
        for day in (10, 11, 12):
            _spend(
                session, accounts["checking"], payee=payee,
                category=tree["Groceries"], when=date(2026, 1, day),
            )

    assert category_service.decide(session, payee) is tree["Groceries"]


def test_uncategorised_rows_do_not_use_up_the_window(
    session, household, accounts, tree, write
):
    """A payee whose last three rows are blank still has a history."""
    payee = _payee(session, household, "El Bar", write)
    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Eating Out"], when=JAN)
        for day in (1, 2, 3):
            _spend(
                session, accounts["checking"], payee=payee,
                category=None, when=date(2026, 3, day),
            )

    assert category_service.decide(session, payee) is tree["Eating Out"]


def test_an_archived_category_is_not_suggested(session, household, accounts, tree, write):
    payee = _payee(session, household, "Blockbuster", write)
    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Subscriptions"])
    with write(BatchKind.admin):
        category_service.update_category(session, tree["Subscriptions"], archived=True)

    assert category_service.decide(session, payee) is None


# --------------------------------------------------------------------------- #
# Deciding one for you: the other two modes
# --------------------------------------------------------------------------- #


def test_a_fixed_category_beats_the_history(session, household, accounts, tree, write):
    payee = _payee(session, household, "Netflix", write)
    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Eating Out"])
    with write(BatchKind.admin):
        category_service.set_rule(
            session, payee, mode=Categorisation.fixed, category_id=tree["Subscriptions"].id
        )

    assert category_service.decide(session, payee) is tree["Subscriptions"]


def test_fixed_needs_a_category(session, household, tree, write):
    payee = _payee(session, household, "Netflix", write)
    with write(BatchKind.admin), pytest.raises(ValidationError, match="choose the category"):
        category_service.set_rule(session, payee, mode=Categorisation.fixed)


def test_switching_away_and_back_keeps_your_choice(session, household, tree, write):
    """Otherwise trying history for a week costs you the default you set."""
    payee = _payee(session, household, "Netflix", write)
    with write(BatchKind.admin):
        category_service.set_rule(
            session, payee, mode=Categorisation.fixed, category_id=tree["Subscriptions"].id
        )
        category_service.set_rule(session, payee, mode=Categorisation.history)

    assert payee.default_category_id == tree["Subscriptions"].id

    with write(BatchKind.admin):
        category_service.set_rule(session, payee, mode=Categorisation.fixed)
    assert category_service.decide(session, payee) is tree["Subscriptions"]


def test_none_means_none_however_strong_the_history(
    session, household, accounts, tree, write
):
    payee = _payee(session, household, "Cash Machine", write)
    with write():
        for day in (1, 2, 3):
            _spend(
                session, accounts["checking"], payee=payee,
                category=tree["Household"], when=date(2026, 1, day),
            )
    with write(BatchKind.admin):
        category_service.set_rule(session, payee, mode=Categorisation.none)

    assert category_service.decide(session, payee) is None


# --------------------------------------------------------------------------- #
# Where it actually gets used
# --------------------------------------------------------------------------- #


def test_a_new_transaction_is_categorised_from_the_payee(
    session, household, accounts, tree, write
):
    """The whole point: you type a payee and the category is already right."""
    payee = _payee(session, household, "Mercadona", write)
    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Groceries"])

    with write():
        second = txn_service.create(
            session, account=accounts["checking"], date=FEB, amount=-2_000, payee=payee
        )

    assert second.category_id == tree["Groceries"].id


def test_an_explicit_category_beats_the_payee(session, household, accounts, tree, write):
    payee = _payee(session, household, "Mercadona", write)
    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Groceries"])

    with write():
        second = txn_service.create(
            session, account=accounts["checking"], date=FEB, amount=-2_000,
            payee=payee, category=tree["Gifts"],
        )
    assert second.category_id == tree["Gifts"].id


def test_asking_for_uncategorised_is_not_the_same_as_saying_nothing(
    session, household, accounts, tree, write
):
    """`None` means leave it blank; omitting it means consult the payee."""
    payee = _payee(session, household, "Mercadona", write)
    with write():
        _spend(session, accounts["checking"], payee=payee, category=tree["Groceries"])

    with write():
        blank = txn_service.create(
            session, account=accounts["checking"], date=FEB, amount=-2_000,
            payee=payee, category=None,
        )
    assert blank.category_id is None


def test_a_category_from_another_household_is_refused(
    session, owner, household, accounts, tree, write
):
    from app.models import Category, Household

    # Not `write`: that batch is filed under `household`, and the audit hook
    # refuses to write another household's rows under it (#77).
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        other = Household(name="Theirs", base_currency="EUR")
        session.add(other)
        session.flush()
        stray_group = category_service.create_group(session, other.id, "Theirs")
        stray = Category(household_id=other.id, group_id=stray_group.id, name="Elsewhere")
        session.add(stray)
        session.flush()

    with write(), pytest.raises(ValidationError, match="different household"):
        txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-100, category=stray
        )
