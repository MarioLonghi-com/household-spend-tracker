"""The audit hook: does every write leave a complete, replayable record?

These assert the *contents* of the log, not that a log row appeared. A test
that checks a mechanism fired is not a test that the mechanism worked -- four of
the previous build's nineteen bugs lived behind exactly that shape.
"""

from __future__ import annotations

from datetime import date as _date

import pytest
from sqlalchemy import func, select, update

from app.audit.batch import batch
from app.audit.registry import EXPECTED_EXCLUDED, excluded_tables
from app.errors import BulkStatementForbidden, CrossHouseholdChange, NoOpenBatch
from app.models import Account, AccountType, Base, Batch, BatchKind, BatchStatus, Change, ChangeOp

JAN_DAY = _date(2026, 1, 15)


def _changes(session, table: str | None = None) -> list[Change]:
    stmt = select(Change).order_by(Change.seq)
    if table:
        stmt = stmt.where(Change.table_name == table)
    return list(session.execute(stmt).scalars())


# --------------------------------------------------------------------------- #
# Ids exist before the flush
# --------------------------------------------------------------------------- #


def test_a_change_row_names_the_row_it_describes(session, household, owner, accounts):
    """The id must be assigned at construction.

    Column defaults are evaluated by the persistence step, which runs after
    before_flush -- so without the eager-id event every change row would record
    row_id as null and the log could never be replayed.
    """
    rows = _changes(session, "accounts")
    assert len(rows) == 3
    assert {r.row_id for r in rows} == {a.id for a in accounts.values()}
    assert all(r.row_id is not None for r in rows)


# --------------------------------------------------------------------------- #
# Complete images
# --------------------------------------------------------------------------- #


def test_an_update_records_every_column_not_only_the_changed_one(session, household, owner, accounts):
    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        checking.name = "Everyday Checking"

    change = _changes(session, "accounts")[-1]
    assert change.op is ChangeOp.update
    assert change.before["name"] == "Checking"
    assert change.after["name"] == "Everyday Checking"
    # The whole row is present in both images, which is what makes undo a plain
    # assignment rather than a merge.
    assert change.before["currency"] == "EUR"
    assert change.before["type"] == "checking"
    assert set(change.before) == set(change.after)


def test_the_before_image_survives_a_commit_inside_the_batch(session, household, owner, accounts):
    """expire_on_commit=True would destroy it.

    An expired instance reports History(added=[new]) with no old value at all,
    so the update would be recorded with an empty before-image and could not be
    undone. This is why db.py sets expire_on_commit=False.
    """
    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        session.commit()
        checking.name = "Renamed After A Commit"

    change = _changes(session, "accounts")[-1]
    assert change.before["name"] == "Checking"
    assert change.after["name"] == "Renamed After A Commit"


def test_a_delete_records_the_row_as_the_database_held_it(session, household, owner, accounts):
    card = accounts["card"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        card.name = "Edited Before Deleting"
        session.delete(card)

    change = _changes(session, "accounts")[-1]
    assert change.op is ChangeOp.delete
    assert change.after is None
    # getattr() would report the pending edit; the database still holds "Visa".
    assert change.before["name"] == "Visa"


def test_touching_without_changing_writes_nothing(session, household, owner, accounts):
    before = len(_changes(session, "accounts"))
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        accounts["checking"].name = "Checking"  # same value

    assert len(_changes(session, "accounts")) == before


# --------------------------------------------------------------------------- #
# Failing loudly
# --------------------------------------------------------------------------- #


def test_a_write_with_no_batch_open_raises_and_persists_nothing(session, household):
    session.add(
        Account(household_id=household.id, name="Sneaky", type=AccountType.cash, currency="EUR")
    )
    with pytest.raises(NoOpenBatch, match="no batch open"):
        session.flush()

    session.rollback()
    remaining = session.execute(
        select(func.count()).select_from(Account).where(Account.name == "Sneaky")
    ).scalar_one()
    assert remaining == 0


def test_excluded_tables_need_no_batch(session, household, owner):
    """Sessions and login attempts churn constantly and carry no history worth
    keeping; auditing the audit log has no base case."""
    from app.models import LoginAttempt

    session.add(LoginAttempt(email_canonical="nobody@example.com", ip="127.0.0.1", ok=False))
    session.flush()  # must not raise
    assert _changes(session, "login_attempts") == []


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


def test_every_table_declares_whether_it_is_audited():
    with pytest.raises(TypeError, match="must declare __audit__"):

        class Undeclared(Base):
            __tablename__ = "undeclared"
            from sqlalchemy.orm import Mapped, mapped_column

            id: Mapped[str] = mapped_column(primary_key=True)


def test_the_exclusions_are_exactly_these():
    assert excluded_tables() == EXPECTED_EXCLUDED, (
        "a table joined or left the audit exclusions. If that was deliberate, "
        "change EXPECTED_EXCLUDED and say why in the commit message."
    )


def test_audited_models_have_a_single_column_id(household):
    from app.audit.registry import audited_models

    for name, model in audited_models().items():
        pk = list(model.__table__.primary_key.columns)
        assert len(pk) == 1 and pk[0].name == "id", f"{name} needs a single-column id primary key"


def test_audited_parents_cascade_through_the_orm():
    """A database-level cascade deletes rows the ORM never sees.

    Deleting an account would then log one delete and silently destroy every
    transaction on it -- and an undo would 'succeed', restoring an account with
    no history.

    There are exactly two honest shapes for a one-to-many off an audited parent:
    the children are *deleted* through the ORM (``delete-orphan``), or their
    foreign key is nullable and the hook clears it as an audited update
    (``_nullify_referencing_children``). What is forbidden is a third shape --
    no cascade and a non-nullable key -- where the database decides and the log
    never hears about it. Either way ``passive_deletes`` must stay off, since
    that is the switch that hands the decision back to the database.
    """
    from sqlalchemy.orm import interfaces

    from app.models import Base as B

    for mapper in B.registry.mappers:
        if not mapper.class_.__audit__:
            continue
        for rel in mapper.relationships:
            if rel.direction is not interfaces.ONETOMANY:
                continue
            where = f"{mapper.class_.__name__}.{rel.key}"
            assert not rel.passive_deletes, where
            if "delete-orphan" in rel.cascade:
                continue
            nullable = [remote for _, remote in rel.local_remote_pairs if remote.nullable]
            assert nullable, (
                f"{where} neither cascades through the ORM nor has a nullable key, "
                "so the database gets to decide and the audit log never sees it"
            )


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #


def test_a_password_hash_never_reaches_the_log(session, owner):
    raw = session.execute(select(Change).where(Change.table_name == "users")).scalars().all()
    assert raw, "creating a user should be audited"
    for change in raw:
        blob = f"{change.before}{change.after}"
        assert "argon2-placeholder" not in blob
        assert "password_hash" not in (change.after or {})
        assert "totp_secret" not in (change.after or {})
        assert "password_hash" in change.redacted


# --------------------------------------------------------------------------- #
# Batches
# --------------------------------------------------------------------------- #


def test_nesting_joins_the_outer_batch(session, household, owner, accounts):
    before = session.execute(select(func.count()).select_from(Batch)).scalar_one()
    # Deliberately nested: the nesting is the thing under test.
    with batch(  # noqa: SIM117
        session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id
    ) as outer:
        with batch(session, kind=BatchKind.manual, actor_id=owner.id) as inner:
            assert inner is outer
            accounts["checking"].note = "touched once"
    after = session.execute(select(func.count()).select_from(Batch)).scalar_one()
    assert after == before + 1


def test_a_failed_batch_leaves_a_record_and_no_changes(session, household, owner):
    with pytest.raises(ValueError), batch(
        session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
    ):
        session.add(
            Account(household_id=household.id, name="Doomed", type=AccountType.cash, currency="EUR")
        )
        session.flush()
        raise ValueError("something went wrong halfway")

    failed = session.execute(
        select(Batch).where(Batch.status == BatchStatus.failed)
    ).scalar_one()
    assert failed.finished_at is not None
    assert session.execute(
        select(func.count()).select_from(Account).where(Account.name == "Doomed")
    ).scalar_one() == 0


# --------------------------------------------------------------------------- #
# The blind spot
# --------------------------------------------------------------------------- #


def test_a_bulk_update_on_an_audited_table_is_refused(session, household, owner, accounts):
    with batch(
        session, kind=BatchKind.bulk_update, actor_id=owner.id, household_id=household.id
    ), pytest.raises(BulkStatementForbidden, match="bypasses the audit hook"):
        session.execute(update(Account).values(note="all at once"))


def test_a_bulk_update_on_an_excluded_table_is_allowed(session, owner):
    from app.models import WebSession

    session.execute(update(WebSession).values(ip="10.0.0.1"))  # must not raise


def test_a_change_only_to_redacted_columns_is_not_a_change(session, owner):
    """Signing in rehashes a password and moves the TOTP counter on. Both are
    columns the log deliberately does not capture, so the entry would record
    nothing but its own existence -- and would then block an undo of the batch
    that really did change that row. It must not demand a batch either.
    """
    before = len(_changes(session, "users"))

    owner.password_hash = "a-freshly-rehashed-value"
    owner.totp_last_counter = 999
    session.flush()  # no batch open, and none needed

    assert len(_changes(session, "users")) == before


def test_a_change_to_a_captured_column_still_needs_a_batch(session, owner):
    """The exemption above is narrow: touch anything real and the rule applies."""
    owner.display_name = "Renamed"
    with pytest.raises(NoOpenBatch):
        session.flush()
    session.rollback()


def test_no_audited_table_cascades_behind_the_orm(household):
    """A database-level cascade deletes rows the hook never sees.

    Deleting an account used to destroy the far leg of every transfer into it --
    a row in a *different* account -- with no change row, so an undo would
    report success having permanently lost it. Any CASCADE on an audited table
    must be covered by an ORM relationship that loads the children first.
    """
    from sqlalchemy.orm import interfaces

    from app.audit.registry import audited_models
    from app.models import Base as B

    covered: set[tuple[str, str]] = set()
    for mapper in B.registry.mappers:
        for rel in mapper.relationships:
            if rel.direction is interfaces.ONETOMANY and "delete-orphan" in rel.cascade:
                for column in rel.remote_side:
                    covered.add((column.table.name, column.name))

    offenders = []
    for name, model in audited_models().items():
        for fk in model.__table__.foreign_keys:
            if (fk.ondelete or "").upper() != "CASCADE":
                continue
            if (name, fk.parent.name) in covered:
                continue
            offenders.append(f"{name}.{fk.parent.name} -> {fk.column.table.name}")

    assert not offenders, (
        "these foreign keys cascade in the database with no ORM relationship to load "
        "the children, so the rows would vanish unlogged:\n  " + "\n  ".join(offenders)
    )


#: RESTRICT keys on audited tables with no relationship of their own, and what
#: orders their deletes instead.
RESTRICT_ORDERED_ELSEWHERE = {
    ("transactions", "household_id"):
        "a transaction goes with its account (Account.transactions) and an account "
        "with its household (Household.accounts), so the sort already puts it first",
}


def test_every_restrict_key_is_known_to_the_unit_of_work():
    """RESTRICT is checked at the statement, even with foreign keys deferred.

    Undo replays a whole batch in one flush with ``defer_foreign_keys`` on,
    which covers every other key -- but not RESTRICT, which SQLite enforces on
    the DELETE itself. The unit of work orders two tables' deletes only when a
    relationship joins them; without one it runs them in whatever order its
    sort produces. ``payees.transfer_account_id`` had no relationship, and
    undoing a YNAB import that created an account and linked a transfer into
    it deleted the account before its transfer payee about half the time.
    """
    from sqlalchemy.orm import configure_mappers

    from app.audit.registry import audited_models
    from app.models import Base as B

    configure_mappers()
    known: set[tuple[str, str]] = set()
    for mapper in B.registry.mappers:
        for rel in mapper.relationships:
            if rel.viewonly:
                continue
            for local, remote in rel.local_remote_pairs:
                known.update({(local.table.name, local.name), (remote.table.name, remote.name)})

    offenders = []
    for name, model in audited_models().items():
        for fk in model.__table__.foreign_keys:
            if (fk.ondelete or "").upper() != "RESTRICT":
                continue
            key = (name, fk.parent.name)
            if key in known or key in RESTRICT_ORDERED_ELSEWHERE:
                continue
            offenders.append(f"{name}.{fk.parent.name} -> {fk.column.table.name}")

    assert not offenders, (
        "these RESTRICT keys have no relationship, so a flush deleting both rows may "
        "delete the parent first and fail:\n  " + "\n  ".join(offenders)
    )


#: SET NULL keys on audited tables that deliberately have no one-to-many
#: relationship, and what clears them instead. A new key belongs here only with
#: a sentence naming the code that makes its nulling a logged update -- or a
#: reason it does not have to be.
SET_NULL_HANDLED_ELSEWHERE = {
    ("transactions", "transfer_transaction_id"):
        "transactions.delete() removes both legs, so no surviving leg is ever nulled",
    ("transactions", "category_id"):
        "categories.delete_category() clears every row's category_id through the ORM first",
    ("payees", "default_category_id"):
        "categories.delete_category() clears every payee's default first",
    ("receipts", "uploaded_by_id"): "who acted, not money: attribution only",
    ("transfer_rejections", "rejected_by_id"): "who acted, not money: attribution only",
    ("ignored_identifier_suggestions", "ignored_by_id"): "who acted, not money: attribution only",
    ("reconciliations", "batch_id"): "points at the unaudited batches table, which is never deleted",
}


def test_every_set_null_key_is_nulled_where_the_log_can_see_it(household):
    """A database SET NULL is an update the audit log never hears about.

    `hook._nullify_referencing_children` turns it into a logged one, but only
    by walking a one-to-many relationship. A nullable foreign key with no such
    relationship is therefore nulled by SQLite, unlogged, and the undo puts
    the parent back with nothing pointing at it. That trap was paid for on
    `payees.transactions` and `receipts.transaction_id`, and it is the one
    `transactions.reimbursed_by_id` turns on -- so every SET NULL key either
    has the relationship or is listed above with the code that covers it.
    """
    from sqlalchemy.orm import interfaces

    from app.audit.registry import audited_models
    from app.models import Base as B

    walked: set[tuple[str, str]] = set()
    for mapper in B.registry.mappers:
        for rel in mapper.relationships:
            if rel.direction is interfaces.ONETOMANY and "delete-orphan" not in rel.cascade:
                for column in rel.remote_side:
                    walked.add((column.table.name, column.name))

    offenders = []
    for name, model in audited_models().items():
        for fk in model.__table__.foreign_keys:
            if (fk.ondelete or "").upper() != "SET NULL":
                continue
            key = (name, fk.parent.name)
            if key in walked or key in SET_NULL_HANDLED_ELSEWHERE:
                continue
            offenders.append(f"{name}.{fk.parent.name} -> {fk.column.table.name}")

    assert not offenders, (
        "these keys are SET NULL with no one-to-many relationship to walk, so a "
        "delete nulls them unlogged:\n  " + "\n  ".join(offenders)
    )
    assert ("transactions", "reimbursed_by_id") in walked


@pytest.mark.parametrize(
    "shortcut", ["bulk_save_objects", "bulk_insert_mappings", "bulk_update_mappings"]
)
def test_the_legacy_bulk_shortcuts_are_closed(session, household, shortcut):
    """These never reach before_flush or do_orm_execute at all.

    They build statements directly, so they were three ways for a write to
    reach an audited table with nothing recorded and no batch demanded --
    which is exactly what CLAUDE.md promises cannot happen.
    """
    with pytest.raises(BulkStatementForbidden, match="audit"):
        getattr(session, shortcut)([])


def test_a_raw_sql_write_is_refused(session, household):
    from sqlalchemy import text

    with pytest.raises(BulkStatementForbidden, match="raw SQL write"):
        session.execute(text("UPDATE accounts SET name = 'renamed'"))


def test_a_raw_sql_read_and_a_pragma_are_fine(session, household):
    from sqlalchemy import text

    session.execute(text("PRAGMA foreign_keys"))
    rows = session.execute(text("SELECT count(*) FROM accounts")).scalar_one()
    assert rows >= 0


def test_reassigning_a_relationship_is_a_change(session, household, owner, accounts):
    """`txn.account = other` moves money between accounts.

    The foreign key is not touched until the unit of work synchronises it,
    which happens during the flush -- after the hook has run. Without folding
    the relationship's own history into the image, this looked like a no-op:
    no change row, and no batch demanded.
    """
    from app.services import transactions as txn_service

    checking, card = accounts["checking"], accounts["card"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn = txn_service.create(session, account=checking, date=JAN_DAY, amount=-5_000)

    txn.account = card
    with pytest.raises(NoOpenBatch):
        session.flush()
    session.rollback()

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn.account = card

    change = _changes(session, "transactions")[-1]
    assert change.before["account_id"] == checking.id
    assert change.after["account_id"] == card.id, "the image must show where the money went"


def test_removing_an_account_from_the_household_is_logged(session, household, owner, accounts):
    """delete-orphan removal is decided during the flush, so session.deleted is
    empty when the hook looks."""
    from app.services import transactions as txn_service

    checking = accounts["checking"]
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn_service.create(session, account=checking, date=JAN_DAY, amount=-1_000)

    household.accounts.remove(checking)
    with pytest.raises(NoOpenBatch):
        session.flush()
    session.rollback()


def test_an_inserts_after_image_is_the_row_that_was_written(session, owner, household):
    """It used to be a row that never existed.

    The persistence step evaluates `mapped_column(default=...)` after
    `before_flush`, so on a pending object history is empty for every such
    column and the snapshot wrote null: `closed: None` where the row holds
    False, `created_at: None`, `sort_order: None`. `Audit Log Decision.md`
    defines before/after as "JSON snapshots of the row", and the log was
    asserting something else.
    """
    from app.models import Account
    from app.services import accounts as account_service

    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session, household=household, name="Probe", type="checking"
        )
    session.commit()

    logged = session.execute(
        select(Change).where(Change.table_name == "accounts", Change.row_id == account.id)
    ).scalar_one()
    stored = session.get(Account, account.id)

    assert logged.after["closed"] == stored.closed is False
    assert logged.after["sort_order"] == stored.sort_order == 0
    assert logged.after["created_at"] == stored.created_at.isoformat()
    assert logged.after["name"] == stored.name == "Probe"
    # Nothing in the image may be null where the row is not.
    for key, value in logged.after.items():
        if value is None:
            assert getattr(stored, key) is None, f"after-image says {key} is null; the row is not"


# --------------------------------------------------------------------------- #
# One batch, one household (#77)
# --------------------------------------------------------------------------- #


def _theirs(session, member, other_household) -> Account:
    with batch(
        session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id
    ):
        account = Account(
            household_id=other_household.id, name="Theirs", type=AccountType.cash, currency="GBP"
        )
        session.add(account)
    return account


def test_a_batch_cannot_write_another_households_row(
    session, household, other_household, owner, member
):
    """Every change row is stamped with the batch's household, and History and
    undo are scoped by that stamp. A row from B written under A's batch would be
    readable, whole, and undoable by A's members -- so the hook refuses it,
    whatever route let the row through."""
    theirs = _theirs(session, member, other_household)
    logged = len(_changes(session))

    with pytest.raises(CrossHouseholdChange, match="another household"), batch(
        session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
    ):
        theirs.name = "Renamed from the wrong household"
        session.flush()

    session.expire_all()
    assert session.get(Account, theirs.id).name == "Theirs"
    assert len(_changes(session)) == logged, "no change row filed under the wrong household"


def test_a_row_cannot_be_moved_out_of_another_household_either(
    session, household, other_household, owner, member
):
    """The before-image is checked too: re-homing B's row into A under A's
    batch would still put B's snapshot in A's History."""
    theirs = _theirs(session, member, other_household)

    with pytest.raises(CrossHouseholdChange), batch(
        session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id
    ):
        theirs.household_id = household.id
        session.flush()

    session.expire_all()
    assert session.get(Account, theirs.id).household_id == other_household.id


def test_a_batch_filed_under_no_household_may_still_span_them(
    session, household, other_household, owner, member
):
    """Setup, sign-in and a household's own creation open a batch with no
    household. Those span households by design and must keep working."""
    theirs = _theirs(session, member, other_household)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        theirs.name = "Tidied"
        session.flush()

    session.expire_all()
    assert session.get(Account, theirs.id).name == "Tidied"
    assert _changes(session, "accounts")[-1].after["name"] == "Tidied"
