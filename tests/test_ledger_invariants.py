"""Invariants over randomised ledgers (#107).

*Build Learnings*: write the invariant suite first. Every other test here
checks one act against the figure it should leave; these generate a ledger
from a seed -- rows, transfers across and within currencies, edits, splits,
deletes -- and then hold four things that must be true of **any** ledger:

1. every account's balance is the sum of its rows, by every route that reports
   one;
2. every transfer pair points at each other, and within one currency the two
   legs net to zero;
3. undoing a run of acts, newest first, gives back every row exactly as it
   was before them -- every column, in both households;
4. no total crosses currencies: each currency's income-and-expense report is
   exactly the flow in that currency's accounts, and a currency nobody holds
   reports nothing.

A seeded generator rather than Hypothesis: there is no Hypothesis in the
requirements, and a failing seed is reproduced by its number alone. Two
households, two currencies in each and two accounts in one currency, so a
check that only works with one of something has nowhere to hide.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.models import Account, AccountType, BatchKind, Transaction
from app.services import accounts as account_service
from app.services import insights, reporting
from app.services import transactions as txn_service

SEEDS = range(12)
START = date(2026, 1, 1)
#: The columns undo must give back exactly. All of them: a column left out
#: here is a column whose restore nobody checked.
COLUMNS = [column.key for column in Transaction.__table__.columns]


# --------------------------------------------------------------------------- #
# The world and the generator
# --------------------------------------------------------------------------- #


@pytest.fixture()
def world(session, owner, member, household, other_household, accounts):
    """Ours: two EUR accounts and a GBP one (the shared fixture). Theirs: USD and GBP."""
    with batch(session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id):
        theirs = [
            Account(household_id=other_household.id, name="Their dollars",
                    type=AccountType.checking, currency="USD"),
            Account(household_id=other_household.id, name="Their pounds",
                    type=AccountType.savings, currency="GBP"),
        ]
        session.add_all(theirs)
    session.commit()
    return {
        household.id: (owner, list(accounts.values())),
        other_household.id: (member, theirs),
    }


def _rows(session, household_id: str) -> list[Transaction]:
    return list(
        session.execute(
            select(Transaction).where(Transaction.household_id == household_id)
        ).scalars()
    )


def _plain(session, household_id: str) -> list[Transaction]:
    """Rows an edit, a split or a delete may touch: not transfer legs, not split parts."""
    return [
        row for row in _rows(session, household_id)
        if row.transfer_transaction_id is None and row.transfer_account_id is None
        and row.split_id is None
    ]


def _act(session, rng: random.Random, household_id: str, actor, accounts) -> str:
    """One random act, in one batch. Returns the batch id."""
    choice = rng.choices(
        ["row", "transfer", "edit", "split", "delete"], weights=[5, 2, 2, 1, 1]
    )[0]
    plain = _plain(session, household_id)
    if choice in ("edit", "split", "delete") and not plain:
        choice = "row"
    when = START + timedelta(days=rng.randrange(0, 300))
    with batch(session, kind=BatchKind.manual, actor_id=actor.id, household_id=household_id) as opened:
        if choice == "row":
            account = rng.choice(accounts)
            amount = rng.choice([-1, 1]) * rng.randrange(1, 500_000)
            txn_service.create(session, account=account, date=when, amount=amount)
        elif choice == "transfer":
            source, destination = rng.sample(accounts, 2)
            amount = rng.randrange(1, 300_000)
            to_amount = (
                None if source.currency == destination.currency
                else rng.randrange(1, 300_000)
            )
            txn_service.create_transfer(
                session, source=source, destination=destination, date=when,
                amount=amount, to_amount=to_amount,
            )
        elif choice == "edit":
            row = rng.choice(plain)
            txn_service.update(
                session, row, amount=rng.choice([-1, 1]) * rng.randrange(1, 500_000), date=when
            )
        elif choice == "split":
            row = rng.choice(plain)
            first = rng.randrange(-abs(row.amount) - 5, abs(row.amount) + 5) or 1
            parts = [
                txn_service.SplitPart(amount=first),
                txn_service.SplitPart(amount=row.amount - first),
            ]
            txn_service.split(session, row, parts)
        else:
            txn_service.delete(session, rng.choice(plain))
    session.commit()
    return opened.id


def _grow(session, world, seed: int, acts: int) -> list[str]:
    rng = random.Random(seed)
    done = []
    for _ in range(acts):
        household_id = rng.choice(list(world))
        actor, accounts = world[household_id]
        done.append(_act(session, rng, household_id, actor, accounts))
    return done


def test_the_generator_does_every_kind_of_act(session, world, monkeypatch):
    """A generator that never splits proves nothing about splits."""
    seen: list[str] = []
    real = random.Random.choices

    def counting(self, population, weights=None, **kw):
        picked = real(self, population, weights=weights, **kw)
        if population and population[0] == "row":
            seen.append(picked[0])
        return picked

    monkeypatch.setattr(random.Random, "choices", counting)
    _grow(session, world, 3, acts=60)
    assert {"row", "transfer", "edit", "split", "delete"} <= set(seen)
    crossed = [
        row for household_id in world for row in _rows(session, household_id)
        if row.transfer_transaction_id and row.transfer_fx_rate
    ]
    assert crossed, "no transfer crossed currencies"


# --------------------------------------------------------------------------- #
# The invariants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seed", SEEDS)
def test_every_balance_is_the_sum_of_its_rows(session, world, seed):
    _grow(session, world, seed, acts=40)
    for household_id, (_actor, accounts) in world.items():
        summed: dict[str, int] = {account.id: 0 for account in accounts}
        for row in _rows(session, household_id):
            summed[row.account_id] += row.amount
        reported = account_service.balances_for_household(session, household_id)
        for account in accounts:
            one = account_service.balances(session, account.id)
            assert reported[account.id]["balance"] == summed[account.id], account.name
            assert one["balance"] == summed[account.id], account.name
            assert (
                reported[account.id]["cleared"] + reported[account.id]["uncleared"]
                == summed[account.id]
            )
        for account, total in insights.balances(session, household_id):
            assert total == summed[account.id], account.name


@pytest.mark.parametrize("seed", SEEDS)
def test_transfer_pairs_point_at_each_other_and_net_to_zero_within_a_currency(
    session, world, seed
):
    _grow(session, world, seed, acts=40)
    currency = {
        account.id: account.currency for _a, accounts in world.values() for account in accounts
    }
    legs = 0
    for household_id in world:
        by_id = {row.id: row for row in _rows(session, household_id)}
        for row in by_id.values():
            if row.transfer_transaction_id is None:
                continue
            legs += 1
            other = by_id[row.transfer_transaction_id]
            assert other.transfer_transaction_id == row.id
            assert other.household_id == row.household_id
            assert row.transfer_account_id == other.account_id
            assert (row.amount > 0) != (other.amount > 0), "one leg out, one in"
            if currency[row.account_id] == currency[other.account_id]:
                assert row.amount + other.amount == 0
    if seed == 0:
        assert legs > 0, "the generator made no transfer, so this checked nothing"


@pytest.mark.parametrize("seed", SEEDS)
def test_undo_restores_every_row_exactly(session, world, seed):
    _grow(session, world, seed, acts=15)

    def image() -> dict[str, dict]:
        return {
            row.id: {name: getattr(row, name) for name in COLUMNS}
            for household_id in world
            for row in _rows(session, household_id)
        }

    before = image()
    acts = _grow(session, world, seed + 1000, acts=15)
    assert image() != before, "the acts changed nothing, so undo has nothing to prove"

    owner = next(iter(world.values()))[0]
    for batch_id in reversed(acts):
        undo_batch(session, batch_id, actor_id=owner.id)
        session.commit()
        session.expire_all()

    assert image() == before


@pytest.mark.parametrize("seed", SEEDS)
def test_no_total_crosses_currencies(session, world, seed):
    _grow(session, world, seed, acts=40)
    for household_id, (_actor, accounts) in world.items():
        currency = {account.id: account.currency for account in accounts}
        flow: dict[str, int] = {}
        for row in _rows(session, household_id):
            if row.transfer_transaction_id is None and row.transfer_account_id is None:
                code = currency[row.account_id]
                flow[code] = flow.get(code, 0) + row.amount
        for code in {account.currency for account in accounts}:
            report = reporting.income_expense(session, household_id, currency=code)
            assert report["net_total_minor"] == flow.get(code, 0), code
        # A currency nobody in this household holds has no flow at all.
        nobody = next(c for c in ("JPY", "CHF", "SEK") if c not in currency.values())
        empty = reporting.income_expense(session, household_id, currency=nobody)
        assert empty["net_total_minor"] == 0
        assert set(reporting.currencies_in_use(session, household_id)) <= set(currency.values())


def test_the_households_are_kept_apart(session, world):
    _grow(session, world, 7, acts=40)
    ids = {household_id: {a.id for a in accounts} for household_id, (_x, accounts) in world.items()}
    for household_id in world:
        for row in _rows(session, household_id):
            assert row.account_id in ids[household_id]
            if row.transfer_account_id:
                assert row.transfer_account_id in ids[household_id]
