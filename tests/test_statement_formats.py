"""Every statement shape this importer has actually been given.

The fixtures in `tests/statement_files/` are synthetic -- the real exports they were
built from are somebody's bank records -- but each one keeps the property that
broke the importer, and the comments say which.

These assert *values*, not that parsing returned something. A statement that
parses into wrong numbers is worse than one that refuses, because nothing tells
you.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from statements import ofx, parsing, sniffing

STATEMENTS = Path(__file__).resolve().parent / "statement_files"


def read(name: str) -> tuple[sniffing.Sniffed, list[parsing.ParsedRow]]:
    raw = (STATEMENTS / name).read_bytes()
    sniffed = sniffing.sniff(raw)
    return sniffed, parsing.parse(raw, sniffed.format)


def good(rows: list[parsing.ParsedRow]) -> list[parsing.ParsedRow]:
    return [r for r in rows if not r.problem]


# --------------------------------------------------------------------------- #
# The one that was silently wrong
# --------------------------------------------------------------------------- #


def test_a_value_date_column_is_not_mistaken_for_the_amount():
    """"value" is an amount needle and "Transaction/Value date" is a date.

    The sniffer picked that column for both, so every amount was the date read
    as a number: "May 23, 2026" became 23.2026, and a EUR 100.00 deposit would
    have imported as EUR 23.20. Every row looked perfectly well-formed, the
    preview showed a full table, and nothing in the file or the screen said
    otherwise. A column now gets one job.
    """
    sniffed, rows = read("two_column_month_names.csv")
    fmt = sniffed.format

    assert fmt.date_column == "Transaction/Value date"
    assert fmt.amount_column is None, "the date column was taken for the amount"
    assert fmt.inflow_column == "Money in"
    assert fmt.outflow_column == "Money out"

    amounts = [r.amount for r in good(rows)]
    assert amounts == [Decimal("10.00"), Decimal("0.01"), Decimal("-10.00")]


def test_a_running_balance_is_never_the_amount():
    """A balance column sits right next to the money columns and reads like one."""
    fmt = read("two_column_month_names.csv")[0].format
    for column in (fmt.amount_column, fmt.inflow_column, fmt.outflow_column):
        assert column != "Balance"
    # AER and NIR are interest rates in the same file, and equally not money.
    assert fmt.amount_column not in {"AER", "NIR"}


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


def test_month_names_and_currency_symbols_are_read():
    rows = good(read("two_column_month_names.csv")[1])
    assert [r.when for r in rows] == [date(2025, 12, 24), date(2026, 1, 23), date(2026, 3, 2)]
    assert rows[0].payee == 'Deposit to "Savings"'


def test_money_out_is_negative_and_money_in_is_positive():
    """The two-column shape carries the direction in the column, not the sign."""
    rows = good(read("two_column_month_names.csv")[1])
    assert rows[0].amount > 0, "a deposit should be money in"
    assert rows[-1].amount < 0, "a withdrawal should be money out"


def test_a_header_below_a_title_block_is_found():
    """Banks put the account number and the balance above the table.

    Reading row 0 as the header gave a file with no date column and no amount
    column -- the most confusing failure this importer had, because the file
    plainly has both. `Format.skip_rows` existed from the first import slice and
    nothing ever set it.
    """
    sniffed, rows = read("preamble_swedish.csv")
    assert sniffed.format.skip_rows == 1
    assert sniffed.format.date_column == "Bokföringsdag"
    assert sniffed.format.amount_column == "Belopp"

    parsed = good(rows)
    assert len(parsed) == 2
    assert parsed[0].when == date(2026, 9, 7)
    assert parsed[0].amount == Decimal("-200.00")
    assert parsed[0].payee == "ICA NARA"


def test_line_numbers_skip_blank_lines_and_count_records():
    """What the Line column says, which the Import screen's gutter repeats (#128).

    Blank lines are dropped before anything is counted, the title block and
    header count, and a quoted cell with a line break in it is one record over
    two lines. `client/src/screens/import-raw-lines.test.tsx` numbers this same
    text in the browser and expects these same three numbers.
    """
    text = (
        "Bank of Nowhere, current account\n"
        "Statement for January 2026\n"
        "\n"
        "Date,Payee,Amount\n"
        "2026-01-02,Alpha Grocer,-12.50\n"
        "\n"
        "   \n"
        '2026-01-05,"Beta Books\n'
        'second line of the payee",-8.00\n'
        "2026-01-09,Gamma Cafe,-3.20"
    )
    _, rows = parsing.read(text.encode())
    assert [(row.line_no, row.raw.split(",")[0]) for row in rows] == [
        (4, "2026-01-02"),
        (5, "2026-01-05"),
        (6, "2026-01-09"),
    ]


def test_a_latin1_export_keeps_its_accents():
    """Decoded strictly, so "Bokföringsdag" does not arrive as "Bokf?ringsdag"."""
    sniffed, _ = read("preamble_swedish.csv")
    assert sniffed.format.encoding in {"iso-8859-1", "cp1252"}
    assert "ö" in sniffed.format.date_column


def test_a_spreadsheet_is_unpacked_and_then_read_like_any_table():
    """A bank .xls is a title block and a table, in a container."""
    sniffed, rows = read("preamble_spanish.xls")
    assert sniffed.format.date_column == "FECHA OPERACIÓN"
    assert sniffed.format.amount_column == "IMPORTE EUR"
    assert sniffed.format.skip_rows > 0, "the table does not start at row 0"

    parsed = good(rows)
    assert len(parsed) == 2
    assert parsed[0].when == date(2026, 9, 18)
    assert parsed[0].amount == Decimal("-51.92")
    assert "Energia" in parsed[0].payee


def test_an_xlsx_says_what_to_do_instead_of_failing_obscurely():
    from statements import spreadsheet

    with pytest.raises(spreadsheet.UnreadableSpreadsheet, match="xlsx"):
        spreadsheet.to_csv_bytes(b"PK\x03\x04" + b"0" * 64)


# --------------------------------------------------------------------------- #
# Rows the bank says did not happen
# --------------------------------------------------------------------------- #


def test_a_reverted_row_is_refused_rather_than_imported():
    """Importing one invents money movement.

    A real Revolut export carried six of them, -454.00 EUR between them, and
    nothing in the row distinguishes it from the forty-two that did happen. The
    file imported "cleanly".
    """
    sniffed, rows = read("signed_with_state.csv")
    assert sniffed.format.state_column == "State"

    refused = [r for r in rows if r.problem]
    assert len(refused) == 1
    assert "REVERTED" in refused[0].problem

    kept = good(rows)
    assert [r.amount for r in kept] == [Decimal("-24.00"), Decimal("1000.00")]


def test_a_datetime_stamp_is_read_as_its_date():
    kept = good(read("signed_with_state.csv")[1])
    assert kept[0].when == date(2026, 9, 3)


# --------------------------------------------------------------------------- #
# OFX, in both of its incompatible shapes
# --------------------------------------------------------------------------- #


def test_ofx_sgml_is_read_although_it_is_not_xml():
    """OFX 1.x is SGML with a KEY:VALUE header. No XML parser will read it."""
    sniffed, rows = read("bank_sgml.ofx")
    assert sniffed.format.kind == "ofx"

    parsed = good(rows)
    assert [r.when for r in parsed] == [date(2025, 9, 8), date(2025, 10, 1)]
    assert [r.amount for r in parsed] == [Decimal("150.00"), Decimal("-12.34")]
    assert parsed[0].payee == "A PERSON"
    assert parsed[1].memo == "card 1234"


def test_ofx_xml_with_unclosed_leaves_is_read():
    """2.x is XML, on one line, under a credit-card wrapper -- and banks still
    leave leaf elements unclosed, which is legal in 1.x and common in both."""
    sniffed, rows = read("card_xml.ofx")
    assert sniffed.format.kind == "ofx"

    parsed = good(rows)
    assert len(parsed) == 2
    assert parsed[0].when == date(2026, 7, 25), "the [-7:MST] suffix was not stripped"
    assert parsed[0].amount == Decimal("195.00")
    assert parsed[1].amount == Decimal("-8.99")


def test_ofx_carries_the_banks_own_transaction_id():
    """FITID is stable across downloads by specification, which makes it a
    better dedupe key than (amount, date, nth that day) can ever be."""
    _, rows = read("bank_sgml.ofx")
    assert [r.fitid for r in good(rows)] == ["202509080001", "202510010002"]


def test_ofx_is_not_sniffed_as_a_table():
    """It used to be, and the CSV reader chewed the markup into hundreds of
    rows with no date in any of them."""
    raw = (STATEMENTS / "bank_sgml.ofx").read_bytes()
    assert ofx.looks_like_ofx(raw)
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.date_column is None
    assert sniffed.row_count == 2


def test_an_ofx_file_states_its_currency():
    statement = ofx.read((STATEMENTS / "bank_sgml.ofx").read_bytes())
    assert statement.currency == "GBP"
    assert statement.account == "12345678"


# --------------------------------------------------------------------------- #
# PDF, where there are no columns -- only coordinates
# --------------------------------------------------------------------------- #


def test_the_balance_column_is_not_read_as_the_amount():
    """A PDF line is `2026-08-30 Uttag -5 000,00 kr 51 491,52 kr`.

    The last number on it is the running balance. Any reader that finds "a
    number on the line" imports the account's balance as a transaction, every
    row. This is the whole reason the PDF reader works from word positions
    rather than from extracted text.
    """
    sniffed, rows = read("two_amount_columns.pdf")
    assert sniffed.format.amount_column == "Belopp"
    assert sniffed.format.date_column == "Datum"

    parsed = good(rows)
    assert [r.amount for r in parsed] == [
        Decimal("-5000.00"),
        Decimal("-930.00"),
        Decimal("1421.53"),
    ]
    assert all(abs(r.amount) < Decimal("50000") for r in parsed), "a balance got in"


def test_a_row_with_only_a_balance_is_not_a_transaction():
    """An Itau statement puts a "SALDO DO DIA" line between each day's entries:
    a figure in the balance column and nothing in the amount. Those imported as
    0.00 rows sitting in the register looking like real ones."""
    rows = read("two_amount_columns.pdf")[1]
    refused = [r for r in rows if r.problem]
    assert any("no amount" in r.problem for r in refused)
    assert not any((r.payee or "") == "SALDO DO DIA" for r in good(rows))


def test_a_description_that_overflows_its_column_keeps_its_last_word():
    """Columns are measured from the header, and a long payee runs past the
    boundary: "PAG BOLETO PLANO DE APOSENTADORIA EXEMPLO -930,00" put EXEMPLO in
    the money cell. The amount still read correctly, so nothing looked wrong --
    the payee was just quietly a word short."""
    parsed = good(read("two_amount_columns.pdf")[1])
    overflowing = next(r for r in parsed if r.payee and r.payee.startswith("PAG BOLETO"))
    assert overflowing.payee.endswith("EXEMPLO")
    assert overflowing.amount == Decimal("-930.00")


def test_a_designed_bill_is_refused_rather_than_half_read():
    """A credit-card bill is summary boxes and marketing copy, not a table.

    Read as one it produced 146 rows of nonsense from a "header" that was
    really `Encargos R$ 119,74 - Valor solicitado R$ 1.098,90`. A header is
    labels; a row carrying figures is a summary. Importing half a bill as
    transactions is worse than a clear no.
    """
    from statements import pdf_statement

    raw = (STATEMENTS / "designed_bill.pdf").read_bytes()
    with pytest.raises(pdf_statement.UnreadablePdf, match="no statement table"):
        sniffing.sniff(raw)


def test_a_pdf_with_no_text_layer_says_so():
    from statements import pdf_statement

    empty = (
        b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>\nendobj\n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\n%%EOF\n"
    )
    with pytest.raises(pdf_statement.UnreadablePdf, match="scan|no text"):
        pdf_statement.to_csv_bytes(empty, score_header=sniffing._header_score)


# --------------------------------------------------------------------------- #
# Documents with a rule of their own
# --------------------------------------------------------------------------- #


def test_a_card_bill_imports_only_this_period_not_future_instalments():
    """The Itau fatura is two tables side by side.

    The left one is what you spent this period. The right one is instalments
    that fall due on *later* bills -- money not yet owed, and importing it
    would invent charges. They share visual lines, so the rule cuts by where
    the transaction table's own amount heading ends.
    """
    sniffed, rows = read("card_bill.pdf")
    parsed = good(rows)

    assert len(parsed) == 3, "a future instalment was imported"
    assert not any("ANUIDADE 03/12" in (r.payee or "") for r in parsed)


def test_a_card_bills_signs_are_reversed_from_the_paper():
    """A bill lists what you spent as a positive number. On a card account in
    this app, spending is money out."""
    parsed = good(read("card_bill.pdf")[1])
    spend = next(r for r in parsed if "Uber" in (r.payee or ""))
    refund = next(r for r in parsed if "CASHBACK" in (r.payee or ""))
    assert spend.amount < 0
    assert refund.amount > 0


def test_a_card_bills_entries_get_a_year_from_the_issue_date():
    """The entries are printed as `26/01` with no year."""
    parsed = good(read("card_bill.pdf")[1])
    # Issued 22/02/2026: January entries are the same year, and a December one
    # would be the year before.
    assert {r.when.year for r in parsed} == {2026}
    assert any(r.when.month == 1 for r in parsed)


def test_a_layout_that_stops_adding_up_is_refused():
    """This is the whole reason a per-bank rule is allowed to exist.

    A rule keyed to one bank's design is the least durable code here: the bank
    will redesign it and nothing in the file says so. Every layout must name a
    figure the document states about itself, and the extracted sum is checked
    against it -- so a redesign becomes a refusal with a sentence, rather than
    a quietly wrong import.
    """
    from statements import pdf_layouts

    raw = (STATEMENTS / "card_bill_changed.pdf").read_bytes()
    with pytest.raises(pdf_layouts.LayoutMismatch) as raised:
        sniffing.sniff(raw)

    message = str(raised.value)
    assert "add up to" in message and "layout has probably changed" in message
    assert "nothing has been imported" in message


def test_the_checksum_is_not_optional():
    """A layout with no figure to check itself against cannot be added."""
    from statements import pdf_layouts

    for layout in pdf_layouts.LAYOUTS:
        source = layout.__doc__ or ""
        assert source, f"{layout.__name__} needs a docstring saying what it recognises"
    # Extracted has no default for the stated figure, so a layout cannot be
    # written that forgets to provide one.
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(pdf_layouts.Extracted)}
    for required in ("stated_total", "stated_as", "extracted_total"):
        assert fields[required].default is dataclasses.MISSING, (
            f"{required} must be required, or a layout could skip its own check"
        )


# --------------------------------------------------------------------------- #
# What is kept from an import, for later
# --------------------------------------------------------------------------- #


def test_ofx_keeps_every_field_the_bank_sent():
    """A transaction outlives the statement it arrived on.

    `REFNUM` is on 229 of the 634 real transactions here and `MEMO` on 584, and
    both were being dropped. They are how a row is found again at the bank's
    end, and what explains it once the file is gone.
    """
    _, rows = read("bank_sgml.ofx")
    first = good(rows)[0]
    assert first.details["trntype"] == "CREDIT"
    assert first.details["fitid"] == "202509080001"
    # What already has a column of its own is not duplicated into the details.
    assert "dtposted" not in first.details
    assert "trnamt" not in first.details


def test_a_tabular_import_keeps_every_column_the_bank_wrote():
    """The sniffer uses four columns. The rest are why a row can be explained."""
    _, rows = read("preamble_swedish.csv")
    first = good(rows)[0]
    assert first.details["Kontonummer"] == "8921964985"
    assert first.details["Valuta"] == "SEK"
    assert first.details["Produkt"] == "Privatkonto"
    # Empty cells are not kept: they say nothing and crowd out what does.
    assert all(value.strip() for value in first.details.values())


def test_a_pending_hold_is_not_imported():
    """OFX says HOLD is "only valid in pending transactions".

    It is money the bank has earmarked, not money that moved -- and it settles
    later as its own transaction with its own FITID, so importing the hold now
    doubles the spend. The same thing REVERTED means in a Revolut CSV.
    """
    from statements import ofx

    held = (
        b"OFXHEADER:100\n\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>GBP</CURDEF>"
        b"<BANKTRANLIST><STMTTRN><TRNTYPE>HOLD</TRNTYPE><DTPOSTED>20260101</DTPOSTED>"
        b"<TRNAMT>-50.00</TRNAMT><FITID>H1</FITID><NAME>PETROL STATION</NAME></STMTTRN>"
        b"<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260103</DTPOSTED>"
        b"<TRNAMT>-48.20</TRNAMT><FITID>S1</FITID><NAME>PETROL STATION</NAME></STMTTRN>"
        b"</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    )
    statement = ofx.read(held)
    assert statement.transactions[0].problem is not None
    assert "pending hold" in statement.transactions[0].problem
    assert statement.transactions[1].problem is None, "the settled one must still import"


def test_a_cancelled_transaction_is_not_imported_and_a_replacement_is_flagged():
    """CORRECTACTION is how a bank amends something it already sent."""
    from statements import ofx

    def bill(action: str) -> bytes:
        return (
            "OFXHEADER:100\n\n<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>GBP</CURDEF>"
            "<BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260101</DTPOSTED>"
            "<TRNAMT>-10.00</TRNAMT><FITID>NEW1</FITID><CORRECTFITID>OLD1</CORRECTFITID>"
            f"<CORRECTACTION>{action}</CORRECTACTION><NAME>A SHOP</NAME></STMTTRN>"
            "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
        ).encode()

    deleted = ofx.read(bill("DELETE"))
    assert "cancel an earlier transaction" in deleted.transactions[0].problem
    assert "OLD1" in deleted.transactions[0].problem

    replaced = ofx.read(bill("REPLACE"))
    assert replaced.transactions[0].problem is None, "a replacement is a real transaction"
    assert any("replaces an earlier one" in w for w in replaced.warnings)
    assert any("OLD1" in w for w in replaced.warnings)


def test_a_reference_number_is_not_treated_as_an_overflowed_amount():
    """A description ending in a document number must not push words left.

    Found by running a real Itau current-account statement through the CLI. Its
    rows read like "EXAMPLE SHOP-XX 10000001" -- description, then the bank's
    own document number. `_reclaim_overflow` saw a trailing run of digits,
    called it an amount that a long payee had spilled past, and pushed
    "EXAMPLE SHOP-XX" into the cell on its left. That cell was the *date*,
    which then read "01/01/2026 EXAMPLE SHOP-XX", failed to parse, and took the
    whole transaction with it. Three rows gone without a word on screen.

    The repair now needs the survivor to be written like money.
    """
    from statements.pdf_statement import _reclaim_overflow

    # The bug: a bare integer ending a description.
    assert _reclaim_overflow(["01/01/2026", "EXAMPLE SHOP-XX 10000001", "-49,00", ""]) == [
        "01/01/2026",
        "EXAMPLE SHOP-XX 10000001",
        "-49,00",
        "",
    ]

    # Still repaired: a payee whose last word really did land in the money cell.
    assert _reclaim_overflow(["02/09/2026", "PAG BOLETO PLANO DE", "APOSENTADORIA -930,00"]) == [
        "02/09/2026",
        "PAG BOLETO PLANO DE APOSENTADORIA",
        "-930,00",
    ]


@pytest.mark.parametrize(
    ("word", "is_money"),
    [
        ("-49,00", True),      # signed
        ("+1.200,00", True),   # signed the other way
        ("(49,00)", True),     # bracketed, as accountants write a debit
        ("930,00", True),      # a decimal fraction
        ("1.234.56", True),    # dot decimals
        ("8.810,07", True),
        ("10000001", False),   # a document number
        ("10000001002", False),
        ("2026", False),       # a year
        ("1.234", False),      # thousands, no fraction -- ambiguous, so no
    ],
)
def test_money_is_told_from_digits_by_how_it_is_written(word: str, is_money: bool):
    """The distinction the overflow repair now turns on."""
    from statements.pdf_statement import _looks_like_money

    assert _looks_like_money(word) is is_money


# --------------------------------------------------------------------------- #
# Verdicts about the whole file come from the whole file (issue #67)
# --------------------------------------------------------------------------- #


def test_the_sign_verdict_reads_past_the_first_forty_rows():
    """A Revolut account statement opens with a page of savings deposits.

    Every one of them is positive, and a sniffer that looked at forty rows told
    the user the file had no money out in it -- then read eighty-four negative
    rows correctly. A false warning on the first real file is how people learn
    to ignore the one warning that protects a year of spending.
    """
    sniffed, rows = read("mixed_products.csv")
    first_forty = [r.amount for r in good(rows)[:40]]
    assert all(amount >= 0 for amount in first_forty), "the fixture must keep its trap"
    assert any(r.amount < 0 for r in good(rows))
    assert not [w for w in sniffed.warnings if "positive" in w], sniffed.warnings


def test_a_file_that_really_is_all_positive_still_warns():
    body = "Date,Description,Amount\n" + "".join(
        f"2026-01-{1 + n % 28:02d},Shop {n},{n + 1}.00\n" for n in range(60)
    )
    sniffed = sniffing.sniff(body.encode())
    assert [w for w in sniffed.warnings if "positive" in w]


def test_a_date_with_a_time_is_a_format_not_a_failure():
    """"2025-12-24 11:58:09" used to be read, then reported as unreadable."""
    sniffed, rows = read("mixed_products.csv")
    assert sniffed.format.date_format == "%Y-%m-%d %H:%M:%S"
    assert not [w for w in sniffed.warnings if "date format" in w], sniffed.warnings
    assert good(rows)[0].when == date(2026, 1, 1)
