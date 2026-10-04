"""A statement that says its currency is held to the account's (issue #260).

Every figure in a file is converted with the *account's* currency. Before this,
a `Currency` column was only kept in `details` and an OFX `CURDEF` only warned
when it was missing, so a USD row staged into a EUR account became the same
figure in euros, and nothing in the preview said so.

Two currencies, two accounts, every time: a EUR checking account and a USD
one, beside the shared fixture's EUR card and GBP savings.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch, resume
from app.errors import ValidationError
from app.models import (
    Account,
    AccountType,
    Batch,
    BatchKind,
    BatchStatus,
    ImportLine,
    ImportOutcome,
    Transaction,
)
from app.services import importing
from statements import parse, sniffing

FIXTURES = Path(__file__).parent / "statement_files"

#: A Revolut-shaped export holding two currencies, the way one download of a
#: multi-currency account does. Synthetic payees, synthetic figures.
MIXED = (
    b"Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance\n"
    b"Card Payment,Current,2026-09-02 10:00:00,2026-09-02 10:00:01,Corner Bakery,-12.50,0.00,EUR,COMPLETED,987.50\n"
    b"Card Payment,Current,2026-09-03 11:00:00,2026-09-03 11:00:01,Diner Abroad,-40.00,0.00,USD,COMPLETED,460.00\n"
    b"Topup,Current,2026-09-04 09:00:00,2026-09-04 09:00:01,Payment from Example,200.00,0.00,EUR,COMPLETED,1187.50\n"
    b"Card Payment,Current,2026-09-05 12:00:00,2026-09-05 12:00:01,Museum Shop,-15.25,0.00,USD,COMPLETED,444.75\n"
)

ALL_USD = (
    b"Date,Description,Amount,Currency\n"
    b"2026-09-02,Diner Abroad,-40.00,USD\n"
    b"2026-09-05,Museum Shop,-15.25,USD\n"
)


def _ofx(curdef: str) -> bytes:
    return (
        "OFXHEADER:100\n\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>"
        f"<CURDEF>{curdef}</CURDEF><BANKTRANLIST>"
        "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260902<TRNAMT>-40.00<FITID>CUR-1<NAME>DINER ABROAD</STMTTRN>"
        "<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260904<TRNAMT>200.00<FITID>CUR-2<NAME>EXAMPLE PAYER</STMTTRN>"
        "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    ).encode()


@pytest.fixture()
def dollars(session, owner, household, accounts) -> Account:
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        made = Account(
            household_id=household.id, name="US Checking", type=AccountType.checking,
            currency="USD",
        )
        session.add(made)
    return made


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


def _commit(session, account, staged):
    with resume(session, staged, status=BatchStatus.applied):
        return importing.commit(session, batch_row=staged, account=account)


def _amounts(session, account) -> list[tuple[date, int]]:
    return [
        (row.date, row.amount)
        for row in session.execute(
            select(Transaction)
            .where(Transaction.account_id == account.id)
            .order_by(Transaction.date, Transaction.amount)
        ).scalars()
    ]


def _all_amounts(session, household) -> list[int]:
    return sorted(
        session.execute(
            select(Transaction.amount).where(Transaction.household_id == household.id)
        ).scalars()
    )


# --------------------------------------------------------------------------- #
# The library reads the currency a file states
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("header", ["Currency", "Moneda", "Divisa", "CCY"])
def test_a_currency_column_is_found_by_its_name(header):
    raw = f"Date,Description,Amount,{header}\n2026-09-02,Diner Abroad,-40.00,usd\n".encode()
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.currency_column == header
    assert sniffed.format.describe()["currency_column"] == header
    [row] = parse(raw, sniffed.format)
    # Upper-cased, so `usd` and `USD` are one currency.
    assert row.currency == "USD"
    assert row.amount is not None and str(row.amount) == "-40.00"


def test_an_original_currency_column_is_not_the_rows_currency():
    """A card bill in EUR that notes what a purchase cost abroad is still EUR."""
    raw = (
        b"Date,Description,Amount,Original Amount,Original Currency\n"
        b"2026-09-02,Diner Abroad,-36.80,-40.00,USD\n"
    )
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.currency_column is None
    [row] = parse(raw, sniffed.format)
    assert row.currency is None
    assert row.details["Original Currency"] == "USD"


def test_a_cell_that_is_not_a_currency_code_states_nothing():
    raw = b"Date,Description,Amount,Currency\n2026-09-02,Bakery,-12.50,\xe2\x82\xac\n"
    [row] = parse(raw, sniffing.sniff(raw).format)
    assert row.currency is None


def test_the_revolut_fixture_carries_its_currency_on_every_row():
    raw = (FIXTURES / "signed_with_state.csv").read_bytes()
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.currency_column == "Currency"
    rows = [r for r in parse(raw, sniffed.format) if not r.problem]
    assert [r.currency for r in rows] == ["EUR", "EUR"]


def test_ofx_curdef_is_carried_onto_every_row():
    raw = _ofx("usd")
    rows = parse(raw, sniffing.sniff(raw).format)
    assert [r.currency for r in rows] == ["USD", "USD"]


def test_an_ofx_transaction_in_its_own_currency_says_so():
    """`<CURRENCY>` inside a transaction is the currency of its amount; an
    `<ORIGCURRENCY>` is only where it came from, already converted."""
    raw = (
        b"OFXHEADER:100\n\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>EUR<BANKTRANLIST>"
        b"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260902<TRNAMT>-40.00<FITID>A1<NAME>ABROAD"
        b"<CURRENCY><CURRATE>0.92<CURSYM>USD</CURRENCY></STMTTRN>"
        b"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260903<TRNAMT>-36.80<FITID>A2<NAME>CONVERTED"
        b"<ORIGCURRENCY><CURRATE>0.92<CURSYM>USD</ORIGCURRENCY></STMTTRN>"
        b"</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    )
    rows = parse(raw, sniffing.sniff(raw).format)
    assert [r.currency for r in rows] == ["USD", "EUR"]


@pytest.mark.parametrize(
    "name", ["preamble_spanish.xls", "card_bill.pdf", "two_amount_columns.pdf"]
)
def test_a_spreadsheet_or_pdf_with_no_currency_column_reads_as_before(name):
    """The converted paths go through the same sniffer; with nothing stating a
    currency, every row is left to the account's, as it always was."""
    raw = (FIXTURES / name).read_bytes()
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.currency_column is None
    rows = [r for r in parse(raw, sniffed.format) if not r.problem]
    assert rows
    assert {r.currency for r in rows} == {None}


# --------------------------------------------------------------------------- #
# Staging holds a stated currency to the account's
# --------------------------------------------------------------------------- #


def test_a_usd_row_never_becomes_euros(session, owner, household, accounts, dollars):
    """The issue's reproduction, and the ledger is what is asserted."""
    checking = accounts["checking"]
    staged, lines = _stage(session, owner, household, checking, MIXED)

    by_line = {line.line_no: line for line in lines}
    assert by_line[2].outcome is ImportOutcome.created
    assert by_line[4].outcome is ImportOutcome.created
    for line_no in (3, 5):
        line = by_line[line_no]
        # The file holds EUR rows too, so these read as another account's.
        assert line.outcome is ImportOutcome.skipped
        assert "USD" in line.reason and "EUR" in line.reason
        assert checking.name in line.reason
        # No figure in euros is recorded for a row that is not in euros.
        assert line.parsed is None
    notes = (staged.source or {}).get("warnings") or []
    assert any("EUR (2), USD (2)" in note for note in notes), notes
    # The balances are per currency: the EUR rows alone must not be judged
    # against the USD rows' running balance.
    assert not any("stops adding up" in note for note in notes), notes

    result = _commit(session, checking, staged)
    assert result["created"] == 2
    assert _amounts(session, checking) == [(date(2026, 9, 2), -1250), (date(2026, 9, 4), 20000)]
    # Nowhere in the household is -40.00 or -15.25 recorded.
    assert _all_amounts(session, household) == [-1250, 20000]


def test_the_same_file_into_the_usd_account_takes_the_other_rows(
    session, owner, household, accounts, dollars
):
    staged, lines = _stage(session, owner, household, dollars, MIXED)
    skipped = [line for line in lines if line.outcome is ImportOutcome.skipped]
    assert sorted(line.line_no for line in skipped) == [2, 4]
    assert all("EUR" in line.reason and "USD" in line.reason for line in skipped)

    _commit(session, dollars, staged)
    assert _amounts(session, dollars) == [(date(2026, 9, 3), -4000), (date(2026, 9, 5), -1525)]
    assert _amounts(session, accounts["checking"]) == []


def test_a_stated_row_beside_unstated_ones_is_rejected(
    session, owner, household, accounts, dollars
):
    """Nothing else in the file is in another account's currency, so the row
    does not read as another account's: it is a line that cannot go here."""
    raw = (
        b"Date,Description,Amount,Currency\n"
        b"2026-09-02,Corner Bakery,-12.50,\n"
        b"2026-09-03,Diner Abroad,-40.00,USD\n"
    )
    checking = accounts["checking"]
    staged, lines = _stage(session, owner, household, checking, raw)
    by_line = {line.line_no: line for line in lines}
    assert by_line[2].outcome is ImportOutcome.created
    assert by_line[3].outcome is ImportOutcome.rejected
    assert "USD" in by_line[3].reason and "EUR" in by_line[3].reason

    _commit(session, checking, staged)
    assert _amounts(session, checking) == [(date(2026, 9, 2), -1250)]
    assert _all_amounts(session, household) == [-1250]


def test_a_file_entirely_in_another_currency_is_refused(
    session, owner, household, accounts, dollars
):
    checking = accounts["checking"]
    with pytest.raises(ValidationError) as refused:
        _stage(session, owner, household, checking, ALL_USD)
    sentence = str(refused.value)
    assert "USD" in sentence and "EUR" in sentence and checking.name in sentence

    # Refused before a line was staged, and nothing reached the ledger.
    assert session.execute(select(func.count()).select_from(ImportLine)).scalar_one() == 0
    # The batch is kept as a record of the attempt, but never as a preview
    # anyone could commit.
    assert session.execute(
        select(func.count())
        .select_from(Batch)
        .where(
            Batch.kind == BatchKind.imported,
            Batch.status.in_((BatchStatus.preview, BatchStatus.applied)),
        )
    ).scalar_one() == 0
    assert _all_amounts(session, household) == []


def test_a_file_entirely_in_another_currency_imports_into_its_own_account(
    session, owner, household, accounts, dollars
):
    staged, lines = _stage(session, owner, household, dollars, ALL_USD)
    assert [line.outcome for line in lines] == [ImportOutcome.created] * 2
    _commit(session, dollars, staged)
    assert _amounts(session, dollars) == [(date(2026, 9, 2), -4000), (date(2026, 9, 5), -1525)]


def test_an_ofx_in_usd_into_a_eur_account_is_refused(
    session, owner, household, accounts, dollars
):
    checking = accounts["checking"]
    with pytest.raises(ValidationError) as refused:
        _stage(session, owner, household, checking, _ofx("USD"), name="statement.ofx")
    assert "USD" in str(refused.value) and "EUR" in str(refused.value)
    assert _all_amounts(session, household) == []


def test_an_ofx_in_usd_into_the_usd_account_imports(
    session, owner, household, accounts, dollars
):
    staged, lines = _stage(session, owner, household, dollars, _ofx("USD"), name="statement.ofx")
    assert [line.outcome for line in lines] == [ImportOutcome.created] * 2
    _commit(session, dollars, staged)
    assert _amounts(session, dollars) == [(date(2026, 9, 2), -4000), (date(2026, 9, 4), 20000)]


def test_a_matching_currency_imports_as_it_always_did(
    session, owner, household, accounts, dollars
):
    """The Revolut fixture is EUR throughout; a EUR account takes all of it."""
    raw = (FIXTURES / "signed_with_state.csv").read_bytes()
    checking = accounts["checking"]
    staged, lines = _stage(session, owner, household, checking, raw)
    assert sorted(line.outcome.value for line in lines) == ["created", "created", "rejected"]
    _commit(session, checking, staged)
    assert _amounts(session, checking) == [(date(2026, 9, 3), -2400), (date(2026, 9, 3), 100000)]


def test_the_account_currency_is_compared_case_blind(
    session, owner, household, accounts, dollars
):
    raw = b"Date,Description,Amount,Currency\n2026-09-02,Corner Bakery,-12.50,eur\n"
    checking = accounts["checking"]
    staged, lines = _stage(session, owner, household, checking, raw)
    assert [line.outcome for line in lines] == [ImportOutcome.created]
    _commit(session, checking, staged)
    assert _amounts(session, checking) == [(date(2026, 9, 2), -1250)]



# --------------------------------------------------------------------------- #
# Which column is the posting currency (review of #277)
# --------------------------------------------------------------------------- #

#: A EUR card bill whose amounts are already converted, with the currency each
#: purchase was made in beside them -- once in Spanish, once in English with
#: the posting currency in a column of its own.
CONVERTED = {
    "moneda_origen": (
        b"Fecha;Concepto;Importe;Moneda origen\n"
        b"02/09/2026;DINER ABROAD;-36,80;USD\n"
        b"05/09/2026;MUSEUM SHOP;-14,03;USD\n"
        b"06/09/2026;CORNER BAKERY;-12,50;EUR\n",
        None,
    ),
    "local_and_account": (
        b"Date,Description,Amount,Local Currency,Account Currency\n"
        b"2026-09-02,Diner Abroad,-36.80,USD,EUR\n"
        b"2026-09-05,Museum Shop,-14.03,USD,EUR\n"
        b"2026-09-06,Corner Bakery,-12.50,EUR,EUR\n",
        "Account Currency",
    ),
}


@pytest.mark.parametrize("shape", sorted(CONVERTED), ids=str)
def test_converted_purchases_abroad_reach_the_eur_ledger(
    session, owner, household, accounts, dollars, shape
):
    """Every purchase abroad was being skipped when the column naming where it
    was spent was taken as the row's currency."""
    raw, column = CONVERTED[shape]
    assert sniffing.sniff(raw).format.currency_column == column

    card = accounts["card"]
    staged, lines = _stage(session, owner, household, card, raw)
    assert [line.outcome for line in lines] == [ImportOutcome.created] * 3
    _commit(session, card, staged)
    assert _amounts(session, card) == [
        (date(2026, 9, 2), -3680),
        (date(2026, 9, 5), -1403),
        (date(2026, 9, 6), -1250),
    ]
    assert _amounts(session, dollars) == []


@pytest.mark.parametrize(
    "header",
    [
        "Original Currency", "Moneda origen", "Moneda original", "Foreign Currency",
        "Local Currency", "Transaction Currency", "Divisa de la transacción",
        "Divisa de la operación", "Moneda operacion", "Exchange Currency",
    ],
)
def test_a_pre_conversion_currency_column_is_never_the_rows(header):
    raw = f"Date,Description,Amount,{header}\n2026-09-02,Diner Abroad,-36.80,USD\n".encode()
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.currency_column is None
    [row] = parse(raw, sniffed.format)
    assert row.currency is None


@pytest.mark.parametrize(
    ("headers", "chosen"),
    [
        ("Currency,Ccy Pair", "Currency"),
        ("Billing Currency,Card Currency", "Billing Currency"),
        ("Divisa,Divisa tarjeta", "Divisa"),
        ("Moneda liquidación,Moneda tarjeta", "Moneda liquidación"),
    ],
)
def test_the_posting_currency_wins_among_several(headers, chosen):
    raw = f"Date,Description,Amount,{headers}\n2026-09-02,Diner Abroad,-36.80,EUR,USD\n".encode()
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.currency_column == chosen
    assert [r.currency for r in parse(raw, sniffed.format)] == ["EUR"]


def test_two_posting_currency_columns_mean_none_is_used_and_it_is_said(
    session, owner, household, accounts, dollars
):
    raw = (
        b"Date,Description,Amount,Account Currency,Settlement Currency\n"
        b"2026-09-02,Diner Abroad,-36.80,EUR,USD\n"
    )
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.currency_column is None
    assert any(
        "Account Currency, Settlement Currency" in w and "none was used" in w
        for w in sniffed.warnings
    ), sniffed.warnings

    checking = accounts["checking"]
    staged, lines = _stage(session, owner, household, checking, raw)
    assert [line.outcome for line in lines] == [ImportOutcome.created]
    _commit(session, checking, staged)
    assert _amounts(session, checking) == [(date(2026, 9, 2), -3680)]


# --------------------------------------------------------------------------- #
# An OFX file is one account: another currency in it is rejected, not skipped
# --------------------------------------------------------------------------- #

EUR_OFX_WITH_A_USD_ROW = (
    b"OFXHEADER:100\n\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>EUR<BANKTRANLIST>"
    b"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260902<TRNAMT>-12.50<FITID>E1<NAME>CORNER BAKERY"
    b"</STMTTRN>"
    b"<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260903<TRNAMT>-40.00<FITID>E2<NAME>DINER ABROAD"
    b"<CURRENCY><CURRATE>0.92<CURSYM>USD</CURRENCY></STMTTRN>"
    b"</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
)


def test_an_ofx_transaction_in_another_currency_is_rejected_out_loud(
    session, owner, household, accounts, dollars
):
    from app.api.routers import agent

    checking = accounts["checking"]
    staged, lines = _stage(
        session, owner, household, checking, EUR_OFX_WITH_A_USD_ROW, name="statement.ofx"
    )
    by_line = {line.line_no: line for line in lines}
    assert by_line[1].outcome is ImportOutcome.created
    usd = by_line[2]
    # Never `skipped`: it is this account's spend, in money this account
    # does not hold, and the person has to see it.
    assert usd.outcome is ImportOutcome.rejected
    assert "USD" in usd.reason and "EUR" in usd.reason and "0.92" in usd.reason
    assert usd.parsed is None
    notes = (staged.source or {}).get("warnings") or []
    assert not any("more than one currency" in note for note in notes), notes

    # The agent's compact answer does not call this safe.
    decision = agent._decision(checking, lines, [])
    assert decision.safe_to_commit is False
    assert decision.rejected == 1

    _commit(session, checking, staged)
    # The rate was not used to convert anything: only the EUR row landed.
    assert _amounts(session, checking) == [(date(2026, 9, 2), -1250)]
    assert _all_amounts(session, household) == [-1250]
