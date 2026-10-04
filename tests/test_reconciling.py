"""Proving an account against a statement.

The one rule worth having is that a reconciliation which does not balance is
refused. A note saying one happened, when it did not, is worse than no note at
all -- the next person trusts it and stops looking further back, which is the
exact opposite of what reconciling is for.

So most of these tests are about the arithmetic being enforced, and the rest are
about what it costs when it is: the rows really do lock, and undo really does
put them back.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import Conflict, NotFound, ValidationError
from app.models import BatchKind, ClearedState, Reconciliation, Transaction
from app.services import payees as payee_service
from app.services import reconciling
from app.services import transactions as txn_service

JAN = date(2026, 1, 15)
FEB = date(2026, 2, 20)
MAR = date(2026, 3, 10)


@pytest.fixture()
def write(session, owner, household):
    def _open(kind=BatchKind.manual):
        return batch(session, kind=kind, actor_id=owner.id, household_id=household.id)

    return _open


@pytest.fixture()
def statement(session, accounts, write):
    """Three rows on the current account: -1000, -2500, +8000. Net 4500."""
    checking = accounts["checking"]
    made = []
    with write():
        for when, amount in ((JAN, -1_000), (FEB, -2_500), (FEB, 8_000)):
            made.append(
                txn_service.create(session, account=checking, date=when, amount=amount)
            )
    return {"account": checking, "rows": made}


def _ids(rows) -> list[str]:
    return [row.id for row in rows]


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def test_a_reconciliation_that_balances_locks_exactly_what_was_ticked(session, statement, write):
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled):
        record = reconciling.reconcile(
            session,
            account,
            statement_date=FEB,
            statement_balance=4_500,
            transaction_ids=_ids(rows),
        )

    assert record.statement_balance == 4_500
    assert record.statement_date == FEB
    for row in rows:
        assert row.cleared is ClearedState.reconciled


def test_a_reconciliation_that_does_not_balance_is_refused(session, statement, write):
    """And nothing is locked, so the next attempt starts from where you were."""
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled), pytest.raises(Conflict, match="does not balance"):
        reconciling.reconcile(
            session,
            account,
            statement_date=FEB,
            statement_balance=9_999,
            transaction_ids=_ids(rows),
        )

    for row in rows:
        assert row.cleared is not ClearedState.reconciled, "a failed attempt locked a row"
    assert session.query(Reconciliation).count() == 0


def test_the_refusal_says_how_far_out_it_is(session, statement, write):
    """"It doesn't balance" sends you looking. "€12.34 out" tells you what for."""
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled), pytest.raises(Conflict) as raised:
        reconciling.reconcile(
            session,
            account,
            statement_date=FEB,
            statement_balance=5_734,  # 1234 more than the rows come to
            transaction_ids=_ids(rows),
        )

    assert "12.34" in str(raised.value), str(raised.value)


def test_ticking_only_some_rows_balances_against_their_own_total(session, statement, write):
    """A statement that closes mid-month covers some rows and not others."""
    account, rows = statement["account"], statement["rows"]
    january_only = [rows[0]]

    with write(BatchKind.reconciled):
        reconciling.reconcile(
            session,
            account,
            statement_date=JAN,
            statement_balance=-1_000,
            transaction_ids=_ids(january_only),
        )

    assert rows[0].cleared is ClearedState.reconciled
    assert rows[1].cleared is not ClearedState.reconciled
    assert rows[2].cleared is not ClearedState.reconciled


def test_the_second_statement_starts_from_the_first(session, statement, write):
    """What is already locked is not proved again -- it is the floor.

    This is the whole payoff: each statement is checked against the rows it
    covers, not against the account's entire history.
    """
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled):
        reconciling.reconcile(
            session, account, statement_date=JAN,
            statement_balance=-1_000, transaction_ids=[rows[0].id],
        )

    assert reconciling.locked_balance(session, account.id) == -1_000

    # February's statement closes at 4500: the -1000 already proved, plus the
    # -2500 and +8000 on this one.
    with write(BatchKind.reconciled):
        reconciling.reconcile(
            session, account, statement_date=FEB,
            statement_balance=4_500, transaction_ids=[rows[1].id, rows[2].id],
        )

    assert reconciling.locked_balance(session, account.id) == 4_500


def test_a_row_the_statement_cannot_contain_is_refused(session, statement, write):
    """Dated after the statement closes, so it is not on it."""
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled), pytest.raises(ValidationError, match="after the statement"):
        reconciling.reconcile(
            session, account, statement_date=JAN,
            statement_balance=4_500, transaction_ids=_ids(rows),
        )


def test_an_already_locked_row_cannot_be_counted_twice(session, statement, write):
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled):
        reconciling.reconcile(
            session, account, statement_date=JAN,
            statement_balance=-1_000, transaction_ids=[rows[0].id],
        )

    with write(BatchKind.reconciled), pytest.raises(Conflict, match="already locked"):
        reconciling.reconcile(
            session, account, statement_date=FEB,
            statement_balance=3_500, transaction_ids=_ids(rows),
        )


def test_a_row_from_another_account_is_refused(session, statement, accounts, write):
    """Otherwise one account could be balanced with another's money."""
    account = statement["account"]
    with write():
        elsewhere = txn_service.create(
            session, account=accounts["card"], date=FEB, amount=-500
        )

    with write(BatchKind.reconciled), pytest.raises(NotFound, match="not on this account"):
        reconciling.reconcile(
            session, account, statement_date=FEB,
            statement_balance=-500, transaction_ids=[elsewhere.id],
        )


def test_reconciling_nothing_is_not_a_reconciliation(session, statement, write):
    account = statement["account"]
    with write(BatchKind.reconciled), pytest.raises(ValidationError, match="tick the rows"):
        reconciling.reconcile(
            session, account, statement_date=FEB, statement_balance=0, transaction_ids=[]
        )


def test_the_same_row_twice_does_not_count_twice(session, statement, write):
    """Sent twice it would sum twice, and a wrong statement would balance."""
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled), pytest.raises(ValidationError, match="ticked twice"):
        reconciling.reconcile(
            session, account, statement_date=JAN,
            statement_balance=-2_000, transaction_ids=[rows[0].id, rows[0].id],
        )


# --------------------------------------------------------------------------- #
# The worksheet
# --------------------------------------------------------------------------- #


def test_the_worksheet_offers_only_what_is_still_unproved(session, statement, write):
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled):
        reconciling.reconcile(
            session, account, statement_date=JAN,
            statement_balance=-1_000, transaction_ids=[rows[0].id],
        )

    sheet = reconciling.worksheet(session, account)
    offered = {one.id for one in sheet.candidates}
    assert rows[0].id not in offered, "a locked row was offered for ticking again"
    assert offered == {rows[1].id, rows[2].id}
    assert sheet.locked_balance == -1_000


def test_the_worksheet_stops_at_the_statement_date(session, statement, write):
    account, rows = statement["account"], statement["rows"]
    with write():
        txn_service.create(session, account=account, date=MAR, amount=-999)

    sheet = reconciling.worksheet(session, account, until=FEB)
    assert all(one.date <= FEB for one in sheet.candidates)
    assert len(sheet.candidates) == len(rows)


def test_the_worksheet_remembers_the_last_statement(session, statement, write):
    account, rows = statement["account"], statement["rows"]
    assert reconciling.worksheet(session, account).last_statement_date is None

    with write(BatchKind.reconciled):
        reconciling.reconcile(
            session, account, statement_date=JAN,
            statement_balance=-1_000, transaction_ids=[rows[0].id],
        )

    sheet = reconciling.worksheet(session, account)
    assert sheet.last_statement_date == JAN
    assert sheet.last_statement_balance == -1_000


def test_the_worksheet_carries_the_payee_so_a_row_can_be_recognised(session, accounts, write):
    """Ticking rows against a paper statement means reading them, not their ids."""
    with write():
        payee = payee_service.get_or_create(session, accounts["checking"].household_id, "Mercadona")
        txn_service.create(
            session, account=accounts["checking"], date=JAN, amount=-1_000,
            payee=payee, memo="weekly",
        )

    sheet = reconciling.worksheet(session, accounts["checking"])
    assert sheet.candidates[0].payee == "Mercadona"
    assert sheet.candidates[0].memo == "weekly"


# --------------------------------------------------------------------------- #
# What it costs, and getting out of it
# --------------------------------------------------------------------------- #


def test_a_reconciled_row_is_locked_afterwards(session, statement, write):
    """The state does what it has always done -- now it is earned rather than set."""
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled):
        reconciling.reconcile(
            session, account, statement_date=FEB,
            statement_balance=4_500, transaction_ids=_ids(rows),
        )

    with write(), pytest.raises(Conflict, match="locked"):
        txn_service.update(session, rows[0], amount=-9_999)


def test_undoing_the_batch_unlocks_every_row_it_locked(session, statement, owner, household):
    """The way out. A reconciliation is one act, so it is one undo."""
    account, rows = statement["account"], statement["rows"]

    with batch(
        session, kind=BatchKind.reconciled, actor_id=owner.id, household_id=household.id
    ) as open_batch:
        reconciling.reconcile(
            session, account, statement_date=FEB, statement_balance=4_500,
            transaction_ids=_ids(rows), batch_id=open_batch.id,
        )
    batch_id = open_batch.id

    assert all(row.cleared is ClearedState.reconciled for row in rows)

    with batch(session, kind=BatchKind.undo, actor_id=owner.id, household_id=household.id):
        undo_batch(session, batch_id, actor_id=owner.id)

    session.expire_all()
    for row in session.query(Transaction).filter(Transaction.id.in_(_ids(rows))):
        assert row.cleared is not ClearedState.reconciled, "undo left a row locked"
    assert reconciling.locked_balance(session, account.id) == 0


def test_the_record_points_at_the_batch_that_did_it(session, statement, owner, household):
    account, rows = statement["account"], statement["rows"]

    with batch(
        session, kind=BatchKind.reconciled, actor_id=owner.id, household_id=household.id
    ) as open_batch:
        record = reconciling.reconcile(
            session, account, statement_date=FEB, statement_balance=4_500,
            transaction_ids=_ids(rows), batch_id=open_batch.id,
        )

    assert record.batch_id == open_batch.id


def test_history_reads_newest_first(session, statement, write):
    account, rows = statement["account"], statement["rows"]

    with write(BatchKind.reconciled):
        reconciling.reconcile(
            session, account, statement_date=JAN,
            statement_balance=-1_000, transaction_ids=[rows[0].id],
        )
    with write(BatchKind.reconciled):
        reconciling.reconcile(
            session, account, statement_date=FEB,
            statement_balance=4_500, transaction_ids=[rows[1].id, rows[2].id],
        )

    dates = [one.statement_date for one in reconciling.history(session, account.id)]
    assert dates == [FEB, JAN]
