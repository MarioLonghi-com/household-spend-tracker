"""Statement shapes that broke, or nearly did, kept as files (#90).

Three synthetic statements with the shapes of real exports, and the generic
undo of a statement line that an existing row absorbed. Each fixture has rows
going both ways, and each test asserts the figures it read, in minor units --
not that the file "imported".

- `f_valor_importe.csv`: a Spanish header that abbreviates *fecha* to "F.".
  "F. Valor" matched no date needle and did match the amount needle "valor",
  so the file had no date column and its value dates were read as amounts.
- `thousands_mixed.csv`: `1,234` beside `1,234.56` in one column. The second
  proves the decimal point; the first is then one thousand two hundred and
  thirty-four, not one point two three four.
- `blank_debit_with_balance.csv`: separate debit and credit columns with a
  running balance beside them. A blank debit cell is a credit row, and the
  balance is never the amount.

Not `repo_wide`: the fixtures are backend input, and `test_data_hygiene`
already reads every file in `statement_files/`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.models import Account, BatchKind, ClearedState, ImportOutcome, Transaction
from tests.test_import_bank_shapes import _commit, _rows, _stage

FILES = Path(__file__).parent / "statement_files"


def _read(name: str) -> bytes:
    return (FILES / name).read_bytes()


def _figures(lines) -> list[tuple[date, int]]:
    return sorted(
        (date.fromisoformat(line.parsed["date"]), int(line.parsed["amount"]))
        for line in lines
        if line.outcome is ImportOutcome.created
    )


def _balance(session, account) -> int:
    return sum(row.amount for row in _rows(session, account))


# --------------------------------------------------------------------------- #
# The three files
# --------------------------------------------------------------------------- #


def test_an_abbreviated_value_date_is_the_date_and_importe_the_amount(
    session, owner, household, accounts
):
    staged, lines = _stage(
        session, owner, household, accounts["checking"], _read("f_valor_importe.csv")
    )
    assert staged.source["format"]["date_column"] == "F. Valor"
    assert staged.source["format"]["amount_column"] == "Importe"
    assert _figures(lines) == [
        (date(2026, 2, 13), -1_234),
        (date(2026, 2, 14), 150_000),
        (date(2026, 2, 16), -4_890),
        (date(2026, 2, 17), 1_234),
    ]
    _commit(session, owner, household, accounts["checking"], staged)
    assert _balance(session, accounts["checking"]) == 145_110
    assert _rows(session, accounts["pounds"]) == [], "the other account took nothing"


def test_a_thousands_comma_beside_a_decimal_point_is_a_thousand(
    session, owner, household, accounts
):
    staged, lines = _stage(
        session, owner, household, accounts["pounds"], _read("thousands_mixed.csv")
    )
    assert _figures(lines) == [
        (date(2026, 2, 3), -123_400),
        (date(2026, 2, 4), 123_456),
        (date(2026, 2, 5), -450),
        (date(2026, 2, 6), 200_000),
    ]
    _commit(session, owner, household, accounts["pounds"], staged)
    assert _balance(session, accounts["pounds"]) == 199_606
    assert _rows(session, accounts["checking"]) == []


def test_a_blank_debit_cell_beside_a_balance_is_a_credit(session, owner, household, accounts):
    staged, lines = _stage(
        session, owner, household, accounts["checking"], _read("blank_debit_with_balance.csv")
    )
    fmt = staged.source["format"]
    assert (fmt["outflow_column"], fmt["inflow_column"]) == ("Debit", "Credit")
    assert fmt["balance_column"] == "Balance"
    assert fmt["amount_column"] is None, "the balance is never the amount"
    assert _figures(lines) == [
        (date(2026, 2, 3), -1_234),
        (date(2026, 2, 4), 150_000),
        (date(2026, 2, 5), -4_500),
        (date(2026, 2, 6), 12),
    ]
    _commit(session, owner, household, accounts["checking"], staged)
    assert _balance(session, accounts["checking"]) == 144_278


# --------------------------------------------------------------------------- #
# Undo of an absorbed row, in the general case
# --------------------------------------------------------------------------- #


def _by_hand(session, owner, account, when: date, amount: int, memo=None) -> Transaction:
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=account.household_id):
        row = Transaction(
            household_id=account.household_id,
            account_id=account.id,
            date=when,
            amount=amount,
            memo=memo,
            cleared=ClearedState.uncleared,
        )
        session.add(row)
    session.commit()
    return row


_IMAGE = (
    "date", "amount", "memo", "cleared", "import_id", "import_source",
    "import_payee_original", "import_alt_ids", "payee_id", "category_id",
)


def _image(row: Transaction) -> dict:
    return {name: getattr(row, name) for name in _IMAGE}


def test_undo_gives_an_absorbed_row_back_exactly_as_it_was(
    session, owner, household, accounts, other_household, member
):
    """Only the YNAB-absorb case was tested. This is every other one.

    A row typed in by hand before the statement arrived absorbs the statement's
    line: it takes the bank's id, its payee text and its source, and becomes
    cleared. Undoing the import has to give it all of that back -- and
    keep the row, which is the person's, while the rows the import created go.
    """
    checking, pounds = accounts["checking"], accounts["pounds"]
    typed = _by_hand(session, owner, checking, date(2026, 2, 13), -1_234)
    elsewhere = _by_hand(session, owner, pounds, date(2026, 2, 13), -1_234)
    with batch(session, kind=BatchKind.admin, actor_id=member.id):
        theirs_account = Account(
            household_id=other_household.id, name="Theirs", type=checking.type, currency="GBP"
        )
        session.add(theirs_account)
    session.commit()
    theirs = _by_hand(session, member, theirs_account, date(2026, 2, 13), -1_234)
    before = {row.id: _image(row) for row in (typed, elsewhere, theirs)}

    staged, lines = _stage(session, owner, household, checking, _read("f_valor_importe.csv"))
    absorbed = [line for line in lines if line.outcome is ImportOutcome.matched_existing]
    assert [line.transaction_id for line in absorbed] == [typed.id]
    _commit(session, owner, household, checking, staged)
    session.commit()

    session.refresh(typed)
    assert typed.import_id is not None
    assert typed.cleared is ClearedState.cleared
    assert typed.import_payee_original == "COMPRA TIENDA EJEMPLO"
    assert len(_rows(session, checking)) == 4

    undo_batch(session, staged.id, actor_id=owner.id)
    session.commit()

    for row in (typed, elsewhere, theirs):
        session.refresh(row)
        assert _image(row) == before[row.id], row.account_id
    remaining = session.execute(
        select(Transaction.id).where(Transaction.account_id == checking.id)
    ).scalars().all()
    assert remaining == [typed.id], "the created rows went and the typed one stayed"
    assert _balance(session, checking) == -1_234
    assert _balance(session, pounds) == -1_234
