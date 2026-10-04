"""Statement text somebody planted cannot make a later read slow (#221).

A csv cell may be 128 KiB, and the import stored it whole: SQLite does not
hold `String(300)` to its width, and the 500-char memo limit only guarded
JSON bodies. Two regexes then went quadratic on it on every open of the
Identifiers and Payee-suggestions screens, for everyone in the household.
"""

from __future__ import annotations

import time

from sqlalchemy import select

from app.models import ImportOutcome, Transaction
from app.services import identifier_suggestions
from app.services import payees as payee_service

from .test_importing import _commit, _stage

LONG = 131_072


def _planted(memo: str, payee: str) -> bytes:
    return (
        "Date,Payee,Memo,Amount\n"
        f"2026-01-05,{payee},{memo},-4.20\n"
        "2026-01-06,Bakery,Bread,-2.10\n"
    ).encode()


def test_a_planted_cell_is_cut_to_the_column_where_the_file_meets_the_ledger(
    session, owner, household, accounts
):
    account = accounts["checking"]
    raw = _planted("*" * LONG, "HOTELCOM" + "1" * (LONG - 9) + "a")
    staged, lines = _stage(session, owner, household, account, raw)
    assert [line.outcome for line in lines] == [ImportOutcome.created, ImportOutcome.created]
    _commit(session, owner, household, account, staged)

    rows = {
        txn.amount: txn
        for txn in session.execute(
            select(Transaction).where(Transaction.account_id == account.id)
        ).scalars()
    }
    planted, ordinary = rows[-420], rows[-210]
    assert planted.memo == "*" * 500
    assert planted.import_payee_original == ("HOTELCOM" + "1" * LONG)[:300]
    assert len(planted.import_payee_original) == 300
    # The ordinary row next to it is stored exactly as the bank wrote it.
    assert (ordinary.memo, ordinary.import_payee_original) == ("Bread", "Bakery")


def _timed(work) -> float:
    started = time.perf_counter()
    work()
    return time.perf_counter() - started


def test_a_run_of_stars_is_read_in_linear_time():
    # Was 5.2 s at 20,000 stars and quadratic beyond.
    assert _timed(lambda: identifier_suggestions.extract("*" * LONG)) < 0.5
    assert _timed(lambda: identifier_suggestions.extract("•" * LONG + "a")) < 0.5
    # And still reads the masked cards it exists for.
    assert [
        f.value for f in identifier_suggestions.extract("CARD ****1234 •••• 5678 XXXX9999")
    ] == ["1234", "5678", "9999"]


def test_a_run_of_digits_welded_to_a_name_is_stripped_in_linear_time():
    # Was 1.5 s at 20,000 digits and quadratic beyond.
    planted = "HOTELCOM" + "1" * LONG + "a"
    assert _timed(lambda: payee_service._reference_stripped(planted)) < 0.5
    assert payee_service._reference_stripped(planted) == planted
    assert payee_service._reference_stripped("HOTELCOM61963202362647") == "HOTELCOM"
