"""A key writing a row's memo.

Categorising was the only edit a key could make to a row already in the
ledger, so a run that read forty-six airline tickets could put the flight and
the booking code on each receipt's note but not on the row a person reads in
the register. This is the door for that, with the same floor as `categorise`:
one batch, one undo, a reconciled row skipped and named, another household's
rows not found.
"""

from __future__ import annotations

from sqlalchemy import select

from app.audit.batch import batch
from app.models import Batch, BatchKind, ClearedState, Transaction, User
from app.services import transfers as transfer_service
from tests import test_agent_links as _links
from tests.conftest import HEADERS
from tests.test_agent_links import V1, _call, _db, _get, _row

#: The links fixture, not a copy of it: two households, three accounts in two
#: currencies, a read key and a write key -- two of everything, as the rule asks.
world = _links.world


def _memos(world, assignments, *, key="write"):
    return _call(world, "PATCH", "/transactions/memo", key=key,
                 json={"assignments": assignments})


def test_each_row_gets_its_own_memo_in_one_act(world):
    """Two rows in two currencies, two different memos, one row left alone."""
    flight = _row(world, "santander", 3, -44248, "Iberia Lae Sa")
    train = _row(world, "monzo", 4, -2790, "Virgin Trains")
    untouched = _row(world, "santander", 5, -1200, "Bakery")

    answer = _memos(world, [
        {"transaction_id": flight, "memo": "IB0739 MAD→AMS 26 May · booking QX7RT"},
        {"transaction_id": train, "memo": "London → Reading, client visit"},
        {"transaction_id": "not-a-row", "memo": "anything"},
    ])
    assert answer.status_code == 200, answer.text

    did = answer.json()
    assert did["changed"] == 2
    assert did["not_found"] == ["not-a-row"], "a silent drop is how a caller believes a lie"
    assert _get(world, flight).memo == "IB0739 MAD→AMS 26 May · booking QX7RT"
    assert _get(world, train).memo == "London → Reading, client visit"
    assert _get(world, untouched).memo is None


def test_the_whole_run_is_one_batch_and_one_undo_puts_every_memo_back(world):
    from app.audit.guard import AuditedSession
    from app.audit.undo import undo_batch

    first = _row(world, "santander", 3, -10000, "Iberia")
    second = _row(world, "monzo", 4, -5000, "British Airways")
    _call(world, "PATCH", "/transactions/memo", json={"assignments": [
        {"transaction_id": first, "memo": "LONDON"},
    ]})

    did = _memos(world, [
        {"transaction_id": first, "memo": "LONDON · IB3651 LHR→MAD"},
        {"transaction_id": second, "memo": "BA466 LHR→MAD"},
    ]).json()

    with _db(world) as own:
        made = own.get(Batch, did["batch_id"])
        assert made is not None, "it has to be in History to be undoable"
        assert made.agent_key_id is not None, "and it has to say a program did it"

    # Through the service rather than the route: undo is a person's button,
    # and what is asserted here is that both memos went in as one act.
    engine = world["client"].app_module.db_engine
    with AuditedSession(bind=engine, expire_on_commit=False) as own:
        actor = own.execute(select(User.id)).scalars().first()
        undo_batch(own, did["batch_id"], actor_id=actor)
        own.commit()

    assert _get(world, first).memo == "LONDON", "back to what it said before, not empty"
    assert _get(world, second).memo is None


def test_null_and_blank_both_empty_it_and_a_repeat_writes_nothing(world):
    one = _row(world, "santander", 3, -1000, "Shop")
    two = _row(world, "monzo", 4, -2000, "Other shop")
    _memos(world, [
        {"transaction_id": one, "memo": "first"},
        {"transaction_id": two, "memo": "second"},
    ])

    did = _memos(world, [
        {"transaction_id": one, "memo": None},
        {"transaction_id": two, "memo": "   "},
    ]).json()
    assert did["changed"] == 2
    assert _get(world, one).memo is None
    assert _get(world, two).memo is None, "blank is the same request as null"

    again = _memos(world, [
        {"transaction_id": one, "memo": ""},
        {"transaction_id": two, "memo": None},
    ]).json()
    assert again["changed"] == 0
    assert again["unchanged"] == 2, "a no-op write is a History entry about nothing"


def test_a_reconciled_row_keeps_its_memo_and_is_named_locked(world):
    """#215: the register refuses to edit a reconciled row, and so does a key."""
    open_row = _row(world, "santander", 3, -1000, "Shop")
    closed = _row(world, "monzo", 4, -2000, "Bar")
    _memos(world, [{"transaction_id": closed, "memo": "as the bank said"}])

    with _db(world) as own:
        with batch(own, kind=BatchKind.reconciled, actor_id=world["user_id"],
                   household_id=world["house"]["id"]):
            own.get(Transaction, closed).cleared = ClearedState.reconciled
        own.commit()

    did = _memos(world, [
        {"transaction_id": open_row, "memo": "written"},
        {"transaction_id": closed, "memo": "not written"},
    ]).json()

    assert did["changed"] == 1
    assert did["locked"] == [closed]
    assert _get(world, open_row).memo == "written"
    assert _get(world, closed).memo == "as the bank said"
    assert _get(world, closed).cleared is ClearedState.reconciled


def test_a_transfer_leg_takes_a_memo_and_keeps_no_category(world):
    """A leg has no category (#124), but a memo says what it was, and a leg
    has one as much as any row does."""
    out_leg = _row(world, "santander", 3, -50000, "To savings")
    in_leg = _row(world, "revolut", 3, 50000, "From current")
    with _db(world) as own:
        with batch(own, kind=BatchKind.manual, actor_id=world["user_id"],
                   household_id=world["house"]["id"]):
            transfer_service.link(own, own.get(Transaction, out_leg), own.get(Transaction, in_leg))
        own.commit()

    did = _memos(world, [
        {"transaction_id": out_leg, "memo": "October rent float"},
        {"transaction_id": in_leg, "memo": "October rent float"},
    ]).json()

    assert did["changed"] == 2
    assert _get(world, out_leg).memo == "October rent float"
    assert _get(world, in_leg).memo == "October rent float"
    assert _get(world, out_leg).category_id is None


def test_a_read_key_is_refused_and_nothing_moves(world):
    row = _row(world, "santander", 3, -1000, "Shop")
    refused = _memos(world, [{"transaction_id": row, "memo": "no"}], key="read")
    assert refused.status_code == 403, refused.text
    assert _get(world, row).memo is None


def test_another_households_rows_are_not_found_and_its_path_is_a_404(world):
    mine = _row(world, "santander", 3, -1000, "Shop")
    theirs = _row(world, "away", 4, -900, "Not yours", where="away")

    did = _memos(world, [
        {"transaction_id": mine, "memo": "mine"},
        {"transaction_id": theirs, "memo": "reached across"},
    ]).json()
    assert did["changed"] == 1
    assert did["not_found"] == [theirs], "404, not 403: they should not learn it is real"
    assert _get(world, theirs).memo is None

    elsewhere = world["client"].patch(
        f"{V1}/households/{world['away']['id']}/transactions/memo",
        json={"assignments": [{"transaction_id": theirs, "memo": "x"}]},
        headers={"authorization": f"Bearer {world['tokens']['write']}", **HEADERS},
    )
    assert elsewhere.status_code == 404, elsewhere.text
    assert _get(world, theirs).memo is None


def test_a_memo_longer_than_the_register_allows_is_refused_whole(world):
    row = _row(world, "santander", 3, -1000, "Shop")
    other = _row(world, "monzo", 4, -1000, "Shop")
    answer = _memos(world, [
        {"transaction_id": row, "memo": "x" * 501},
        {"transaction_id": other, "memo": "fine"},
    ])
    assert answer.status_code == 422, answer.text
    assert _get(world, row).memo is None
    assert _get(world, other).memo is None, "checked before anything is written"
