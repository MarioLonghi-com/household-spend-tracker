"""Statements that hold several accounts, charge fees, and are downloaded monthly.

Issues #68 and #69, written from Revolut's three exports. Every figure here is
synthetic: `mixed_products.csv` keeps the *shape* of Revolut's account
statement -- checking and savings in one file, fees, pending and reverted
rows, forty-five positive savings rows before the first negative one -- and
the savings statements below are typed out by hand.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

from app.audit.batch import batch
from app.errors import Conflict, ValidationError
from app.models import (
    Account,
    AccountType,
    BatchKind,
    BatchStatus,
    Category,
    ImportOutcome,
    Transaction,
)
from app.services import identifiers, importing
from statements import sniffing

MIXED = (Path(__file__).parent / "statement_files" / "mixed_products.csv").read_bytes()


def _stage(session, owner, household, account, raw: bytes, name="statement.csv"):
    sniffed = sniffing.sniff(raw)
    with batch(
        session,
        kind=BatchKind.imported,
        actor_id=owner.id,
        household_id=household.id,
        source={
            "filename": name,
            "sha256": importing.file_digest(raw),
            "account_id": account.id,
            "format": sniffed.format.describe(),
        },
    ) as staged:
        lines = importing.stage_file(
            session, account=account, raw=raw, fmt=sniffed.format, batch_row=staged
        )
        staged.status = BatchStatus.preview
    return staged, lines


def _commit(session, owner, household, account, staged):
    """Re-opened rather than a new batch, the way the route does it."""
    from app.audit.batch import resume

    with resume(session, staged, status=BatchStatus.applied):
        return importing.commit(session, batch_row=staged, account=account)


def _count(lines, outcome):
    return sum(1 for line in lines if line.outcome is outcome)


def _rows(session, account):
    return list(
        session.execute(
            select(Transaction)
            .where(Transaction.account_id == account.id)
            .order_by(Transaction.date, Transaction.amount)
        ).scalars()
    )


@pytest.fixture()
def revolut(session, owner, household, accounts):
    """A checking account and two savings pockets, beside the shared fixture's two."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        made = {
            "current": Account(
                household_id=household.id, name="Current", type=AccountType.checking,
                currency="EUR",
            ),
            "pocket_a": Account(
                household_id=household.id, name="Pocket A", type=AccountType.savings,
                currency="EUR",
            ),
            "pocket_b": Account(
                household_id=household.id, name="Pocket B", type=AccountType.savings,
                currency="EUR",
            ),
        }
        session.add_all(made.values())
    return made


# --------------------------------------------------------------------------- #
# #68 -- one file, several accounts
# --------------------------------------------------------------------------- #


def test_a_checking_account_takes_only_the_current_rows_of_a_mixed_file(
    session, owner, household, revolut
):
    """The whole bug: every Deposit row used to land in checking."""
    staged, lines = _stage(session, owner, household, revolut["current"], MIXED)

    # Six settled Current rows, plus a fee line for each of the two that
    # carried one. Forty-five Deposit rows and a zero-amount closing row are
    # skipped; one REVERTED and two PENDING rows are refused.
    assert _count(lines, ImportOutcome.created) == 8
    assert _count(lines, ImportOutcome.skipped) == 46
    assert _count(lines, ImportOutcome.rejected) == 3
    notes = (staged.source or {}).get("warnings") or []
    assert any("Only the Current rows" in note for note in notes), notes

    _commit(session, owner, household, revolut["current"], staged)
    # Exactly the statement's own closing checking balance.
    assert sum(row.amount for row in _rows(session, revolut["current"])) == 30000


def test_a_mixed_file_into_an_account_that_has_not_said_which_product_is_refused(
    session, owner, household, revolut
):
    with pytest.raises(ValidationError, match="has not said which of them"):
        _stage(session, owner, household, revolut["pocket_a"], MIXED)


def test_an_account_that_names_its_product_takes_those_rows(
    session, owner, household, revolut
):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        revolut["pocket_a"].statement_product = "Deposit"
    _, lines = _stage(session, owner, household, revolut["pocket_a"], MIXED)
    created = [line for line in lines if line.outcome is ImportOutcome.created]
    # Forty-five deposit rows, and thirty of them carry a fee.
    assert len([line for line in created if not line.parsed["import_id"].endswith(":fee")]) == 45
    # Six settled Current rows and the zero-amount closing row; the reverted and
    # pending Current rows are refused before anyone asks whose they are.
    assert _count(lines, ImportOutcome.skipped) == 7


def test_a_product_the_file_does_not_have_is_refused(session, owner, household, revolut):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        revolut["pocket_b"].statement_product = "Savings"
    with pytest.raises(ValidationError, match="has none"):
        _stage(session, owner, household, revolut["pocket_b"], MIXED)


def test_the_booked_date_is_the_one_that_is_kept(session, owner, household, revolut):
    """Started 1 March just before midnight, completed 2 March."""
    staged, _ = _stage(session, owner, household, revolut["current"], MIXED)
    _commit(session, owner, household, revolut["current"], staged)
    shop = next(r for r in _rows(session, revolut["current"]) if r.import_payee_original == "Corner Shop")
    assert shop.date == date(2026, 3, 2)


def test_a_fee_is_its_own_row_in_bank_fees(session, owner, household, revolut):
    staged, _ = _stage(session, owner, household, revolut["current"], MIXED)
    _commit(session, owner, household, revolut["current"], staged)

    fees = [r for r in _rows(session, revolut["current"]) if r.import_id.endswith(":fee")]
    assert sorted(r.amount for r in fees) == [-150, -40]
    assert {r.payee.name for r in fees} == {"Bank fee"}
    assert {r.category.name for r in fees} == {"Bank fees"}
    # And the payment itself is not netted: the ATM withdrawal is 50.00.
    atm = next(r for r in _rows(session, revolut["current"]) if "Cash withdrawal" in (r.import_payee_original or ""))
    assert atm.amount == -5000


def test_a_pending_row_waits_for_the_next_statement(session, owner, household, revolut):
    _, lines = _stage(session, owner, household, revolut["current"], MIXED)
    pending = [line for line in lines if "has not settled" in (line.reason or "")]
    assert len(pending) == 2


def test_a_reimport_adds_nothing_including_the_fees(session, owner, household, revolut):
    staged, _ = _stage(session, owner, household, revolut["current"], MIXED)
    _commit(session, owner, household, revolut["current"], staged)
    _, again = _stage(session, owner, household, revolut["current"], MIXED)
    assert _count(again, ImportOutcome.created) == 0
    assert _count(again, ImportOutcome.duplicate_skipped) == 8


def test_a_running_balance_that_stops_adding_up_is_said_out_loud(
    session, owner, household, revolut
):
    """A row missing from the middle is exactly what the balance column catches."""
    text = MIXED.decode()
    dropped = "\n".join(line for line in text.splitlines() if "Corner Shop" not in line) + "\n"
    staged, _ = _stage(session, owner, household, revolut["current"], dropped.encode())
    notes = (staged.source or {}).get("warnings") or []
    assert any("stops adding up" in note for note in notes), notes


def test_investments_are_spending_by_default(session, owner, household, revolut):
    raw = (
        b"Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
        b"Topup,Current,2026-04-01 09:00:00,2026-04-01 09:00:00,Payment from Employer Ltd,100.00,0.00,EUR,COMPLETED,100.00\n"
        b"Transfer,Current,2026-04-02 09:00:00,2026-04-02 09:00:00,To investment account,-10.00,0.00,EUR,COMPLETED,90.00\n"
        b"Transfer,Current,2026-04-03 09:00:00,2026-04-03 09:00:00,To Robo portfolio,-25.00,0.00,EUR,COMPLETED,65.00\n"
    )
    staged, _ = _stage(session, owner, household, revolut["current"], raw)
    _commit(session, owner, household, revolut["current"], staged)
    spent = [r for r in _rows(session, revolut["current"]) if r.amount < 0]
    assert {r.category.name for r in spent} == {"Investments"}


# --------------------------------------------------------------------------- #
# #69 -- a savings statement downloaded every month
# --------------------------------------------------------------------------- #

HEADER = "Transaction/Value Date,Description,AER,NIR,Money in,Money out,Balance\n"


def savings(*rows: tuple[str, str, str, str, str]) -> bytes:
    """(date, description, in, out, balance) -> a Revolut-shaped savings statement."""
    return (
        HEADER
        + "".join(
            f'"{when}","{what}",,,"{money_in}","{money_out}","{balance}"\n'
            for when, what, money_in, money_out, balance in rows
        )
    ).encode()


JANUARY = [
    ("Jan 5, 2026", "Deposit to 'Rainy Day'", "€100.00", "", "€100.00"),
    ("Jan 6, 2026", "Net Interest Paid to 'Rainy Day' for Jan 6, 2026", "€0.01", "", "€100.01"),
    ("Jan 7, 2026", "Net Interest Paid to 'Rainy Day' for Jan 7, 2026", "€0.00", "", "€100.01"),
    # In, out and in again on one day: rows one and three agree on the
    # amount, the date and the balance after them.
    ("Jan 8, 2026", "Deposit to 'Rainy Day'", "€50.00", "", "€150.01"),
    ("Jan 8, 2026", "Withdrawal from 'Rainy Day'", "", "€50.00", "€100.01"),
    ("Jan 8, 2026", "Deposit to 'Rainy Day'", "€50.00", "", "€150.01"),
]
FEBRUARY = [
    ("Feb 2, 2026", "Deposit to 'Rainy Day'", "€1,000.00", "", "€1,150.01"),
    ("Feb 3, 2026", "Net Interest Paid to 'Rainy Day' for Feb 3, 2026", "€0.06", "", "€1,150.07"),
]
MARCH = [
    ("Mar 2, 2026", "Deposit to 'Rainy Day'", "€10.00", "", "€1,160.07"),
    ("Mar 3, 2026", "Net Interest Paid to 'Rainy Day' for Mar 3, 2026", "€0.06", "", "€1,160.13"),
]


def test_a_savings_statement_lands_on_its_closing_balance_without_its_zero_rows(
    session, owner, household, revolut
):
    staged, lines = _stage(session, owner, household, revolut["pocket_a"], savings(*JANUARY))
    assert _count(lines, ImportOutcome.skipped) == 1  # the day that paid €0.00
    _commit(session, owner, household, revolut["pocket_a"], staged)
    rows = _rows(session, revolut["pocket_a"])
    assert sum(r.amount for r in rows) == 15001
    interest = [r for r in rows if r.amount == 1]
    assert [(r.payee.name, r.category.name) for r in interest] == [("Interest", "Interest income")]


def test_a_download_cut_mid_day_then_a_full_one_adds_only_what_is_new(
    session, owner, household, revolut
):
    """The nth-that-day key gets this wrong; the balance key does not."""
    first = JANUARY[:4]  # cut after the first of the three 8 January rows
    staged, _ = _stage(session, owner, household, revolut["pocket_a"], savings(*first))
    _commit(session, owner, household, revolut["pocket_a"], staged)

    staged, lines = _stage(
        session, owner, household, revolut["pocket_a"], savings(*JANUARY, *FEBRUARY)
    )
    assert _count(lines, ImportOutcome.created) == 4  # 8 Jan out, 8 Jan in, two in February
    _commit(session, owner, household, revolut["pocket_a"], staged)
    assert sum(r.amount for r in _rows(session, revolut["pocket_a"])) == 115007


def test_a_month_skipped_between_downloads_is_flagged_with_the_gap(
    session, owner, household, revolut
):
    staged, _ = _stage(session, owner, household, revolut["pocket_a"], savings(*JANUARY))
    _commit(session, owner, household, revolut["pocket_a"], staged)

    staged, _ = _stage(session, owner, household, revolut["pocket_a"], savings(*MARCH))
    notes = (staged.source or {}).get("warnings") or []
    assert any("may be missing" in note and "€1,000.06" in note for note in notes), notes


def test_a_download_that_carries_on_from_the_ledger_says_nothing(
    session, owner, household, revolut
):
    staged, _ = _stage(session, owner, household, revolut["pocket_a"], savings(*JANUARY))
    _commit(session, owner, household, revolut["pocket_a"], staged)
    staged, _ = _stage(session, owner, household, revolut["pocket_a"], savings(*FEBRUARY))
    assert not (staged.source or {}).get("warnings")


def test_rows_imported_before_the_balance_was_read_are_still_recognised(
    session, owner, household, revolut
):
    """An older build keyed these rows by (amount, date, nth that day)."""
    no_balance = (
        "Transaction/Value Date,Description,Money in,Money out\n"
        + "".join(f'"{w}","{d}",{i},{o}\n' for w, d, i, o, _ in JANUARY)
    ).encode()
    staged, _ = _stage(session, owner, household, revolut["pocket_a"], no_balance)
    _commit(session, owner, household, revolut["pocket_a"], staged)
    assert all(r.import_id.startswith("ST:") for r in _rows(session, revolut["pocket_a"]))

    _, lines = _stage(session, owner, household, revolut["pocket_a"], savings(*JANUARY))
    assert _count(lines, ImportOutcome.created) == 0


def test_a_pocket_statement_dropped_on_the_other_pocket_is_refused(
    session, owner, household, revolut
):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        identifiers.add(
            session, household_id=household.id, kind="alias", value="Rainy Day",
            account=revolut["pocket_a"],
        )
        identifiers.add(
            session, household_id=household.id, kind="alias", value="Holiday Fund",
            account=revolut["pocket_b"],
        )
    with pytest.raises(Conflict, match="this statement is for Pocket A"):
        _stage(session, owner, household, revolut["pocket_b"], savings(*JANUARY))


def test_an_import_that_made_a_category_takes_it_back_when_undone(
    session, owner, household, revolut
):
    from app.audit.undo import undo_batch

    staged, _ = _stage(session, owner, household, revolut["pocket_a"], savings(*JANUARY))
    _commit(session, owner, household, revolut["pocket_a"], staged)
    named = select(Category).where(
        Category.household_id == household.id, Category.name == "Interest income"
    )
    assert session.execute(named).scalar_one_or_none() is not None

    undo_batch(session, staged.id, actor_id=owner.id)
    assert session.execute(named).scalar_one_or_none() is None
