"""Households, membership, accounts and balances.

Membership is binary, and the answer for a non-member is the same as for a
household that does not exist -- so nobody learns an id is real by asking.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.errors import Conflict, Forbidden, NotFound, ValidationError
from app.models import Account, AccountType, BatchKind, ClearedState, Transaction
from app.services import accounts as account_service
from app.services import households as household_service

JAN = date(2026, 1, 15)


def _spend(session, account, amount: int, *, cleared=ClearedState.uncleared) -> Transaction:
    txn = Transaction(
        household_id=account.household_id,
        account_id=account.id,
        date=JAN,
        amount=-abs(amount),
        cleared=cleared,
    )
    session.add(txn)
    return txn


# --------------------------------------------------------------------------- #
# Membership
# --------------------------------------------------------------------------- #


def test_the_creator_is_the_first_member(session, owner):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        house = household_service.create_household(session, name="Ours", creator=owner)

    assert [m.user_id for m in household_service.members(session, house.id)] == [owner.id]
    assert household_service.get_for(session, house.id, owner).id == house.id


def test_a_non_member_is_told_it_does_not_exist(session, owner, member, other_household):
    """404, not 403. A 403 confirms the id is real."""
    with pytest.raises(NotFound, match="no such household"):
        household_service.get_for(session, other_household.id, owner)

    with pytest.raises(NotFound, match="no such household"):
        household_service.get_for(session, "a" * 32, owner)


def test_you_only_see_the_households_you_are_in(session, owner, member, household, other_household):
    assert [h.id for h in household_service.list_for(session, owner)] == [household.id]
    assert {h.id for h in household_service.list_for(session, member)} == {
        household.id,
        other_household.id,
    }


def test_only_the_owner_changes_who_is_in(session, owner, member, household):
    """Forbidden, not ValidationError.

    The service raised 422 and `deps.require_owner` raised 403 for the same
    rule. Over HTTP the dependency fires first, so the service's answer was
    unreachable -- and this test pinned a status the API could never return.
    """
    with batch(session, kind=BatchKind.admin, actor_id=member.id), pytest.raises(
        Forbidden, match="only the owner"
    ):
        household_service.add_member(
            session, household_id=household.id, user_id=owner.id, added_by=member
        )


def test_adding_someone_twice_is_refused(session, owner, member, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id), pytest.raises(
        Conflict, match="already in this household"
    ):
        household_service.add_member(
            session, household_id=household.id, user_id=member.id, added_by=owner
        )


def test_the_last_member_cannot_be_removed(session, owner, member, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        household_service.remove_member(
            session, household_id=household.id, user_id=member.id, removed_by=owner
        )
    with batch(session, kind=BatchKind.admin, actor_id=owner.id), pytest.raises(
        Conflict, match="at least one member"
    ):
        household_service.remove_member(
            session, household_id=household.id, user_id=owner.id, removed_by=owner
        )


def test_removing_someone_takes_their_access_with_it(session, owner, member, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        household_service.remove_member(
            session, household_id=household.id, user_id=member.id, removed_by=owner
        )
    with pytest.raises(NotFound):
        household_service.get_for(session, household.id, member)


def test_membership_changes_are_audited(session, owner, member, household):
    from app.models import Change

    rows = session.execute(
        select(Change).where(Change.table_name == "household_members")
    ).scalars().all()
    assert len(rows) == 2, "both members were recorded when the fixture built the household"
    assert {r.after["user_id"] for r in rows} == {owner.id, member.id}


def test_a_member_cannot_undo_a_membership_change(session, owner, member, household):
    """Undo replays the original act, so it needs the original act's authority.

    `remove_member` is owner-only. Undo reached the same rows through a route
    that asked only for membership, so any member could reverse the batch that
    admitted somebody -- evicting them, the owner included -- or reverse a
    removal the owner had just made.
    """
    from app.audit.undo import undo_batch
    from app.errors import Forbidden

    third = _bootstrap_third_person(session)
    with batch(
        session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id
    ) as admission:
        household_service.add_member(
            session, household_id=household.id, user_id=third.id, added_by=owner
        )
    session.commit()

    with pytest.raises(Forbidden, match="only the owner"):
        undo_batch(session, admission.id, actor_id=member.id)
    session.rollback()

    # Still in, because the undo was refused rather than half-applied.
    assert household_service.get_for(session, household.id, third).id == household.id

    # And the owner can still do it.
    undo_batch(session, admission.id, actor_id=owner.id)
    session.commit()
    with pytest.raises(NotFound):
        household_service.get_for(session, household.id, third)


def _bootstrap_third_person(session):
    from app.models import Role

    from .conftest import _bootstrap_user

    return _bootstrap_user(session, email="third@gmail.com", name="Third", role=Role.member)


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #


def test_an_account_defaults_to_the_household_currency(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session, household=household, name="Everyday", type=AccountType.checking
        )
    assert account.currency == "EUR"


def test_an_account_can_have_its_own_currency(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session, household=household, name="Pounds", type=AccountType.savings, currency="gbp"
        )
    assert account.currency == "GBP"


def test_two_accounts_cannot_share_a_name(session, owner, household, accounts):
    with batch(
        session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id
    ), pytest.raises(Conflict, match="already an account called"):
        account_service.create_account(
            session, household=household, name="Checking", type=AccountType.cash
        )


def _account_names(session, household_id: str) -> list[str]:
    return sorted(
        session.execute(select(Account.name).where(Account.household_id == household_id)).scalars()
    )


def test_two_accounts_cannot_share_a_name_in_different_case(session, owner, household, accounts):
    """#98. "Transfer : <name>" is unique case-folded, so "Checking" and
    "CHECKING" would be two accounts sharing one transfer payee, and every
    transfer into the second failed on the constraint as a 500."""
    before = _account_names(session, household.id)
    with batch(
        session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id
    ), pytest.raises(Conflict, match="already an account called 'Checking'"):
        account_service.create_account(
            session, household=household, name="  CHECKING ", type=AccountType.cash
        )
    session.rollback()
    assert _account_names(session, household.id) == before


def test_renaming_to_another_accounts_name_in_different_case_is_refused(
    session, owner, household, accounts
):
    with batch(
        session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id
    ), pytest.raises(Conflict, match="already an account called 'Visa'"):
        account_service.rename(session, accounts["checking"], "VISA")
    session.rollback()
    session.expire_all()
    assert session.get(Account, accounts["checking"].id).name == "Checking"


def test_an_account_can_be_renamed_to_its_own_name_in_different_case(
    session, owner, household, accounts, other_household, member
):
    """Only *another* account's name is taken -- and only in this household."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account_service.rename(session, accounts["checking"], "CHECKING")
    with batch(
        session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id
    ):
        account_service.create_account(
            session, household=other_household, name="checking", type=AccountType.cash
        )
    assert "CHECKING" in _account_names(session, household.id)
    assert _account_names(session, other_household.id) == ["checking"]


def test_a_card_and_a_loan_are_liabilities_and_a_current_account_is_not():
    assert AccountType.credit_card.is_liability
    assert AccountType.other_liability.is_liability
    assert not AccountType.checking.is_liability
    assert not AccountType.other_asset.is_liability


def test_only_types_with_designed_behaviour_exist():
    """An enum value with no designed behaviour is a bug with a menu item."""
    assert {t.value for t in AccountType} == {
        "checking", "savings", "cash", "credit_card", "other_asset", "other_liability",
    }


# --------------------------------------------------------------------------- #
# Balances
# --------------------------------------------------------------------------- #


def test_the_three_balances_split_by_cleared_state(session, owner, household, accounts):
    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        _spend(session, checking, 10_000, cleared=ClearedState.reconciled)
        _spend(session, checking, 2_500, cleared=ClearedState.cleared)
        _spend(session, checking, 400, cleared=ClearedState.uncleared)

    figures = account_service.balances(session, checking.id)
    assert figures["balance"] == -12_900
    assert figures["cleared"] == -12_500, "reconciled counts as cleared"
    assert figures["uncleared"] == -400


def test_an_empty_account_reads_zero_not_an_error(session, accounts):
    """The first thing anyone sees is an empty ledger."""
    assert account_service.balances(session, accounts["card"].id) == {
        "balance": 0,
        "cleared": 0,
        "uncleared": 0,
    }


def test_every_balance_in_the_household_comes_back_in_one_query(session, owner, household, accounts, engine):
    from sqlalchemy import event

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        _spend(session, accounts["checking"], 5_000)
        _spend(session, accounts["card"], 1_500, cleared=ClearedState.cleared)

    queries = []

    def _count(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        figures = account_service.balances_for_household(session, household.id)
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert figures[accounts["checking"].id]["balance"] == -5_000
    assert figures[accounts["card"].id]["cleared"] == -1_500
    assert figures[accounts["pounds"].id]["balance"] == 0, "an untouched account still appears"
    assert len(queries) == 1, f"one query, not one per account; got {len(queries)}"


# --------------------------------------------------------------------------- #
# Opening balances
# --------------------------------------------------------------------------- #


def test_an_opening_balance_is_a_real_transaction(session, owner, household):
    """Not a column on the account.

    Being a transaction is what makes it appear in the register, count by the
    same arithmetic as everything else, and be correctable later if the figure
    was wrong -- rather than a number only one code path knows about.
    """
    opened = date(2019, 4, 1)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session,
            household=household,
            name="Old Savings",
            type=AccountType.savings,
            opening_balance=1_250_00,
            opening_date=opened,
        )

    rows = list(
        session.execute(
            select(Transaction).where(Transaction.account_id == account.id)
        ).scalars()
    )
    assert len(rows) == 1
    assert rows[0].amount == 1_250_00
    assert rows[0].date == opened, "dated when the account was opened, not today"
    assert rows[0].cleared is ClearedState.reconciled, "it is what the bank said, by definition"
    assert account_service.balances(session, account.id)["balance"] == 1_250_00


def test_an_opening_balance_can_be_negative(session, owner, household):
    """A card you already owe money on is the common case."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        card = account_service.create_account(
            session,
            household=household,
            name="Old Visa",
            type=AccountType.credit_card,
            opening_balance=-340_00,
            opening_date=date(2025, 6, 30),
        )
    assert account_service.balances(session, card.id)["balance"] == -340_00


def test_no_opening_balance_means_no_transaction(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session, household=household, name="Fresh", type=AccountType.cash
        )
    assert session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account.id)
    ).scalar_one() == 0


def test_an_account_cannot_have_been_opened_in_the_future(session, owner, household):
    from datetime import timedelta

    with batch(
        session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id
    ), pytest.raises(ValidationError, match="in the future"):
        account_service.create_account(
            session,
            household=household,
            name="Tomorrow",
            type=AccountType.cash,
            opening_balance=100,
            opening_date=date.today() + timedelta(days=1),
        )


def test_the_opening_balance_is_undoable_with_the_account(session, owner, household):
    """It arrived as part of creating the account, so it leaves the same way."""
    from app.audit.undo import undo_batch

    with batch(
        session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id
    ) as creation:
        account = account_service.create_account(
            session,
            household=household,
            name="Wrong",
            type=AccountType.checking,
            opening_balance=99_99,
            opening_date=date(2026, 1, 1),
        )
    account_id = account.id

    undo_batch(session, creation.id, actor_id=owner.id)
    session.commit()

    assert session.get(Account, account_id) is None
    assert session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account_id)
    ).scalar_one() == 0


def test_an_account_says_whether_it_is_a_debt(session, owner, household):
    """`is_liability` had no caller anywhere -- not a service, not a router, not
    a screen -- which by this project's own rule made six account types six menu
    items with no behaviour behind them. It is now what the API sends, so the
    client does not keep a second copy of which types are debts."""
    from app.api.routers.households import _account_out

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        card = account_service.create_account(
            session, household=household, name="Visa", type=AccountType.credit_card
        )
        current = account_service.create_account(
            session, household=household, name="Santander", type=AccountType.checking
        )
        loan = account_service.create_account(
            session, household=household, name="Mortgage", type=AccountType.other_liability
        )

    assert _account_out(card).is_liability is True
    assert _account_out(loan).is_liability is True
    assert _account_out(current).is_liability is False


def test_an_account_can_be_moved_in_the_list(session, owner, household):
    """`sort_order` was in the ORDER BY and written by nothing.

    No schema field, no route -- so the list was silently ordered by name and
    the column was decoration on the query that depended on it.
    """
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        first = account_service.create_account(
            session, household=household, name="Aardvark", type=AccountType.checking
        )
        second = account_service.create_account(
            session, household=household, name="Zebra", type=AccountType.savings
        )

    listed = account_service.list_for_household(session, household.id)
    assert [a.name for a in listed][:2] == ["Aardvark", "Zebra"], "by name while both are 0"

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        second.sort_order = -0 + 1
        first.sort_order = 2
        session.flush()

    reordered = account_service.list_for_household(session, household.id)
    assert [a.name for a in reordered][:2] == ["Zebra", "Aardvark"]


def test_a_new_household_starts_with_categories(session, owner):
    """`seed_defaults` existed, was on a route, and nothing that made a household called it.

    Its own docstring names the consequence: "an empty category list makes the
    feature look broken on the first screen somebody opens: every picker is
    empty and there is nothing to learn from". That is exactly what happened --
    the split panel's category select offered one option, "Uncategorised", and
    read as a bug in the split screen rather than as an empty ledger.

    A doc describing behaviour nothing implements is the pattern CLAUDE.md is
    about. This is the test that makes the sentence true.
    """
    from app.audit.batch import batch
    from app.models import BatchKind
    from app.services import categories as category_service
    from app.services import households as household_service

    with batch(session, kind=BatchKind.manual, actor_id=owner.id):
        made = household_service.create_household(session, name="Fresh", creator=owner)

    groups = category_service.list_groups(session, made.id)
    names = {one.name for one in category_service.list_categories(session, made.id)}

    assert groups, "a new household has no categories, so every picker is empty"
    # Not just "some": the tree the service documents as the starting point.
    assert {"Income", "Bills", "Everyday"} <= {one.name for one in groups}
    assert {"Groceries", "Salary", "Transport"} <= names

    # And they belong to the household that was just made, not to the fixture's.
    assert all(one.household_id == made.id for one in groups)
