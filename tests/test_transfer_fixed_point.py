"""Linking strong pairs until nothing new is strong (#88).

A `named` link makes its two accounts a lane, and a lane is evidence: a pair
between them that was only a suggestion becomes strong the moment the first
is linked. "Link all" and the link at import each asked once, linked what
they found and stopped, so a second press of "Link all" -- or a second pass
over the same import -- found more. These build exactly that chain, on two
lanes at once, with a pair on a third pair of accounts that must stay a
question.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.models import BatchKind, LinkSource, Transaction
from app.services import transfers
from tests import test_transfer_matching as matching
from tests.test_transfer_matching import DAY, _import, _row

#: Current, Saver and a card in EUR, the fixture's GBP account, and their
#: identifiers -- the matcher's own fixture, shared rather than copied.
ledger = matching.ledger


def _chain(session, owner, household, ledger):
    """Two lanes, each with one pair a name makes strong and one pair only the
    lane can; and one pair on no lane. Returns them by name."""
    later = DAY + timedelta(days=10)
    rows = {
        # Current -> Saver: the out-leg names the Saver's number.
        "named_out": _row(session, owner, household, ledger["current"], -10000, "TO A/C 33334444"),
        "named_in": _row(session, owner, household, ledger["saver"], 10000, "INCOMING"),
        "lane_out": _row(session, owner, household, ledger["current"], -20000, "THREE", later),
        "lane_in": _row(session, owner, household, ledger["saver"], 20000, "FOUR", later),
        # Current -> Card: a card payment naming the card.
        "card_out": _row(session, owner, household, ledger["current"], -30000, "CARDBRAND PAYMENT"),
        "card_in": _row(session, owner, household, ledger["card"], 30000, "THANK YOU"),
        "card_lane_out": _row(session, owner, household, ledger["current"], -40000, "FIVE", later),
        "card_lane_in": _row(session, owner, household, ledger["card"], 40000, "SIX", later),
        # Saver -> Card: never linked before, so amount and date alone.
        "loose_out": _row(session, owner, household, ledger["saver"], -50000, "SEVEN", later),
        "loose_in": _row(session, owner, household, ledger["card"], 50000, "EIGHT", later),
    }
    return rows


def _partner(session, txn: Transaction) -> str | None:
    session.refresh(txn)
    return txn.transfer_transaction_id


def test_the_chain_is_real_one_pass_leaves_the_lane_pairs(session, owner, household, ledger):
    """The reproduction: what the screen shows as strong before, and after
    linking only that, what a second press would find."""
    rows = _chain(session, owner, household, ledger)
    first = transfers.find(session, household.id)
    assert {(p.out_leg.id, p.in_leg.id) for p in first.strong} == {
        (rows["named_out"].id, rows["named_in"].id),
        (rows["card_out"].id, rows["card_in"].id),
    }
    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id):
        assert transfers.link_strong(session, first) == 2
    second = transfers.find(session, household.id)
    assert {(p.out_leg.id, p.in_leg.id) for p in second.strong} == {
        (rows["lane_out"].id, rows["lane_in"].id),
        (rows["card_lane_out"].id, rows["card_lane_in"].id),
    }


def test_link_all_links_what_a_second_press_would(session, owner, household, ledger):
    rows = _chain(session, owner, household, ledger)
    shown = transfers.find(session, household.id).strong
    with batch(session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id):
        linked = transfers.link_on_evidence(
            session, household.id, [(p.out_leg, p.in_leg) for p in shown]
        )
    assert linked == 4

    for out, into in (("named", "named"), ("lane", "lane"), ("card", "card"), ("card_lane", "card_lane")):
        assert _partner(session, rows[f"{out}_out"]) == rows[f"{into}_in"].id
    assert (rows["named_out"].link_source, rows["card_out"].link_source) == (LinkSource.named,) * 2
    assert (rows["lane_out"].link_source, rows["card_lane_in"].link_source) == (LinkSource.history,) * 2

    # The pair on no lane is still a person's question, and nothing is left strong.
    assert _partner(session, rows["loose_out"]) is None
    after = transfers.find(session, household.id)
    assert after.strong == []
    assert {(p.out_leg.id, p.in_leg.id) for p in after.suggested} == {
        (rows["loose_out"].id, rows["loose_in"].id)
    }


def test_one_undo_takes_back_the_whole_link_all(session, owner, household, ledger):
    rows = _chain(session, owner, household, ledger)
    shown = transfers.find(session, household.id).strong
    with batch(
        session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id
    ) as made:
        transfers.link_on_evidence(session, household.id, [(p.out_leg, p.in_leg) for p in shown])
    undo_batch(session, made.id, actor_id=owner.id)
    session.expire_all()
    assert all(session.get(Transaction, t.id).transfer_transaction_id is None for t in rows.values())
    assert len(transfers.find(session, household.id).strong) == 2, "back to what was shown"


def test_the_import_links_until_nothing_new_is_strong(session, owner, household, ledger):
    """The same chain, arriving as the current account's statement."""
    later = DAY + timedelta(days=10)
    _row(session, owner, household, ledger["saver"], 10000, "INCOMING")
    _row(session, owner, household, ledger["saver"], 20000, "FOUR", later)
    _row(session, owner, household, ledger["card"], 30000, "THANK YOU")
    _row(session, owner, household, ledger["card"], 40000, "SIX", later)
    raw = (
        "Date,Description,Amount\n"
        f"{DAY.isoformat()},TO A/C 33334444,-100.00\n"
        f"{later.isoformat()},THREE,-200.00\n"
        f"{DAY.isoformat()},CARDBRAND PAYMENT,-300.00\n"
        f"{later.isoformat()},FIVE,-400.00\n"
        f"{later.isoformat()},COFFEE,-3.00\n"
    ).encode()
    _, result = _import(session, owner, household, ledger["current"], raw)
    assert result["transfers_linked"] == 4

    legs = session.execute(
        select(Transaction.amount, Transaction.transfer_account_id).where(
            Transaction.account_id == ledger["current"].id
        )
    ).all()
    assert sorted(legs, key=lambda r: r.amount) == sorted(
        [
            (-10000, ledger["saver"].id),
            (-20000, ledger["saver"].id),
            (-30000, ledger["card"].id),
            (-40000, ledger["card"].id),
            (-300, None),
        ],
        key=lambda r: r[0],
    )
