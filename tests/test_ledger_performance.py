"""How much work the ledger's hot paths do, asserted as the ledger grows.

Issues #100-#105. Every one of them was a path that did the right thing and
did it once per row: a statement per imported line, a scan of the ledger per
payee, four whole-table reads per History entry. None of them was wrong, so no
test that asserts *what* was written could see them. These assert *how much*
it took -- counted statements and query plans, never wall-clock time, which
this machine and CI both make meaningless -- and each is written as the
difference between a small N and a larger one, so the thing asserted is that
the cost stops growing with N, not that it matches today's figure.

What the paths produce is asserted by their own suites (`test_importing`,
`test_audit_undo`, `test_describing`, `test_transfer_matching`); the
equivalence of the rewritten paths is checked here where the rewrite could
plausibly have changed an answer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, func, select, text

from app.audit.batch import batch
from app.auth import housekeeping
from app.errors import Conflict
from app.models import (
    Base,
    BatchKind,
    BatchStatus,
    Change,
    ChangeOp,
    ClearedState,
    ImportLine,
    ImportOutcome,
    Payee,
    Transaction,
)
from app.services import importing
from app.services import payees as payee_service
from statements import ParsedRow

#: Three shops, so every figure below grows with rows and not with payees.
SHOPS = ("Mercadona", "Bar Marisol", "Renfe")


@contextmanager
def statements(engine) -> Iterator[list[str]]:
    """Every statement sent to the database inside the block."""
    seen: list[str] = []

    def count(conn, cursor, statement, params, context, executemany):  # noqa: ARG001
        seen.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", count)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", count)


def _plan(engine, statement) -> str:
    compiled = statement.compile(engine, compile_kwargs={"literal_binds": True})
    with engine.connect() as conn:
        rows = conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {compiled}").all()
    return "\n".join(str(row[-1]) for row in rows)


# --------------------------------------------------------------------------- #
# #100: the ledger's missing indexes, and statistics for the planner
# --------------------------------------------------------------------------- #


def test_a_payees_category_history_is_read_from_an_index_not_a_scan():
    """`categories.from_history`, run once per payee on every import.

    Without `(payee_id, date)` this is `SCAN transactions` plus a temporary
    B-tree for the sort: 7-11 ms a query at 50k rows, 1.6 s of a 300-line
    import.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    history = (
        select(Transaction.category_id)
        .where(Transaction.payee_id == "p" * 32, Transaction.category_id.is_not(None))
        .order_by(Transaction.date.desc(), Transaction.created_at.desc())
        .limit(5)
    )
    plan = _plan(engine, history)
    assert "ix_transactions_payee_date" in plan, plan
    assert "SCAN transactions" not in plan, plan


def test_the_transfer_lanes_and_the_account_delete_check_use_an_index():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    restrict = select(Transaction.id).where(Transaction.transfer_account_id == "a" * 32)
    plan = _plan(engine, restrict)
    assert "ix_transactions_transfer_account_id" in plan, plan


def test_the_housekeeping_timer_leaves_the_planner_statistics(tmp_path):
    """Nothing ever ran `ANALYZE`, so SQLite chose indexes blind.

    Asserted as the statistics table appearing with a row for the ledger in
    it, from a database that had none: the sweep's own connection has queried
    nothing, which is exactly the case where `PRAGMA optimize` on SQLite before
    3.46 quietly does nothing at all.
    """
    path = tmp_path / "stats.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            text(
                "INSERT INTO transactions (id, household_id, account_id, date, amount, cleared,"
                " created_at, updated_at) VALUES (:id, 'h', 'a', '2026-01-01', -100,"
                " 'uncleared', '2026-01-01', '2026-01-01')"
            ),
            [{"id": f"{n:032d}"} for n in range(50)],
        )

    def analysed() -> set[str]:
        with sqlite3.connect(path) as raw:
            try:
                return {row[0] for row in raw.execute("SELECT tbl FROM sqlite_stat1")}
            except sqlite3.OperationalError:  # no such table: never analysed
                return set()

    assert "transactions" not in analysed()
    housekeeping.sweep(engine)
    assert "transactions" in analysed()
    engine.dispose()


# --------------------------------------------------------------------------- #
# Staging and committing an import
# --------------------------------------------------------------------------- #


def _rows(n: int, *, tag: str, start: date = date(2026, 1, 1)) -> list[ParsedRow]:
    """`n` statement lines over three shops, every one a distinct purchase."""
    return [
        ParsedRow(
            line_no=i + 1,
            raw=f"{tag}-{i}",
            when=start + timedelta(days=i % 28),
            amount=Decimal(-(1000 + i)) / 100,
            payee=SHOPS[i % len(SHOPS)],
            fitid=f"{tag}-{i}",
        )
        for i in range(n)
    ]


def _stage(session, owner, household, account, rows, *, tag):
    with batch(
        session,
        kind=BatchKind.imported,
        actor_id=owner.id,
        household_id=household.id,
        source={"filename": f"{tag}.csv", "sha256": tag, "account_id": account.id},
    ) as staged:
        importing.stage(session, account=account, rows=rows, batch_row=staged)
        staged.status = BatchStatus.preview
    return staged


def _commit(session, owner, household, account, staged):
    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id):
        result = importing.commit(session, batch_row=staged, account=account)
        staged.status = BatchStatus.applied
    return result


def _commit_counted(engine, session, owner, household, account, n, *, tag):
    staged = _stage(session, owner, household, account, _rows(n, tag=tag), tag=tag)
    session.commit()
    with statements(engine) as seen:
        result = _commit(session, owner, household, account, staged)
    assert result["created"] == n
    return len(seen), staged


@pytest.fixture()
def shops(session, owner, household):
    """The three payees exist before any import, as they would in month two."""
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        for name in SHOPS:
            payee_service.get_or_create(session, household.id, name)
    session.commit()


def test_committing_an_import_costs_about_one_statement_per_row_not_seven(
    engine, session, owner, household, accounts, shops
):
    """#101: a 300-row commit issued 2,208 statements.

    A clash query per row that the unique constraint already enforces, a
    flush per row, and every written row selected again by primary key
    because the weak identity map had dropped it. What is left per row is the
    audit log's own insert, one change row each, which is the point of the
    log.
    """
    small, _ = _commit_counted(engine, session, owner, household, accounts["checking"], 10, tag="a")
    large, staged = _commit_counted(
        engine, session, owner, household, accounts["checking"], 70, tag="b"
    )

    per_extra_row = (large - small) / 60
    assert per_extra_row <= 1.25, f"{small} statements for 10 rows, {large} for 70"

    # Fewer statements, and still every row in the log: the change rows are
    # what undo replays, so a cheaper commit that logged less would be worse.
    logged = session.execute(
        select(func.count())
        .select_from(Change)
        .where(
            Change.table_name == "transactions",
            Change.op == ChangeOp.insert,
            Change.batch_id.in_(
                select(Change.batch_id)
                .join(Transaction, Transaction.id == Change.row_id)
                .where(Transaction.import_source == "b.csv")
            ),
        )
    ).scalar_one()
    assert logged == 70
    written = session.execute(
        select(func.count()).where(
            Transaction.account_id == accounts["checking"].id,
            Transaction.import_source == "b.csv",
            Transaction.cleared == ClearedState.cleared,
        )
    ).scalar_one()
    assert written == 70
    lines = session.execute(
        select(ImportLine).where(ImportLine.batch_id == staged.id)
    ).scalars().all()
    assert all(line.transaction_id for line in lines)
    assert {line.outcome for line in lines} == {ImportOutcome.created}


def test_a_line_that_lands_behind_the_importers_back_is_still_a_conflict(
    session, owner, household, accounts, shops, monkeypatch
):
    """The clash query is gone from the import path; the constraint is not.

    The importer reads every id the account holds before it writes, so the
    only way past that read is a row landing in between -- simulated here by
    the read coming back empty. It used to be refused by the clash query as a
    409; it must still be a 409, not an integrity error surfacing as a 500.
    """
    checking = accounts["checking"]
    first = _stage(session, owner, household, checking, _rows(3, tag="x"), tag="x1")
    again = _stage(session, owner, household, checking, _rows(3, tag="x"), tag="x2")
    session.commit()
    _commit(session, owner, household, checking, first)
    session.commit()
    for line in session.execute(
        select(ImportLine).where(ImportLine.batch_id == again.id)
    ).scalars():
        # Staged before the first commit landed, so it still believes these new.
        assert line.outcome is ImportOutcome.created

    monkeypatch.setattr(importing, "_import_ids_in", lambda _session, _account_id: set())
    with pytest.raises(Conflict, match="already in this account"):
        _commit(session, owner, household, checking, again)


def _hand_entered(session, owner, household, account, n, *, start=date(2026, 1, 1)):
    """`n` rows typed in by hand, spread over the same month a statement covers."""
    from app.services import transactions as txn_service

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        for i in range(n):
            txn_service.create(
                session,
                account=account,
                date=start + timedelta(days=(i * 3) % 28),
                amount=-(5000 + i),
                category=None,
            )
    session.commit()


def _ledger_selects(seen: list[str]) -> int:
    return sum(1 for one in seen if one.startswith("SELECT") and "FROM transactions" in one)


def test_staging_matches_twins_in_one_query_however_long_the_file(
    engine, session, owner, household, accounts, shops
):
    """#104: `_find_twin` ran once per staged line, and again on every preview.

    Counted as the queries against the ledger for a file of 10 lines and one
    of 70: equal, because the twins come from one range query per file.
    """
    checking = accounts["checking"]
    _hand_entered(session, owner, household, checking, 20)

    counts = []
    previews = []
    for n, tag in ((10, "s"), (70, "t")):
        with statements(engine) as seen:
            staged = _stage(session, owner, household, checking, _rows(n, tag=tag), tag=tag)
        counts.append(_ledger_selects(seen))
        with statements(engine) as seen:
            importing.reassess(session, batch_row=staged, account=checking)
        previews.append(_ledger_selects(seen))
        session.rollback()

    assert counts[0] == counts[1], counts
    assert previews[0] == previews[1], previews


def _twin_by_query(session, account_id, when, amount, claimed):
    """The per-line query `_find_twin` used to be, kept here as the reference."""
    window = timedelta(days=importing.MATCH_WINDOW_DAYS)
    rows = [
        row
        for row in session.execute(
            select(Transaction).where(
                Transaction.account_id == account_id,
                Transaction.import_id.is_(None),
                Transaction.cleared != ClearedState.reconciled,
                Transaction.amount == amount,
                Transaction.date >= when - window,
                Transaction.date <= when + window,
            )
        ).scalars()
        if row.id not in claimed
    ]
    if not rows:
        return None
    return min(rows, key=lambda t: (abs((t.date - when).days), t.date, t.created_at))


def test_matching_twins_from_one_range_query_picks_what_the_per_line_query_did(
    session, owner, household, accounts
):
    """The rewrite changes how many queries, not which twin.

    A ledger built to make the choice hard -- the same amount on nearby days,
    a locked row, an imported row, a row in the other account, and more lines
    than rows to match -- walked in staging order by both, claims and all.
    """
    from app.services import transactions as txn_service

    checking, card = accounts["checking"], accounts["card"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        for day in range(1, 29):
            for account in (checking, card):
                for amount in (-1200, -1250, -1300 - day % 3):
                    if (day + abs(amount)) % 4 == 0:
                        continue
                    txn_service.create(
                        session,
                        account=account,
                        date=date(2026, 2, day),
                        amount=amount,
                        category=None,
                        cleared=(
                            ClearedState.reconciled if day % 9 == 0 else ClearedState.uncleared
                        ),
                        import_id=f"bank-{day}-{amount}" if day % 7 == 0 else None,
                    )
    session.commit()

    lines = [
        (date(2026, 1, 28) + timedelta(days=i % 40), (-1200, -1250, -1300, -1301, -1302)[i % 5])
        for i in range(120)
    ]
    twins = importing._Twins(session, checking.id, [when for when, _ in lines])
    claimed_new: set[str] = set()
    claimed_old: set[str] = set()
    matched = 0
    for when, amount in sorted(lines):
        new = twins.find(when, amount, claimed=claimed_new)
        old = _twin_by_query(session, checking.id, when, amount, claimed_old)
        assert (new.id if new else None) == (old.id if old else None), (when, amount)
        if new is not None:
            claimed_new.add(new.id)
            claimed_old.add(old.id)
            matched += 1
    # Enough of both outcomes for the comparison to have meant something.
    assert 20 < matched < len(lines)


# --------------------------------------------------------------------------- #
# Undoing an import
# --------------------------------------------------------------------------- #


def test_undoing_an_import_reads_the_same_amount_however_many_rows(
    engine, session, owner, household, accounts, shops
):
    """#103: undoing a 300-row import was 1,811 statements, 1,405 of them reads.

    Every row was read twice -- the dependants check, then the replay --
    because the weak identity map dropped it in between, and each one's
    receipts were a lazy load of their own. Asserted on reads: the writes are
    the audit log recording the undo, one change row per row put back.
    """
    from app.audit.undo import undo_batch

    checking = accounts["checking"]
    reads = []
    for n, tag in ((10, "u"), (70, "v")):
        _, staged = _commit_counted(engine, session, owner, household, checking, n, tag=tag)
        session.commit()
        applied = session.execute(
            select(Change.batch_id)
            .join(Transaction, Transaction.id == Change.row_id)
            .where(Transaction.import_source == f"{tag}.csv")
            .limit(1)
        ).scalar_one()
        with statements(engine) as seen:
            undo_batch(session, applied, actor_id=owner.id)
        session.commit()
        reads.append(sum(1 for one in seen if one.startswith("SELECT")))

        # And it did undo: the rows are gone, the lines' import is reusable.
        left = session.execute(
            select(func.count()).where(Transaction.import_source == f"{tag}.csv")
        ).scalar_one()
        assert left == 0

    assert reads[0] == reads[1], reads


# --------------------------------------------------------------------------- #
# The History list
# --------------------------------------------------------------------------- #


def _history(session, household):
    from app.api.routers.imports import list_batches

    return list_batches(
        household=household,
        session=session,
        include_single_edits=False,
        actor=None,
        agent_key_id=None,
        limit=100,
    )


def test_the_history_list_costs_the_same_however_many_imports_it_shows(
    engine, session, owner, member, household, accounts, shops
):
    """#102: every entry rebuilt four whole-table name lookups and read every
    change row of its batch -- 120 queries for 20 imports, about 5 s for a
    full page. Two imports and six, one of each undone, cost the same.

    #238: and a 300-row bulk edit on the page is said from a count, not from
    300 row images; the manual batches hidden from the list are decided per
    batch, not by counting every change on the instance."""
    from app.audit.undo import undo_batch
    from app.services import transactions as txn_service

    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        typed = [
            txn_service.create(
                session, account=checking, date=date(2026, 5, 1), amount=-(10 + i),
                category=None, flush=False,
            )
            for i in range(300)
        ]
    session.commit()
    with batch(session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id):
        for one in typed:
            one.memo = "bulk"
    session.commit()
    # One lone manual edit, which the list hides.
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        typed[0].memo = "alone"
    session.commit()

    costs = []
    for round_, extra in enumerate((2, 4)):
        for k in range(extra):
            tag = f"h{round_}{k}"
            _commit_counted(engine, session, owner, household, checking, 12, tag=tag)
            session.commit()
        if round_ == 0:
            # An undone import, and the undo, which reads the import it reversed.
            applied = session.execute(
                select(Change.batch_id)
                .join(Transaction, Transaction.id == Change.row_id)
                .where(Transaction.import_source == "h00.csv")
                .limit(1)
            ).scalar_one()
            undo_batch(session, applied, actor_id=member.id)
            session.commit()
        with statements(engine) as seen:
            page = _history(session, household)
        costs.append(len(seen))
        assert not [one for one in seen if 'changes."before"' in one], seen
        # No count of the whole instance's log, which has no WHERE before its GROUP BY.
        assert not [one for one in seen if "FROM changes GROUP BY" in one], seen
    assert costs[0] == costs[1], costs
    assert sum(1 for row in page if row.headline == "Statement import") == 12
    bulk = [row for row in page if row.headline == "Bulk edit"]
    assert [(row.detail, row.change_count) for row in bulk] == [("300 transactions changed.", 300)]
    # The 300 rows typed in one batch show; the lone edit does not.
    edits = [row.detail for row in page if row.headline == "Edit"]
    assert "300 transactions added." in edits
    assert not [one for one in edits if "alone" in one], edits


def test_the_history_list_says_what_describing_each_batch_alone_says(
    engine, session, owner, member, household, accounts, shops
):
    """The shared lookups and the counts change the cost, not a word."""
    from app.audit.undo import undo_batch
    from app.models import Batch
    from app.services import describing

    checking = accounts["checking"]
    for tag in ("d1", "d2"):
        _commit_counted(engine, session, owner, household, checking, 5, tag=tag)
        session.commit()
    applied = session.execute(
        select(Change.batch_id)
        .join(Transaction, Transaction.id == Change.row_id)
        .where(Transaction.import_source == "d2.csv")
        .limit(1)
    ).scalar_one()
    undo_batch(session, applied, actor_id=member.id)
    session.commit()

    page = _history(session, household)
    assert len(page) >= 5
    kinds = set()
    for row in page:
        alone = describing.describe(session, session.get(Batch, row.id))
        assert (row.headline, row.detail, row.actor_name, row.via) == (
            alone.headline,
            alone.detail,
            alone.actor,
            alone.via,
        )
        kinds.add(row.headline)
    assert {"Statement import", "Undo"} <= kinds
    assert any(row.detail.startswith("Reversed: statement import") for row in page)
    assert any(row.actor_name == "Partner" for row in page)


# --------------------------------------------------------------------------- #
# The transfer sweep
# --------------------------------------------------------------------------- #


@pytest.fixture()
def mixed_ledger(session, owner, household, accounts):
    """A ledger where most rows cannot be a transfer leg, and some can.

    Opposite amounts on nearby days and six days apart, in one account and
    across two, in two currencies; descriptors naming another account with and
    without a partner; a household member's name; an opening balance; a split
    part; a pair already linked; ties in date.
    """
    from app.models import Account, AccountType, SystemPayee
    from app.services import identifiers, transfers
    from app.services import transactions as txn_service

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        saver = Account(
            household_id=household.id, name="Saver", type=AccountType.savings, currency="EUR"
        )
        session.add(saver)
        session.flush()
        identifiers.add(session, household_id=household.id, kind="number", value="33334444", account=saver)
        identifiers.add(session, household_id=household.id, kind="holder", value="DOE JANE", account=None)
    checking, card, pounds = accounts["checking"], accounts["card"], accounts["pounds"]
    day = date(2026, 3, 1)
    rows = [
        # Nothing opposite anywhere: the bulk of any ledger.
        *[(checking, -(700 + i), f"SHOP {i}", day + timedelta(days=i % 20)) for i in range(40)],
        *[(card, -(900 + i), None, day + timedelta(days=i % 20)) for i in range(20)],
        # Pairs, some named, some not, some tied.
        (checking, -5000, "TO A/C 33334444", day),
        (saver, 5000, "FROM CURRENT", day + timedelta(days=1)),
        (checking, -2500, "coffee money", day + timedelta(days=2)),
        (card, 2500, "PAYMENT THANK YOU", day + timedelta(days=2)),
        (saver, 2500, "DOE JANE", day + timedelta(days=4)),
        (checking, -1200, None, day + timedelta(days=3)),
        (card, 1200, None, day + timedelta(days=9)),  # six days: too far
        (card, -3300, None, day + timedelta(days=12)),
        (checking, 3300, None, day + timedelta(days=17)),  # five days: just in
        (checking, -1300, None, day + timedelta(days=5)),
        (checking, 1300, None, day + timedelta(days=5)),  # same account
        (card, -8800, None, day + timedelta(days=10)),
        (checking, 8800, None, day + timedelta(days=11)),  # no evidence: suggested
        (checking, -4000, None, day + timedelta(days=6)),
        (pounds, 4000, None, day + timedelta(days=6)),  # another currency
        # Naming another account, with no other leg in the ledger yet.
        (checking, -7777, "TO A/C 33334444 SAVINGS", day + timedelta(days=7)),
        (card, -6666, "DOE JANE REFUND", day + timedelta(days=8)),
    ]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        for i, (account, amount, words, when) in enumerate(rows):
            txn_service.create(
                session, account=account, date=when, amount=amount, payee=None,
                category=None, import_payee_original=words, import_id=f"m{i}",
            )
        opening = payee_service.get_or_create(
            session, household.id, "Opening balance", system=SystemPayee.opening_balance
        )
        txn_service.create(session, account=saver, date=day, amount=-5000, payee=opening, category=None)
        split_part = txn_service.create(session, account=card, date=day, amount=5000, category=None)
        split_part.split_id = "s" * 32
        linked_out = txn_service.create(session, account=checking, date=day, amount=-3000, category=None)
        linked_in = txn_service.create(session, account=saver, date=day, amount=3000, category=None)
        transfers.link(session, linked_out, linked_in)
    session.commit()
    session.expunge_all()
    return saver


def _said(found):
    return (
        [(p.out_leg.id, p.in_leg.id, p.strength, p.why) for p in found.strong],
        [(p.out_leg.id, p.in_leg.id, p.strength, p.why) for p in found.suggested],
        [(t.id, why) for t, why in found.awaiting],
    )


def test_the_transfer_sweep_finds_what_walking_every_row_found(session, household, mixed_ledger):
    """#105 moved the first cut into SQL; the answer, and its order, stay.

    The reference is the matcher run the old way -- over every unlinked row of
    the household, loaded as objects, in the order the old query walked them.
    """
    from sqlalchemy import literal_column

    from app.models import Payee
    from app.services import transfers

    swept = _said(transfers.find(session, household.id))

    everything = list(
        session.execute(
            select(Transaction)
            .outerjoin(Payee, Payee.id == Transaction.payee_id)
            .where(*transfers._unlinked(household.id))
            .order_by(Transaction.date, literal_column("transactions.rowid"))
        ).scalars()
    )
    ctx = transfers._context(session, household.id)
    walked = _said(
        transfers._match(ctx, [t for t in everything if transfers._is_candidate(t)], None)
    )

    assert swept == walked
    strong, suggested, awaiting = swept
    assert strong and suggested and awaiting  # every kind of answer is exercised


def test_the_transfer_sweep_builds_objects_only_for_rows_that_can_be_a_leg(
    session, household, mixed_ledger
):
    """Building an object per unlinked row was 1.26 s of the 2.3 s at 30k rows.

    Counted as objects loaded: the sixty rows with nothing opposite them, and
    naming nothing, are never built.
    """
    from sqlalchemy.orm import Session as OrmSession

    from app.services import transfers

    built: list[str] = []

    def loaded(_session, instance):
        if isinstance(instance, Transaction):
            built.append(instance.id)

    event.listen(OrmSession, "loaded_as_persistent", loaded)
    try:
        found = transfers.find(session, household.id)
    finally:
        event.remove(OrmSession, "loaded_as_persistent", loaded)

    unlinked = session.execute(
        select(func.count()).where(
            Transaction.household_id == household.id,
            Transaction.transfer_transaction_id.is_(None),
        )
    ).scalar_one()
    answered = {t.id for p in found.strong + found.suggested for t in (p.out_leg, p.in_leg)}
    answered |= {t.id for t, _ in found.awaiting}
    assert unlinked > 70
    # Every answer was built, and little else: a leg with a partner in the
    # window that lost it to a closer or stronger pair is built and not
    # answered -- one of the 2500s here.
    assert answered <= set(built)
    assert len(built) <= len(answered) + 2, (len(built), len(answered))


# --------------------------------------------------------------------------- #
# #229: the audit log's own inserts
# --------------------------------------------------------------------------- #


def test_the_change_rows_of_one_flush_go_in_batches_and_keep_their_order(
    engine, session, owner, household, accounts
):
    """Every change row was its own `INSERT ... RETURNING seq`: 500 of them to
    log 500 transactions, because `seq` is made by the database and nothing
    let SQLAlchemy match many returned keys back to many rows.

    Cheaper is only half of it. `seq` is the order undo replays backwards, so
    it still has to rise in the order the hook added the rows, and each object
    has to have been handed back its own row's `seq`, not a neighbour's.
    """
    from sqlalchemy.orm import Session as OrmSession

    from app.audit.undo import undo_batch
    from app.services import transactions as txn_service

    added: list[Change] = []

    def pending(_session, instance):
        if isinstance(instance, Change):
            added.append(instance)

    checking, card = accounts["checking"], accounts["card"]
    event.listen(OrmSession, "transient_to_pending", pending)
    try:
        with statements(engine) as seen, batch(
            session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
        ) as made:
            for i in range(500):
                txn_service.create(
                    session,
                    account=checking if i % 2 else card,
                    date=date(2026, 1, 1) + timedelta(days=i % 28),
                    amount=-(100 + i),
                    category=None,
                    flush=False,
                )
    finally:
        event.remove(OrmSession, "transient_to_pending", pending)
    session.commit()

    inserts = [one for one in seen if one.startswith("INSERT INTO changes")]
    assert len(inserts) < 10, len(inserts)

    assert len(added) == 500
    seqs = [one.seq for one in added]
    assert seqs == sorted(seqs) and len(set(seqs)) == 500
    stored = dict(
        session.execute(
            select(Change.seq, Change.row_id).where(Change.batch_id == made.id)
        ).all()
    )
    assert {one.seq: one.row_id for one in added} == stored

    # And the log it wrote still undoes: every row gone, in one reversed replay.
    undo_batch(session, made.id, actor_id=owner.id)
    session.commit()
    left = session.execute(
        select(func.count()).where(Transaction.household_id == household.id)
    ).scalar_one()
    assert left == 0


# --------------------------------------------------------------------------- #
# #230: undoing a one-time import that created its account
# --------------------------------------------------------------------------- #


def _one_time_import_into_a_new_account(session, owner, household, n: int, *, tag: str):
    """A YNAB register of `n` rows whose one account the import creates."""
    from app.services.one_time_import import engine as one_time
    from app.services.one_time_import import ynab_source
    from tests.test_one_time_import_ynab import _register, _row

    name = f"Pot {tag}"
    rows = [
        _row(name, "2026-01-05", "Starting Balance", "Ready to Assign", "Inflow", inn="£1,000.00"),
        *[
            _row(name, f"2026-02-{1 + i % 27:02d}", SHOPS[i % 3], out=f"£{1 + i}.00")
            for i in range(n - 1)
        ],
    ]
    plan = one_time.Plan(
        currency="GBP",
        date_format="YYYY-MM-DD",
        accounts={name: {"kind": "create", "name": name, "type": "savings"}},
        categories={},
        acknowledge_cleared_reset=True,
    )
    report = one_time.run(
        session,
        household,
        actor_id=owner.id,
        source=ynab_source.from_file(_register(rows), f"{tag}.csv"),
        plan=plan,
        commit=True,
    )
    session.commit()
    return report["batch_id"], name


def test_undoing_an_import_that_created_its_account_reads_the_log_the_same_however_many_rows(
    engine, session, owner, household
):
    """#230: the dependants check asked which batch made each child, one query
    per child -- and when the batch had created the account, every transaction
    it wrote was a child. 10,425 queries to undo a 10.4k-row YNAB import.
    """
    from app.audit.undo import undo_batch
    from app.models import Account

    reads = []
    for n, tag in ((20, "small"), (200, "large")):
        made, name = _one_time_import_into_a_new_account(session, owner, household, n, tag=tag)
        with statements(engine) as seen:
            undo_batch(session, made, actor_id=owner.id)
        session.commit()
        reads.append(sum(1 for one in seen if one.startswith("SELECT") and "FROM changes" in one))

        # It undid: the account the import made is gone, and its rows with it.
        assert session.execute(
            select(func.count()).where(Account.household_id == household.id, Account.name == name)
        ).scalar_one() == 0
        assert session.execute(
            select(func.count()).where(Transaction.household_id == household.id)
        ).scalar_one() == 0

    assert reads[0] == reads[1], reads


def test_a_row_a_later_batch_added_to_the_imported_account_still_refuses_the_undo(
    session, owner, household
):
    """The grouped read decides what the per-row one did: a live later batch
    blocks, and once that batch is itself undone it no longer does."""
    from app.audit.undo import undo_batch
    from app.models import Account, Batch
    from app.services import transactions as txn_service

    made, name = _one_time_import_into_a_new_account(session, owner, household, 30, tag="later")
    pot = session.execute(
        select(Account).where(Account.household_id == household.id, Account.name == name)
    ).scalar_one()
    with batch(
        session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
    ) as later:
        added = txn_service.create(
            session, account=pot, date=date(2026, 3, 1), amount=-500, category=None
        )
    session.commit()

    with pytest.raises(Conflict, match=rf"1 row\(s\).*transactions {added.id}, by batch {later.id}"):
        undo_batch(session, made, actor_id=owner.id)
    session.rollback()

    undo_batch(session, later.id, actor_id=owner.id)
    session.commit()
    undo_batch(session, made, actor_id=owner.id)
    session.commit()
    assert session.get(Batch, made).status is BatchStatus.undone


# --------------------------------------------------------------------------- #
# #234: undo replays a chunk at a time; the batch detail is paged
# --------------------------------------------------------------------------- #


def _by_table_and_op(session, batch_id) -> dict[tuple[str, str], int]:
    return {
        (table, op.value): count
        for table, op, count in session.execute(
            select(Change.table_name, Change.op, func.count())
            .where(Change.batch_id == batch_id)
            .group_by(Change.table_name, Change.op)
        ).all()
    }


def test_an_undo_replayed_across_several_flushes_logs_each_row_once_and_redoes(
    session, owner, household, monkeypatch
):
    """Seven change rows per flush, so the account the import made is deleted
    in a later flush than its transactions. Its loaded collection still listed
    them, and the cascade deleted -- and logged -- every one a second time,
    which the redo then tried to insert twice."""
    import warnings

    from sqlalchemy.exc import SAWarning

    from app.audit import undo as undo_module
    from app.models import Account

    monkeypatch.setattr(undo_module, "_REPLAY_CHUNK", 7)
    made, name = _one_time_import_into_a_new_account(session, owner, household, 30, tag="chunks")
    forward = _by_table_and_op(session, made)
    # The three shops and the starting balance's payee.
    assert forward == {
        ("accounts", "insert"): 1,
        ("payees", "insert"): 4,
        ("transactions", "insert"): 30,
    }

    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        undone = undo_module.undo_batch(session, made, actor_id=owner.id)
        session.commit()
    assert _by_table_and_op(session, undone.id) == {
        (table, "delete"): count for (table, _), count in forward.items()
    }
    assert session.execute(
        select(func.count()).where(Transaction.household_id == household.id)
    ).scalar_one() == 0

    redone = undo_module.undo_batch(session, undone.id, actor_id=owner.id)
    session.commit()
    assert _by_table_and_op(session, redone.id) == forward
    pot = session.execute(
        select(Account).where(Account.household_id == household.id, Account.name == name)
    ).scalar_one()
    assert session.execute(
        select(func.count()).where(Transaction.account_id == pot.id)
    ).scalar_one() == 30


def test_the_undo_replay_holds_a_chunk_of_the_log_not_the_whole_batch(
    session, owner, household, monkeypatch
):
    """Measured against itself, so no machine's figure is baked in: the same
    undo with the whole batch as one chunk -- which is what reading the log
    in one go amounted to -- and with a chunk an eighth of it."""
    import tracemalloc

    from app.audit import undo as undo_module

    peaks = {}
    for chunk, tag in ((800, "whole"), (100, "chunked")):
        monkeypatch.setattr(undo_module, "_REPLAY_CHUNK", chunk)
        made, _ = _one_time_import_into_a_new_account(session, owner, household, 800, tag=tag)
        session.expunge_all()
        tracemalloc.start()
        try:
            undo_module.undo_batch(session, made, actor_id=owner.id)
            session.commit()
            peaks[tag] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        session.expunge_all()
    assert peaks["chunked"] < 0.75 * peaks["whole"], peaks


def _detail(session, household, batch_id, **page):
    from app.api.routers.imports import batch_detail

    return batch_detail(
        batch_id=batch_id, household=household, session=session, **{"limit": 200, "offset": 0, **page}
    )


def test_the_batch_detail_is_a_page_of_rows_and_the_whole_count(
    engine, session, owner, household, accounts
):
    """The confirmation before an undo read, described and returned every row
    of the batch: 2,000 lines and 2,000 full rows to say "2,000 rows go back".
    """
    from app.services import transactions as txn_service

    with batch(
        session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
    ) as made:
        for i in range(2000):
            txn_service.create(
                session,
                account=accounts["checking"] if i % 2 else accounts["card"],
                date=date(2026, 1, 1) + timedelta(days=i % 28),
                amount=-(100 + i),
                category=None,
                flush=False,
            )
    session.commit()

    with statements(engine) as seen:
        first = _detail(session, household, made.id)
    assert first.change_count == 2000
    assert len(first.changed_rows) == 200 and len(first.lines) == 200
    assert first.detail == "2000 transactions added."
    # Only the page's full rows were read: one select of `changes."before"`,
    # limited, and no other.
    full = [one for one in seen if 'changes."before"' in one]
    assert len(full) == 1 and "LIMIT" in full[0], full

    second = _detail(session, household, made.id, offset=200)
    seqs = [row.seq for row in first.changed_rows + second.changed_rows]
    assert seqs == sorted(seqs) and len(set(seqs)) == 400
    last = _detail(session, household, made.id, offset=1900, limit=500)
    assert len(last.changed_rows) == 100 and last.change_count == 2000


# --------------------------------------------------------------------------- #
# #238: indexes and query plans


# #236: the write paths that still flushed once per row
# --------------------------------------------------------------------------- #


def _not_changes(seen: list[str]) -> list[str]:
    return [one for one in seen if not one.startswith("INSERT INTO changes")]


def _distinct(n: int, *, tag: str, start: date = date(2026, 1, 1)) -> list[ParsedRow]:
    """`n` lines, every one a different payee -- the reference-bearing shape
    (`PAGO MOVIL SHOP 12 8123456`) that #58 and #59 exist for."""
    return [
        ParsedRow(
            line_no=i + 1,
            raw=f"{tag}-{i}",
            when=start + timedelta(days=i % 28),
            amount=Decimal(-(1000 + i)) / 100,
            payee=f"PAGO MOVIL SHOP {tag} {i} 81{i:05d}",
            fitid=f"{tag}-{i}",
        )
        for i in range(n)
    ]


def test_a_commit_of_distinct_descriptors_costs_about_one_statement_per_row(
    engine, session, owner, household, accounts
):
    """With no rule and a new payee per line, it was seven statements a line.

    A SELECT, an INSERT and a flush per new payee, then `from_history` for a
    payee created a moment ago. The three-shop test above cannot see that.
    """

    def counted(n, tag):
        staged = _stage(session, owner, household, accounts["checking"], _distinct(n, tag=tag), tag=tag)
        session.commit()
        with statements(engine) as seen:
            result = _commit(session, owner, household, accounts["checking"], staged)
        assert result["created"] == n
        return seen

    small = counted(10, "a")
    large = counted(70, "b")

    # With the change-insert sentinel (#229) the audit log's rows go in batches
    # too, so the whole commit is flat in the number of lines, log included.
    work = [len(_not_changes(small)), len(_not_changes(large))]
    assert work[1] - work[0] <= 2, f"{work[0]} statements besides the log for 10 rows, {work[1]} for 70"
    per_extra_row = (len(large) - len(small)) / 60
    assert per_extra_row <= 1.25, f"{len(small)} statements for 10 rows, {len(large)} for 70"
    made = session.execute(
        select(func.count()).select_from(Payee).where(
            Payee.household_id == household.id, Payee.name.like("PAGO MOVIL SHOP b %")
        )
    ).scalar_one()
    assert made == 70, "and every line still got its own payee"
    linked = session.execute(
        select(func.count()).where(
            Transaction.import_source == "b.csv", Transaction.payee_id.is_not(None)
        )
    ).scalar_one()
    assert linked == 70


def _ynab_source(n_plain: int, n_pairs: int):
    """A Register.csv of `n_plain` rows with their own payees and `n_pairs`
    transfers between the two accounts, both legs present."""
    from app.services.one_time_import import ynab_source
    from tests.test_one_time_import_ynab import _register, _row

    rows = []
    for i in range(n_plain):
        day = date(2025, 1, 1) + timedelta(days=i % 300)
        rows.append(_row("Checking", day.isoformat(), f"Shop {i}", out=f"€{10 + i}.00"))
    for i in range(n_pairs):
        day = (date(2025, 1, 1) + timedelta(days=i * 3)).isoformat()
        rows.append(_row("Checking", day, "Transfer : Visa", out=f"€{500 + i}.00"))
        rows.append(_row("Visa", day, "Transfer : Checking", inn=f"€{500 + i}.00"))
    return ynab_source.from_file(_register(rows), "Register.csv")


def _ynab_plan(accounts):
    from app.services.one_time_import import engine as one_time

    return one_time.Plan(
        currency="EUR",
        date_format="YYYY-MM-DD",
        accounts={
            "Checking": {"kind": "existing", "account_id": accounts["checking"].id},
            "Visa": {"kind": "existing", "account_id": accounts["card"].id},
        },
        categories={},
        acknowledge_cleared_reset=True,
    )


def test_a_one_time_import_commits_in_a_few_statements_per_row(
    engine, session, owner, household, accounts
):
    """10.4k rows were 13,575 statements: a payee SELECT and INSERT per new
    payee, and `_payments`, `_rejections` and two `transfer_payee` per pair."""
    from app.services.one_time_import import engine as one_time

    source = _ynab_source(400, 50)
    session.commit()
    with statements(engine) as seen:
        report = one_time.run(
            session, household, actor_id=owner.id, source=source,
            plan=_ynab_plan(accounts), commit=True,
        )
    assert report["counts"]["imported"] == 500
    assert report["counts"]["transfers_linked"] == 50
    assert len(seen) <= 3 * 500, f"{len(seen)} statements for 500 rows"
    # Besides the log's own inserts, a handful: not one per row, per payee or per pair.
    assert len(_not_changes(seen)) <= 40, _not_changes(seen)[:40]
    payee_reads = [one for one in seen if one.startswith("SELECT") and "FROM payees" in one]
    # Counted before and after, the source's names once, one transfer payee per account.
    assert len(payee_reads) <= 5, payee_reads
    # The report's count of rule suggestions (#269): the Rules screen's one
    # grouped read, once per commit, not a read per row or per payee.
    suggestion_reads = [one for one in seen if "import_payee_original" in one and "GROUP BY" in one]
    assert len(suggestion_reads) == 1, suggestion_reads
    assert report["rule_suggestions"] == 0
    legs = session.execute(
        select(Transaction).where(Transaction.transfer_transaction_id.is_not(None))
    ).scalars().all()
    assert len(legs) == 100
    assert {leg.payee.name for leg in legs} == {"Transfer : Checking", "Transfer : Visa"}


def _bulk_world(client, n: int) -> tuple[str, list[str]]:
    from app import db
    from tests.conftest import HEADERS, _setup_owner

    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    with db.SessionLocal() as seeding:
        owner_id = seeding.execute(text("select id from users limit 1")).scalar_one()
        with batch(seeding, kind=BatchKind.manual, actor_id=owner_id, household_id=house["id"]):
            rows = [
                Transaction(
                    household_id=house["id"], account_id=account["id"],
                    date=date(2026, 1, 1) + timedelta(days=i % 28), amount=-(100 + i),
                )
                for i in range(n)
            ]
            seeding.add_all(rows)
        seeding.commit()
        return house["id"], [row.id for row in rows]


def test_a_bulk_edit_flushes_once_not_once_per_row(client):
    """A 1,000-row bulk edit was 1,958 statements: a flush, so an UPDATE and a
    change insert, per row."""
    from app import db
    from tests.conftest import HEADERS

    house, ids = _bulk_world(client, 300)

    def edit(chosen, memo):
        with statements(db.engine) as seen:
            done = client.post(
                f"/api/households/{house}/transactions/bulk",
                json={"transaction_ids": chosen, "memo": memo, "reimbursement": "expected"},
                headers=HEADERS,
            )
        assert done.status_code == 200, done.text
        return seen

    small = edit(ids[:10], "ten")
    large = edit(ids[10:], "the rest")
    # The UPDATEs go as one executemany; what still grows is the log's change
    # insert per row, until the change-insert sentinel batches those too.
    assert len(_not_changes(large)) - len(_not_changes(small)) <= 3, (
        f"{len(_not_changes(small))} statements besides the log for 10 rows, "
        f"{len(_not_changes(large))} for 290"
    )
    assert len(large) - len(small) <= 280 + 3
    with db.SessionLocal() as reading:
        memos = dict(
            reading.execute(
                select(Transaction.memo, func.count()).where(Transaction.id.in_(ids)).group_by(Transaction.memo)
            ).all()
        )
        flagged = reading.execute(
            select(func.count()).where(Transaction.id.in_(ids), Transaction.reimbursement.is_not(None))
        ).scalar_one()
        logged = reading.execute(
            select(func.count()).select_from(Change).where(
                Change.table_name == "transactions", Change.op == ChangeOp.update,
                Change.row_id.in_(ids),
            )
        ).scalar_one()
    assert memos == {"ten": 10, "the rest": 290}
    assert flagged == 300
    assert logged == 300, "one change row per edited row, still"


def test_linking_many_expenses_to_one_payment_reads_payments_once(
    engine, session, owner, household, accounts
):
    """200 expenses were 802 statements: `is_payment` twice and a flush each."""
    from app.services import transactions as txn_service

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        expenses = [
            Transaction(
                household_id=household.id, account_id=accounts["card"].id,
                date=date(2026, 2, 1), amount=-(100 + i),
            )
            for i in range(200)
        ]
        payment = Transaction(
            household_id=household.id, account_id=accounts["checking"].id,
            date=date(2026, 2, 20), amount=sum(100 + i for i in range(200)),
        )
        session.add_all([*expenses, payment])
    session.commit()

    with (
        statements(engine) as seen,
        batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id),
    ):
        txn_service.link_reimbursements(session, expenses, payment)
    session.commit()

    assert len(_not_changes(seen)) <= 12, _not_changes(seen)
    assert len(seen) <= 200 + 12, f"{len(seen)} statements to link 200 expenses"
    linked = session.execute(
        select(func.count()).where(Transaction.reimbursed_by_id == payment.id)
    ).scalar_one()
    assert linked == 200


# --------------------------------------------------------------------------- #
# #237: read paths that built an object per row to copy a few fields
# --------------------------------------------------------------------------- #


@contextmanager
def _executed(engine) -> Iterator[list[tuple[str, tuple]]]:
    """Every statement sent inside the block, with its parameters, to EXPLAIN."""
    seen: list[tuple[str, tuple]] = []

    def keep(conn, cursor, statement, params, context, executemany):  # noqa: ARG001
        if not executemany:
            seen.append((statement, params))

    event.listen(engine, "before_cursor_execute", keep)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", keep)


def _plans(engine, seen: list[tuple[str, tuple]], starting: str = "SELECT") -> list[str]:
    out = []
    with engine.connect() as conn:
        for statement, params in seen:
            if statement.lstrip().startswith(starting):
                rows = conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", params).all()
                out.append("\n".join(str(row[-1]) for row in rows))
    return out


def test_who_entered_a_households_transactions_is_read_from_that_households_log(
    engine, session, household, other_household
):
    """`transactions_logged` searched `ix_changes_row_history` on its only
    usable prefix, the table name: every household's transaction changes."""
    from app.services import households

    with _executed(engine) as seen:
        households.transactions_logged(session, household.id)
    plan = "\n".join(_plans(engine, seen))
    assert "ix_changes_household_table_row" in plan, plan
    assert "SCAN changes" not in plan, plan


def test_where_a_transaction_came_from_is_an_index_search():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    plan = _plan(engine, select(ImportLine).where(ImportLine.transaction_id == "t" * 32))
    assert "ix_import_lines_transaction_id" in plan, plan
    assert "SCAN import_lines" not in plan, plan


def test_the_flow_reports_start_from_the_household_even_with_statistics():
    """With `ANALYZE` run -- which housekeeping does -- the planner took the
    transfer id index for `transfer_transaction_id IS NULL`, true of nearly
    every row, and read every household's ledger to keep one. `sqlite_stat1`
    averages rows per value, so it believed that meant a handful."""
    from app.services.reporting import flow_rows

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    ours, theirs = "a" * 32, "b" * 32
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            text(
                "INSERT INTO transactions (id, household_id, account_id, date, amount, cleared,"
                " created_at, updated_at, transfer_transaction_id) VALUES (:id, :house, :house,"
                " :day, -100, 'uncleared', '2026-01-01', '2026-01-01', :pair)"
            ),
            [
                {
                    "id": f"{n:032x}",
                    "house": ours if n % 5 == 0 else theirs,
                    "day": f"2026-{1 + n % 12:02d}-{1 + n % 28:02d}",
                    "pair": f"{n + 1:032x}" if n % 33 == 0 else None,
                }
                for n in range(6000)
            ],
        )
        conn.execute(text("ANALYZE"))
    plan = _plan(engine, flow_rows(ours).with_only_columns(func.count()))
    first = plan.splitlines()[0]
    assert first.startswith("SEARCH transactions") and "(household_id=?)" in first, plan
    assert "transfer_transaction_id" not in plan, plan


@pytest.mark.parametrize(
    "statistics", [None, "sampled", "full"], ids=["no-statistics", "sampled", "full"]
)
def test_the_flow_reports_read_the_repayments_once_whatever_the_statistics(statistics):
    """The "is this row somebody's repayment?" check, over every flow row.

    As a correlated EXISTS it was a probe per row, and which index the probe
    used was the planner's guess. With no statistics it searched the household
    index, which in a one-household ledger is every row; with full statistics
    it called the link index useless, `reimbursed_by_id` being NULL on nearly
    every row, and scanned. Either way each row read the whole table again:
    Income vs Expense took 41s over 10,403 transactions (#297). Read once as a
    list, no statistics can turn it back into a probe -- so the plan has no
    correlated subquery in it under none, under what housekeeping writes, and
    under a full `ANALYZE`.

    `NOT IN` is also where a rewrite could change an answer, so the rows each
    household keeps and drops are counted against the links themselves. Some
    links cross households, and a link names a repayment only for the
    household of the expense holding it.
    """
    from app.services.reporting import _is_reimbursement, flow_rows

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    ours, theirs = "a" * 32, "b" * 32
    rows = [
        {
            "id": f"{n:032x}",
            "house": ours if n % 5 else theirs,
            "day": f"2026-{1 + n % 12:02d}-{1 + n % 28:02d}",
            # n + 5 is always the same household; n + 1 never is.
            "paid_by": (
                f"{n + 5:032x}" if n % 97 == 0 else f"{n + 1:032x}" if n % 89 == 0 else None
            ),
        }
        for n in range(3000)
    ]
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=OFF"))
        conn.execute(
            text(
                "INSERT INTO transactions (id, household_id, account_id, date, amount, cleared,"
                " created_at, updated_at, reimbursed_by_id) VALUES (:id, :house, :house,"
                " :day, -100, 'uncleared', '2026-01-01', '2026-01-01', :paid_by)"
            ),
            rows,
        )
        if statistics == "sampled":
            conn.execute(text(f"PRAGMA analysis_limit={housekeeping.ANALYSIS_LIMIT}"))
        if statistics:
            conn.execute(text("ANALYZE"))

    for house in (ours, theirs):
        payments = {r["paid_by"] for r in rows if r["house"] == house and r["paid_by"]}
        to_drop = sum(1 for r in rows if r["house"] == house and r["id"] in payments)
        assert to_drop, "the fixture must give each household a repayment to drop"
        kept = flow_rows(house).with_only_columns(func.count())
        dropped = select(func.count()).select_from(Transaction).where(
            Transaction.household_id == house, _is_reimbursement(house)
        )
        for statement in (kept, dropped):
            plan = _plan(engine, statement)
            assert "CORRELATED" not in plan, plan
            assert "LIST SUBQUERY" in plan, plan
        with engine.connect() as conn:
            in_house = sum(1 for r in rows if r["house"] == house)
            assert conn.execute(kept).scalar_one() == in_house - to_drop
            assert conn.execute(dropped).scalar_one() == to_drop


def test_insights_aggregate_the_filtered_rows_directly(engine, session, household, accounts, owner):
    """`id IN (SELECT id ...)` materialised every matching id and probed the
    key once per row. The same totals come back with the WHERE applied to the
    aggregate itself, searched and grouped every way there is."""
    from app.models import Payee
    from app.services import insights
    from app.services import transactions as txn_service

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        shops = [payee_service.get_or_create(session, household.id, name) for name in SHOPS]
        for i in range(60):
            txn_service.create(
                session,
                account=(accounts["checking"], accounts["pounds"])[i % 2],
                date=date(2026, 1, 1) + timedelta(days=i * 3),
                amount=-(100 + i),
                payee=shops[i % 3] if i % 4 else None,
                memo="Marisol tapas" if i % 7 == 0 else None,
                category=None,
                flush=False,
            )
    session.commit()

    def reference(group_by, search):
        """The totals as the IN (SELECT id) version computed them."""
        base = txn_service.filtered(household.id, search=search)
        inner = base.with_only_columns(Transaction.id).subquery()
        label_id, label_name, needs = insights._label_for(group_by)
        if group_by is insights.GroupBy.month:
            label_id = label_name = func.strftime("%Y-%m", Transaction.date)
        stmt = insights._joined(
            select(
                insights.Account.currency, label_id, label_name,
                func.sum(Transaction.amount), func.count(),
            ).select_from(Transaction),
            needs,
        ).where(Transaction.id.in_(select(inner.c.id))).group_by(
            insights.Account.currency, label_id, label_name
        )
        return sorted((c, n or "", int(s), int(k)) for c, _key, n, s, k in session.execute(stmt).all())

    for group_by in insights.GroupBy:
        for search in (None, "marisol"):
            with _executed(engine) as seen:
                answer = insights.summary(session, household.id, group_by=group_by, search=search)
            got = sorted(
                (currency, "" if g.name == insights.UNSET_NAME[group_by] else g.name, g.sum_minor, g.count)
                for currency, totals in answer.items()
                for g in totals.groups
            )
            assert got == reference(group_by, search), (group_by, search)
            assert not [p for p in _plans(engine, seen) if _probes_by_id(p)]
    with _executed(engine) as seen:
        series = insights.timeseries(session, household.id, search="marisol")
    assert sum(t.count for t in series.values()) == session.execute(
        select(func.count()).where(
            Transaction.household_id == household.id,
            Transaction.memo.ilike("%marisol%")
            | Transaction.payee_id.in_(select(Payee.id).where(Payee.name.ilike("%marisol%"))),
        )
    ).scalar_one()
    assert not [p for p in _plans(engine, seen) if _probes_by_id(p)]


def _probes_by_id(plan: str) -> bool:
    """The IN (SELECT id) shape: the ledger searched by primary key per row.

    Searching payees by name is still a subquery, and a correct one; what went
    is materialising the matching transactions' ids to look each one up again.
    """
    return "sqlite_autoindex_transactions_1 (id=?)" in plan


def test_the_app_connection_syncs_at_checkpoints_and_keeps_a_real_cache(tmp_path):
    """`synchronous=FULL` under WAL fsynced every commit; the cache was 2 MiB."""
    from sqlalchemy import event as sa_event

    from app import db

    engine = create_engine(f"sqlite:///{tmp_path / 'pragmas.sqlite3'}")
    sa_event.listen(engine, "connect", db._sqlite_pragmas)
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
            assert conn.exec_driver_sql("PRAGMA synchronous").scalar() == 1  # NORMAL
            assert conn.exec_driver_sql("PRAGMA cache_size").scalar() == -(db.CACHE_BYTES // 1024)
            assert db.CACHE_FLOOR <= db.CACHE_BYTES <= db.CACHE_CEILING
            assert conn.exec_driver_sql("PRAGMA temp_store").scalar() == 2  # MEMORY
            assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        engine.dispose()


@contextmanager
def _built(model) -> Iterator[list[str]]:
    """Every `model` instance loaded from the database inside the block."""
    from sqlalchemy.orm import Session as OrmSession

    seen: list[str] = []

    def loaded(_session, instance):
        if isinstance(instance, model):
            seen.append(instance.id)

    event.listen(OrmSession, "loaded_as_persistent", loaded)
    try:
        yield seen
    finally:
        event.remove(OrmSession, "loaded_as_persistent", loaded)


@pytest.fixture()
def big_account(session, owner, household, accounts):
    """5,000 rows on one account over a year and a half, a payee on some of
    them, and a reconciliation six months in."""
    from app.models import Reconciliation

    checking = accounts["checking"]
    shop = None
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        shop = payee_service.get_or_create(session, household.id, "Mercadona")
        session.add_all(
            Transaction(
                household_id=household.id, account_id=checking.id,
                date=date(2025, 1, 1) + timedelta(days=i % 540), amount=-(100 + i),
                payee_id=shop.id if i % 3 == 0 else None,
                cleared=ClearedState.reconciled if i % 540 < 180 else ClearedState.cleared,
            )
            for i in range(5000)
        )
        session.add(
            Reconciliation(
                account_id=checking.id,
                statement_date=date(2025, 6, 29), statement_balance=0, batch_id=None,
            )
        )
    session.commit()
    session.expunge_all()
    return checking


def test_the_reconcile_worksheet_reads_columns_in_one_statement(engine, session, big_account):
    from app.services import reconciling

    until = date(2026, 6, 30)
    with statements(engine) as seen, _built(Transaction) as built:
        sheet = reconciling.worksheet(session, big_account, until=until)

    assert built == [], f"{len(built)} Transaction objects built to copy five fields"
    reads = [one for one in seen if "FROM transactions" in one and "sum(" not in one]
    assert len(reads) == 1, reads
    expected = session.execute(
        select(func.count()).where(
            Transaction.account_id == big_account.id,
            Transaction.cleared != ClearedState.reconciled,
            Transaction.date <= until,
        )
    ).scalar_one()
    assert len(sheet.candidates) == expected > 3000
    first = sheet.candidates[0]
    assert (first.cleared, first.payee in (None, "Mercadona")) == ("cleared", True)
    assert [one.date for one in sheet.candidates] == sorted(one.date for one in sheet.candidates)


def test_the_worksheet_without_a_date_stops_45_days_after_the_last_statement(session, big_account):
    from app.services import reconciling

    sheet = reconciling.worksheet(session, big_account)
    reach = date(2025, 6, 29) + timedelta(days=reconciling.DEFAULT_REACH_DAYS)
    assert sheet.candidates, "rows up to the reach are still offered"
    assert max(one.date for one in sheet.candidates) == reach
    later = reconciling.worksheet(session, big_account, until=date(2026, 12, 31))
    assert max(one.date for one in later.candidates) > reach, "and a date sent is a date obeyed"


def _review_by_walking(session, household_id, *, min_rows=3, dominant_percent=80, limit=100):
    """The review as it was computed before #237: every row into Python."""
    from collections import Counter

    from sqlalchemy import or_

    from app.services import review
    from app.services import transactions as txn_service

    base = txn_service.filtered(household_id)
    is_leg = or_(
        Transaction.transfer_account_id.is_not(None),
        Transaction.transfer_transaction_id.is_not(None),
    )
    rows = session.execute(
        base.where(~is_leg)
        .outerjoin(Payee, Payee.id == Transaction.payee_id)
        .where(Payee.system.is_(None))
        .with_only_columns(Transaction.id, Transaction.payee_id, Transaction.category_id)
        .order_by(Transaction.date.desc(), Transaction.created_at.desc(), Transaction.id.desc())
    ).all()
    names = review.category_names(session, household_id)
    by_payee: dict[str, list] = {}
    for row in rows:
        if row[1] is not None:
            by_payee.setdefault(row[1], []).append(row)
    usual, profiles = {}, []
    for payee_id, its in by_payee.items():
        counts = Counter(row[2] for row in its if row[2] is not None)
        categorised = sum(counts.values())
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], names.get(kv[0], ""), kv[0]))
        if ranked and categorised >= min_rows and ranked[0][1] * 100 >= dominant_percent * categorised:
            usual[payee_id] = ranked[0][0]
        if len(its) >= min_rows:
            profiles.append((payee_id, len(its), categorised, ranked, usual.get(payee_id)))
    outliers, with_usual, without_usual = [], [], []
    for row in rows:
        chosen = usual.get(row[1]) if row[1] else None
        if row[2] is None:
            (with_usual if chosen else without_usual).append(row[0])
        elif chosen and row[2] != chosen:
            outliers.append(row[0])
    return {
        "rows": len(rows),
        "profiles": sorted(profiles),
        "outliers": (outliers[:limit], len(outliers)),
        "with_usual": (with_usual[:limit], len(with_usual)),
        "without_usual": (without_usual[:limit], len(without_usual)),
    }


@pytest.fixture()
def reviewed_ledger(session, owner, household, accounts):
    """5,000 rows over forty payees and four categories: usual, split, thin,
    uncategorised, a transfer leg and an opening balance among them."""
    from app.models import SystemPayee
    from app.services import categories as category_service

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        cats = [
            category_service.ensure(session, household.id, name, group_name="Spending")
            for name in ("Groceries", "Fuel", "Eating out", "Home")
        ]
        payees = [payee_service.get_or_create(session, household.id, f"Payee {i:02d}") for i in range(40)]
        opening = payee_service.get_or_create(
            session, household.id, "Opening balance", system=SystemPayee.opening_balance
        )
        session.flush()
        rows = []
        for i in range(5000):
            payee = payees[i % 40] if i % 17 else None
            # Payee n mostly takes category n % 4; every seventh row strays;
            # every eleventh is uncategorised; payees 30+ are thin or split.
            if payee is not None and int(payee.name[-2:]) >= 30:
                category = cats[i % 4] if i % 2 else None
            elif i % 11 == 0:
                category = None
            else:
                n = int(payee.name[-2:]) if payee is not None else 0
                category = cats[(n + (1 if i % 7 == 0 else 0)) % 4]
            rows.append(
                Transaction(
                    household_id=household.id, account_id=accounts["checking"].id,
                    date=date(2025, 1, 1) + timedelta(days=i % 400), amount=-(100 + i),
                    payee_id=payee.id if payee is not None else None,
                    category_id=category.id if category is not None else None,
                )
            )
        rows[0].payee_id = opening.id
        rows[1].transfer_account_id = accounts["card"].id
        session.add_all(rows)
    session.commit()
    session.expunge_all()


def test_the_categorisation_review_is_grouped_in_sql_and_says_what_walking_said(
    engine, session, household, reviewed_ledger
):
    import tracemalloc

    from app.services import review

    review.categorisation_review(session, household.id, limit=25)  # warm the caches
    tracemalloc.start()
    try:
        with statements(engine) as seen, _built(Transaction) as built:
            found = review.categorisation_review(session, household.id, limit=25)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(seen) <= 5, seen
    assert built == []
    # Walking every row into Python peaked at 3.3 MiB here (30.6 at 40k rows);
    # grouped in SQL it is the answer's size, not the ledger's.
    assert peak < 1 * 2**20, f"{peak / 2**20:.1f} MiB for a review capped at 25"
    walked = _review_by_walking(session, household.id, limit=25)
    assert found.rows_considered == walked["rows"] == 4998
    assert (found.excluded_transfer_legs, found.excluded_system_payee_rows) == (1, 1)
    reference = {pid: (n, c, list(ranked), u) for pid, n, c, ranked, u in walked["profiles"]}
    assert found.payees_total == len(reference) == 40
    for profile in found.payees:
        assert (
            profile.row_count,
            profile.categorised_count,
            [(one.category_id, one.count) for one in profile.categories],
            profile.usual_category_id,
        ) == reference[profile.payee_id]
    assert sum(1 for one in reference.values() if one[3]) >= 20, "most payees have a usual category"
    for mine, theirs in (
        ((found.outliers, found.outliers_total), walked["outliers"]),
        ((found.uncategorised_with_usual, found.uncategorised_with_usual_total), walked["with_usual"]),
        ((found.uncategorised_without_usual, found.uncategorised_without_usual_total), walked["without_usual"]),
    ):
        assert ([one.transaction_id for one in mine[0]], mine[1]) == theirs
        assert theirs[1] > 0, "each list has something in it, or the comparison proves nothing"


def test_the_default_register_request_stays_small_and_is_what_the_model_would_write(client):
    """25,000 rows were 89 MiB at peak: validated into models, then serialised
    again. At 5,000 rows it was 17 MiB, and 19.5 with the running balance."""
    import tracemalloc

    from app.schemas import RegisterPage
    from tests.conftest import HEADERS

    house, _ids = _bulk_world(client, 5000)
    account = client.get(f"/api/households/{house}/accounts", headers=HEADERS).json()[0]["id"]
    for url in (
        f"/api/households/{house}/transactions",
        f"/api/households/{house}/transactions?account_id={account}",
    ):
        client.get(url, headers=HEADERS)  # warm
        tracemalloc.start()
        try:
            answer = client.get(url, headers=HEADERS)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert answer.status_code == 200, answer.text
        assert peak < 10 * 2**20, f"{peak / 2**20:.1f} MiB for {url}"
        page = answer.json()
        assert page["total"] == len(page["transactions"]) == 5000
        # Byte for byte what validating through the declared model and
        # writing it back out produces: same fields, same order, same forms.
        assert RegisterPage.model_validate_json(answer.content).model_dump_json().encode() == (
            answer.content
        )
    assert page["has_running_balance"] is True
    balances = [row["running_balance"] for row in page["transactions"]]
    assert balances[0] == sum(-(100 + i) for i in range(5000)), "newest row carries the whole sum"


# --------------------------------------------------------------------------- #
# #240: six smaller ones
# --------------------------------------------------------------------------- #


def test_a_payee_merge_rewrites_in_chunks_and_logs_every_row(session, owner, household, accounts):
    """909 rows were 11.5 MiB as objects held at once; 10k would be ~120."""
    import tracemalloc

    from app.models import PayeeRule  # noqa: F401 -- the merge moves rules too

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        amazon = payee_service.get_or_create(session, household.id, "AMAZON MKTPLACE")
        target = payee_service.get_or_create(session, household.id, "Amazon")
        session.flush()
        session.add_all(
            Transaction(
                household_id=household.id, account_id=accounts["card"].id,
                date=date(2025, 1, 1) + timedelta(days=i % 365), amount=-(100 + i),
                payee_id=amazon.id,
            )
            for i in range(2000)
        )
    session.commit()
    session.expunge_all()
    amazon, target = session.get(Payee, amazon.id), session.get(Payee, target.id)

    tracemalloc.start()
    try:
        with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id) as merged:
            payee_service.merge(session, source=amazon, target=target)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    session.commit()

    assert peak < 20 * 2**20, f"{peak / 2**20:.1f} MiB to merge 2,000 rows"
    moved = session.execute(
        select(func.count()).where(Transaction.payee_id == target.id)
    ).scalar_one()
    logged = session.execute(
        select(func.count()).select_from(Change).where(
            Change.batch_id == merged.id, Change.table_name == "transactions"
        )
    ).scalar_one()
    assert moved == logged == 2000, "every row moved, and every move is in the log"


def test_the_setup_gate_asks_the_database_once_and_not_per_request(client):
    from app import db
    from tests.conftest import HEADERS, _setup_owner

    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    with statements(db.engine) as seen:
        for _ in range(100):
            assert client.get(f"/api/households/{house['id']}/accounts", headers=HEADERS).status_code == 200
    asked = [one for one in seen if "FROM instance " in one or one.endswith("FROM instance")]
    assert asked == [], f"{len(asked)} instance reads over 100 requests"


def test_the_setup_gate_still_refuses_before_setup_and_opens_after(client):
    from tests.conftest import HEADERS, _setup_owner

    refused = client.get("/api/households", headers=HEADERS)
    assert refused.status_code == 503
    assert "has not been set up" in refused.json()["detail"]
    _setup_owner(client)
    assert client.get("/api/households", headers=HEADERS).status_code == 200


@pytest.fixture()
def same_amount_year(session, owner, household, accounts):
    """300 ATM-sized outs and 300 top-ups of one amount over a year: the
    shape that made pairing quadratic."""
    import random

    from app.models import Account, AccountType

    pick = random.Random(240)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        saver = Account(
            household_id=household.id, name="Saver", type=AccountType.savings, currency="EUR"
        )
        session.add(saver)
        session.flush()
        session.add_all(
            Transaction(
                household_id=household.id,
                account_id=(accounts["checking"].id if amount < 0 else saver.id),
                date=date(2025, 1, 1) + timedelta(days=pick.randrange(365)),
                amount=amount,
            )
            for amount in [-2000] * 300 + [2000] * 300
        )
    session.commit()
    session.expunge_all()


def test_pairing_looks_only_inside_the_window_and_pairs_what_it_paired(
    session, household, same_amount_year, monkeypatch
):
    from app.services import identifiers as identifier_service
    from app.services import transfers

    evidence = transfers._evidence
    asked: list[tuple[str, str]] = []
    made: list[tuple[str, str]] = []
    reads: list[int] = []
    real_named_in = identifier_service.named_in

    def counted(ctx, out_leg, in_leg):
        asked.append((out_leg.id, in_leg.id))
        return evidence(ctx, out_leg, in_leg)

    class Recorded(transfers.Pair):
        __slots__ = ()

        def __init__(self, out_leg, in_leg, *rest):
            made.append((out_leg.id, in_leg.id))
            super().__init__(out_leg, in_leg, *rest)

    def named_in(*args, **kwargs):
        reads.append(1)
        return real_named_in(*args, **kwargs)

    monkeypatch.setattr(transfers, "_evidence", counted)
    monkeypatch.setattr(transfers, "Pair", Recorded)
    monkeypatch.setattr(identifier_service, "named_in", named_in)
    transfers.find(session, household.id)

    # The pairs the old loop made: every out against every in, in order.
    pool = list(
        session.execute(
            select(Transaction).where(Transaction.household_id == household.id).order_by(
                Transaction.date, Transaction.id
            )
        ).scalars()
    )
    window = timedelta(days=transfers.WINDOW_DAYS)
    outs = [t for t in pool if t.amount < 0]
    ins = [t for t in pool if t.amount > 0]
    near = [
        (o.id, i.id) for o in outs for i in ins if abs(i.date - o.date) <= window
    ]

    assert len(asked) == len(near) <= 300 * 12, f"{len(asked)} evidence calls for 300 x 300"
    assert sorted(asked) == sorted(near), "every pair in the window, and only those"
    assert made == [pair for pair in asked], "pairs made in the order they were asked about"
    assert len(reads) <= 600 + len(pool), f"{len(reads)} identifier scans for 600 rows"


def test_the_category_suggestion_is_the_one_scoring_every_pair_gave(monkeypatch):
    import difflib
    import random

    from app.services.one_time_import import engine as one_time

    pick = random.Random(240)
    words = ["Groceries", "Fuel", "Eating out", "Home", "Rent", "Kids", "Car", "Gifts",
             "Holidays", "Insurance", "Phone", "Gym", "Books", "Taxes", "Pets", "Garden"]
    live = [{"id": f"c{i}", "name": f"{pick.choice(words)} {pick.choice(words)}"} for i in range(200)]
    live += [{"id": "exact", "name": "Eating out"}, {"id": "tie", "name": "Eating out"}]
    source = [f"{pick.choice(words)} {pick.choice(words)}"[: pick.randrange(4, 20)] for _ in range(150)]
    source += ["Eating out", "Eating Out!", "Grocery", ""]

    ratios = 0
    real = difflib.SequenceMatcher.ratio

    def counted(self):
        nonlocal ratios
        ratios += 1
        return real(self)

    folded = [(one_time.fold(one["name"]), one) for one in live]
    for name in source:
        before = max(
            ((one_time.score(name, one["name"]), one) for one in live),
            key=lambda pair: pair[0],
            default=(0.0, None),
        )
        monkeypatch.setattr(difflib.SequenceMatcher, "ratio", counted)
        after = one_time._closest_category(name, folded)
        monkeypatch.setattr(difflib.SequenceMatcher, "ratio", real)
        if before[0] >= one_time.CATEGORY_SCORE:
            assert after == before, name
        else:
            assert after[1] is None or after[0] < one_time.CATEGORY_SCORE, name
    assert ratios < len(source) * len(live) // 4, f"{ratios} full ratios for {len(source)} x {len(live)}"


def test_a_repeat_one_time_import_reads_the_ledger_as_columns(session, owner, household, accounts):
    from app.services.one_time_import import engine as one_time

    source = _ynab_source(300, 10)
    plan = _ynab_plan(accounts)
    first = one_time.run(session, household, actor_id=owner.id, source=source, plan=plan, commit=True)
    assert first["counts"]["imported"] == 320
    session.expunge_all()

    with _built(Transaction) as built:
        again = one_time.run(
            session, household, actor_id=owner.id, source=_ynab_source(300, 10), plan=plan,
            commit=False,
        )
    assert built == [], f"{len(built)} ledger rows built as objects to find duplicates"
    assert again["counts"]["imported"] == 0
    assert again["counts"]["duplicates_skipped"] == 320


def test_a_locked_database_is_a_409_not_a_500(client, monkeypatch):
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from app.services import categories as category_service
    from tests.conftest import HEADERS, _setup_owner

    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()

    def locked(*_args, **_kwargs):
        raise OperationalError("DELETE FROM categories", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(category_service, "create_group", locked)
    refused = client.post(
        f"/api/households/{house['id']}/category-groups", json={"name": "Spending"}, headers=HEADERS
    )
    assert refused.status_code == 409, refused.text
    assert "busy with another change" in refused.json()["detail"]
    assert refused.headers.get("x-content-type-options") == "nosniff", "a refusal carries the headers too"

    def broken(*_args, **_kwargs):
        raise OperationalError("SELECT", {}, sqlite3.OperationalError("no such table: categories"))

    monkeypatch.setattr(category_service, "create_group", broken)
    with pytest.raises(OperationalError):
        client.post(
            f"/api/households/{house['id']}/category-groups", json={"name": "Spending"}, headers=HEADERS
        )
