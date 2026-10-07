"""Which way the money went, however the bank wrote the sign (#261, #262).

A debit written ``12.50-`` was read as zero and skipped as "moves no money";
one written with a real minus sign, ``−12.50``, lost the sign and imported as
money in. Both silently. These assert the staged minor-unit amount and the
line's outcome, in both decimal conventions and in two currencies, because
"the row imported" is exactly what both bugs also did.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.audit.batch import batch
from app.models import BatchKind, BatchStatus, ImportOutcome
from app.services import importing
from statements import parsing, pdf_statement, signs, sniffing

MINUS = "\u2212"

# --------------------------------------------------------------------------- #
# Reading one amount
# --------------------------------------------------------------------------- #


def test_the_minus_table_is_the_six_characters_the_issue_names():
    """Pinned, because #268 copies this table and a test will hold them equal."""
    assert set(signs.MINUS_SIGNS) == {"\u2212", "\u2012", "\u2013", "\u2014", "\ufe63", "\uff0d"}


@pytest.mark.parametrize("minus", sorted(signs.MINUS_SIGNS))
def test_every_minus_like_character_is_a_minus(minus):
    assert sniffing.parse_amount(f"{minus}12.50") == Decimal("-12.50")
    assert sniffing.parse_amount(f"{minus}1.234,56", decimal_separator=",") == Decimal("-1234.56")
    assert sniffing.parse_amount(f"12.50{minus}") == Decimal("-12.50")


@pytest.mark.parametrize(
    ("text", "separator", "expected"),
    [
        ("12.50-", ".", "-12.50"),
        ("1,234.56-", ".", "-1234.56"),
        ("1.234,56-", ",", "-1234.56"),
        ("12,50-", ",", "-12.50"),
        ("12.50 -", ".", "-12.50"),
        ("€ 12,50-", ",", "-12.50"),
        ("12.50+", ".", "12.50"),
        ("-12.50", ".", "-12.50"),
        ("(12.50)", ".", "-12.50"),
        # Placeholders for "nothing", as an empty cell is.
        ("", ".", "0"),
        ("-", ".", "0"),
        ("\u2014", ".", "0"),
        ("0.00", ".", "0.00"),
    ],
)
def test_a_sign_at_either_end_is_read(text, separator, expected):
    assert str(sniffing.parse_amount(text, decimal_separator=separator)) == expected


@pytest.mark.parametrize(
    ("text", "separator", "reason"),
    [
        ("-12.50-", ".", "signed twice"),
        (f"{MINUS}1.234,56-", ",", "signed twice"),
        ("+12.50-", ".", "signed twice"),
        ("(-12.50)", ".", "signed twice"),
        ("n/a", ".", "could not read 'n/a' as an amount"),
        ("--12.50", ".", "could not read"),
        ("12-50", ".", "could not read"),
        ("SHOP-XX -930,00", ",", "could not read"),
    ],
)
def test_an_amount_that_cannot_be_read_says_so_rather_than_reading_zero(text, separator, reason):
    with pytest.raises(ValueError, match=reason):
        sniffing.parse_amount(text, decimal_separator=separator)


@pytest.mark.parametrize(
    ("text", "separator", "expected"),
    [
        ("12.50 DR", ".", "-12.50"),
        ("12.50DR", ".", "-12.50"),
        ("12.50 dr", ".", "-12.50"),
        ("12,50 DR", ",", "-12.50"),
        ("1.234,56 DR", ",", "-1234.56"),
        ("€ 1,234.56 DR.", ".", "-1234.56"),
        ("12.50 CR", ".", "12.50"),
        ("1.234,56 Cr", ",", "1234.56"),
        (f"12.50{chr(0xa0)}DR", ".", "-12.50"),
    ],
)
def test_a_debit_or_credit_written_after_the_figure_is_its_sign(text, separator, expected):
    """#84: the letters were stripped as decoration, so a debit came in as money in."""
    assert str(sniffing.parse_amount(text, decimal_separator=separator)) == expected


@pytest.mark.parametrize("text", ["-12.50 DR", "12.50- CR", "(12.50) DR", f"{MINUS}12.50 CR", "+12.50 DR"])
def test_a_sign_and_dr_or_cr_together_is_signed_twice(text):
    with pytest.raises(ValueError, match="signed twice"):
        sniffing.parse_amount(text)


def test_dr_and_cr_are_signs_only_after_a_figure():
    """A word that ends in the letters is not a marker, and is not a number either."""
    assert signs.debit_credit("ACCR") == ("ACCR", None)
    assert signs.debit_credit("12.50 DR") == ("12.50", "-")
    with pytest.raises(ValueError, match="could not read"):
        sniffing.parse_amount("DR")
    assert signs.written_negative("12.50 DR")
    assert not signs.written_negative("12.50 CR")


def test_dr_does_not_hide_the_decimal_comma():
    """"12,500 DR" has a three-digit tail, not the six characters "500 DR"."""
    assert sniffing.guess_decimal_separator(["12,500 DR", "1.500,00 DR"]) == ","
    assert sniffing.guess_decimal_separator(["12,50 DR"]) == ","
    assert sniffing._settle(["12,500 DR"]) == (None, "12,500 DR")


def test_a_trailing_sign_does_not_hide_the_decimal_comma():
    """``12,50-`` has a two-digit fraction; read as "50-" it was a thousands group."""
    assert sniffing.guess_decimal_separator(["12,50-"]) == ","
    assert sniffing.guess_decimal_separator([f"{MINUS}12,50"]) == ","
    assert sniffing.guess_decimal_separator(["12.50-"]) == "."


# --------------------------------------------------------------------------- #
# Staged, in both conventions and both currencies
# --------------------------------------------------------------------------- #

# Dot decimals, comma-delimited, for the GBP account.
DOT_DECIMALS = (
    "Date,Description,Amount\n"
    "2026-01-15,EXAMPLE GROCER,12.50-\n"
    f"2026-01-16,EXAMPLE CAFE,{MINUS}3.20\n"
    "2026-01-17,EXAMPLE BOOKS,\u20137.00\n"
    "2026-01-18,EXAMPLE PAYROLL,2100.00\n"
    "2026-01-19,EXAMPLE TWICE,-4.00-\n"
    "2026-01-20,EXAMPLE SMUDGE,n/a\n"
).encode()

# Comma decimals, semicolon-delimited, for the EUR account.
COMMA_DECIMALS = (
    "Fecha;Concepto;Importe\n"
    "15/01/2026;EXAMPLE GROCER;1.234,56-\n"
    f"16/01/2026;EXAMPLE CAFE;{MINUS}45,20\n"
    "17/01/2026;EXAMPLE BOOKS;\uff0d7,00\n"
    "18/01/2026;EXAMPLE PAYROLL;2.100,00\n"
    "19/01/2026;EXAMPLE TWICE;-1,00-\n"
    "20/01/2026;EXAMPLE SMUDGE;abc\n"
).encode()


def _stage(session, owner, household, account, raw: bytes):
    sniffed = sniffing.sniff(raw)
    with batch(
        session,
        kind=BatchKind.imported,
        actor_id=owner.id,
        household_id=household.id,
        source={
            "filename": "statement.csv",
            "sha256": importing.file_digest(raw),
            "bytes": len(raw),
            "account_id": account.id,
            "format": sniffed.format.describe(),
        },
    ) as staged:
        lines = importing.stage_file(
            session, account=account, raw=raw, fmt=sniffed.format, batch_row=staged
        )
        staged.status = BatchStatus.preview
    return staged, {line.line_no: line for line in lines}


@pytest.mark.parametrize(
    ("raw", "account_key", "amounts", "column"),
    [
        (DOT_DECIMALS, "pounds", {2: -1_250, 3: -320, 4: -700, 5: 210_000}, "Amount"),
        (COMMA_DECIMALS, "checking", {2: -123_456, 3: -4_520, 4: -700, 5: 210_000}, "Importe"),
    ],
    ids=["dot-decimals-gbp", "comma-decimals-eur"],
)
def test_signed_rows_stage_with_their_sign_and_unreadable_ones_are_problems(
    session, owner, household, accounts, raw, account_key, amounts, column
):
    account = accounts[account_key]
    staged, lines = _stage(session, owner, household, account, raw)

    assert {n: lines[n].parsed["amount"] for n in amounts} == amounts
    assert {lines[n].outcome for n in amounts} == {ImportOutcome.created}

    twice, smudged = lines[6], lines[7]
    assert twice.outcome is ImportOutcome.rejected
    assert "signed twice" in twice.reason
    assert twice.parsed is None
    assert smudged.outcome is ImportOutcome.rejected
    assert "as an amount" in smudged.reason and f"in the {column} column" in smudged.reason

    assert not any(line.reason == "this line moves no money" for line in lines.values()), (
        "a debit written 12.50- is not a line that moves no money"
    )

    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id):
        result = importing.commit(session, batch_row=staged, account=account)
        staged.status = BatchStatus.applied
    assert result["created"] == 4

    from app.services import accounts as account_service

    assert account_service.balances(session, account.id)["balance"] == sum(amounts.values())
    other = accounts["checking" if account_key == "pounds" else "pounds"]
    assert account_service.balances(session, other.id)["balance"] == 0


def test_a_file_signed_only_with_minus_signs_is_not_called_all_positive():
    """The whole-file warning looked for ASCII ``-`` at the front only."""
    for raw in (DOT_DECIMALS, COMMA_DECIMALS):
        warnings = sniffing.sniff(raw).warnings
        assert not any("every amount in this file is positive" in w for w in warnings)
    trailing_only = b"Date,Description,Amount\n2026-01-15,EXAMPLE GROCER,12.50-\n"
    assert not any("positive" in w for w in sniffing.sniff(trailing_only).warnings)


# Ledger-style: every amount unsigned, the direction said in letters (#84).
DEBIT_CREDIT = {
    "dot": (
        b"Date,Description,Amount\n"
        b"2026-01-15,EXAMPLE GROCER,12.50 DR\n"
        b"2026-01-16,EXAMPLE PAYROLL,2100.00 CR\n"
        b"2026-01-17,EXAMPLE CAFE,3.20DR\n"
        b"2026-01-18,EXAMPLE TWICE,-4.00 DR\n"
    ),
    "comma": (
        b"Fecha;Concepto;Importe\n"
        b"15/01/2026;EXAMPLE GROCER;1.234,56 DR\n"
        b"16/01/2026;EXAMPLE PAYROLL;2.100,00 CR\n"
        b"17/01/2026;EXAMPLE CAFE;45,20 dr\n"
        b"18/01/2026;EXAMPLE TWICE;(1,00) DR\n"
    ),
}


@pytest.mark.parametrize(
    ("convention", "account_key", "amounts"),
    [
        ("dot", "pounds", {2: -1_250, 3: 210_000, 4: -320}),
        ("comma", "checking", {2: -123_456, 3: 210_000, 4: -4_520}),
    ],
)
def test_dr_rows_stage_as_money_out_and_cr_as_money_in(
    session, owner, household, accounts, convention, account_key, amounts
):
    raw = DEBIT_CREDIT[convention]
    assert not any("every amount in this file is positive" in w for w in sniffing.sniff(raw).warnings)

    account = accounts[account_key]
    staged, lines = _stage(session, owner, household, account, raw)
    assert {n: lines[n].parsed["amount"] for n in amounts} == amounts
    assert lines[5].outcome is ImportOutcome.rejected and "signed twice" in lines[5].reason

    with batch(session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id):
        result = importing.commit(session, batch_row=staged, account=account)
        staged.status = BatchStatus.applied
    assert result["created"] == 3

    from app.services import accounts as account_service

    assert account_service.balances(session, account.id)["balance"] == sum(amounts.values())
    other = accounts["checking" if account_key == "pounds" else "pounds"]
    assert account_service.balances(session, other.id)["balance"] == 0


FEE_AND_BALANCE = (
    "Date,Description,Outflow,Inflow,Fee,Balance\n"
    f"2026-01-15,EXAMPLE GROCER,{MINUS}12.50,,0.30,100.00\n"
    "2026-01-16,EXAMPLE CAFE,3.20-,,n/a,96.80\n"
    "2026-01-17,EXAMPLE KIOSK,3.10,,,n/a\n"
    "2026-01-18,EXAMPLE SMUDGE,x,,,90.00\n"
).encode()


def test_an_unreadable_fee_is_a_problem_and_an_unreadable_balance_is_no_balance():
    """A fee moves money, so a fee that cannot be read cannot be guessed at.

    A running balance moves none: an unreadable one costs the row its balance,
    not the row -- the debit beside it is real -- and the file says so.
    """
    sniffed, rows = parsing.read(FEE_AND_BALANCE)
    assert sniffed.format.fee_column == "Fee"
    assert sniffed.format.balance_column == "Balance"

    good, fee, kiosk, outflow = rows
    assert (good.amount, good.fee, good.balance) == (Decimal("-12.50"), Decimal("0.30"), Decimal("100.00"))
    assert good.problem is None
    assert fee.amount is None and "in the Fee column" in fee.problem
    assert (kiosk.amount, kiosk.balance, kiosk.problem) == (Decimal("-3.10"), None, None)
    assert outflow.amount is None and "in the Outflow column" in outflow.problem

    said = [w for w in sniffed.warnings if "Balance column" in w]
    assert len(said) == 1
    assert "'n/a'" in said[0] and "on 1 row " in said[0] and "without a running balance" in said[0]


def test_a_debit_beside_an_unreadable_balance_stages(session, owner, household, accounts):
    _, lines = _stage(session, owner, household, accounts["pounds"], FEE_AND_BALANCE)

    kiosk = lines[4]
    assert kiosk.outcome is ImportOutcome.created
    assert kiosk.parsed["amount"] == -310
    assert lines[3].outcome is ImportOutcome.rejected and "in the Fee column" in lines[3].reason
    assert lines[5].outcome is ImportOutcome.rejected and "in the Outflow column" in lines[5].reason


def test_a_file_with_readable_balances_has_no_balance_warning():
    raw = b"Date,Description,Amount,Balance\n2026-01-15,EXAMPLE GROCER,12.50-,87.50\n"
    assert not any("Balance column" in w for w in sniffing.sniff(raw).warnings)


# --------------------------------------------------------------------------- #
# The PDF reader
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "word", [f"{MINUS}930,00", "930,00-", "\u2013930,00", "\uff0d49,00", "930,00DR", "49,00CR"]
)
def test_a_pdf_word_signed_either_way_is_money(word):
    """Not seen as a number, it is placed by its left edge, like text."""
    assert pdf_statement._numeric(word)
    assert pdf_statement._looks_like_money(word)


@pytest.mark.parametrize(
    "word", ["--12--", "+12.50-", "-12.50-", "--12", "12--", "-", "+-", "-12.50DR", "DR"]
)
def test_a_pdf_word_signed_more_than_once_is_not_a_number(word):
    """At most one sign, at one end -- what `parse_amount` accepts, too."""
    assert not pdf_statement._numeric(word)
    assert not pdf_statement._looks_like_money(word)


def _tiny_pdf(rows: list[list[tuple[int, str]]]) -> bytes:
    """A one-page PDF with words at given x positions, one row per line.

    Helvetica with byte 0x81 mapped to the glyph ``minus``, which a PDF's text
    layer gives back as U+2212 -- the character a typeset statement writes.
    """
    ops = ["BT", "/F1 10 Tf"]
    for index, row in enumerate(rows):
        for x, text in row:
            data = text.replace(MINUS, "\x81")
            ops += [f"1 0 0 1 {x} {750 - 14 * index} Tm", f"({data}) Tj"]
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding "
        b"<< /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [129 /minus] >> >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_a_pdf_with_a_real_minus_sign_imports_a_debit(session, owner, household, accounts):
    raw = _tiny_pdf(
        [
            [(50, "Date"), (150, "Description"), (400, "Amount")],
            [(50, "15/01/2026"), (150, "EXAMPLE GROCER"), (400, f"{MINUS}12,50")],
            [(50, "16/01/2026"), (150, "EXAMPLE PAYROLL"), (400, "2.100,00")],
            [(50, "17/01/2026"), (150, "EXAMPLE CAFE"), (400, "3,20-")],
        ]
    )
    sniffed, rows = parsing.read(raw)
    assert MINUS in rows[0].raw, "the text layer gave back a real minus sign"
    assert [r.amount for r in rows] == [Decimal("-12.50"), Decimal("2100.00"), Decimal("-3.20")]

    _, lines = _stage(session, owner, household, accounts["checking"], raw)
    assert [lines[n].parsed["amount"] for n in (2, 3, 4)] == [-1_250, 210_000, -320]
    assert {line.outcome for line in lines.values()} == {ImportOutcome.created}


def test_a_pdf_with_dr_and_cr_after_the_figure_imports_their_direction():
    """#84, in a PDF: "DR" is its own word, and lands in the amount cell beside the figure."""
    raw = _tiny_pdf(
        [
            [(50, "Date"), (150, "Description"), (400, "Amount")],
            [(50, "15/01/2026"), (150, "EXAMPLE GROCER"), (400, "12,50"), (430, "DR")],
            [(50, "16/01/2026"), (150, "EXAMPLE PAYROLL"), (400, "2.100,00"), (440, "CR")],
            [(50, "17/01/2026"), (150, "EXAMPLE CAFE"), (400, "3,20DR")],
        ]
    )
    _, rows = parsing.read(raw)
    assert [r.amount for r in rows] == [Decimal("-12.50"), Decimal("2100.00"), Decimal("-3.20")]
