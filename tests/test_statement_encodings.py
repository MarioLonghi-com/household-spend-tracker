"""Which encoding a statement file is read in, and what that does to its text.

Two exports that read wrong without a word of warning (issues #258 and #263):

- A **Windows-1252** file was decoded as ISO-8859-1, because Latin-1 decodes
  every byte and cp1252 was tried after it. The bytes cp1252 spends on `€`,
  curly quotes, the en dash and the ellipsis arrived as C1 control characters
  in the payee, where payee rules and folding no longer matched them.
- A **UTF-16** file was decoded as Latin-1 too, with a NUL between every
  character, so no header, no date column and no amount column were found --
  and the warning blamed the header row.

Both fixtures are synthetic. The assertions are on the decoded payee text and
the staged amount in minor units, because the encoding name alone can be right
while the ledger is wrong.
"""

from __future__ import annotations

import codecs
from datetime import date
from pathlib import Path

import pytest

from app.audit.batch import batch
from app.models import BatchKind, BatchStatus, ImportOutcome
from app.services import importing
from statements import UnreadableStatement, ofx, parsing, sniffing

STATEMENTS = Path(__file__).resolve().parent / "statement_files"
CP1252 = (STATEMENTS / "windows_1252_curly_quotes.csv").read_bytes()
UTF16_LE = (STATEMENTS / "utf16_le_bom.csv").read_bytes()

CP1252_PAYEES = [
    "Café “Sol” – Plaza Mayor",
    "Librería ‘El Faro’…",
    "Devolución “Mercado Norte”",
]
UTF16_PAYEES = [
    "Grey Wharf Café",
    "Bakery “North Lane”",
    "Refund — Hilltop Books",
]


def _stage(session, owner, household, account, raw: bytes, name: str):
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
    return sniffed, lines


# --------------------------------------------------------------------------- #
# The fixtures are what they claim to be
# --------------------------------------------------------------------------- #


def test_the_fixtures_carry_the_bytes_that_broke_the_reader():
    """Without these the tests below would pass against the old order too."""
    assert any(0x80 <= byte <= 0x9F for byte in CP1252), "no cp1252-only byte in the fixture"
    assert UTF16_LE.startswith(codecs.BOM_UTF16_LE)
    assert b"\x00" in UTF16_LE


# --------------------------------------------------------------------------- #
# Windows-1252 (#258)
# --------------------------------------------------------------------------- #


def test_a_windows_1252_export_keeps_its_euro_sign_and_curly_quotes():
    text, encoding = sniffing.decode(CP1252)
    assert encoding == "cp1252"
    assert "-3,50 €" in text
    assert not any("\x80" <= char <= "\x9f" for char in text), "a C1 control character survived"

    sniffed = sniffing.sniff(CP1252)
    assert sniffed.format.payee_column == "Concepto"
    assert sniffed.format.amount_column == "Importe"
    assert [r["Concepto"] for r in sniffed.sample_rows] == CP1252_PAYEES

    rows = parsing.parse(CP1252, sniffed.format)
    assert [r.problem for r in rows] == [None, None, None]
    assert [r.payee for r in rows] == CP1252_PAYEES
    assert [r.when for r in rows] == [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 9)]


def test_a_byte_cp1252_leaves_undefined_does_not_cost_the_file_its_euro_signs():
    """cp1252 leaves five bytes undefined, and one of them used to send the
    whole file back to Latin-1 -- every `€` with it, and no warning.

    The case the review found: a cp1252 export with one line of UTF-8 in it.
    "Łódź" in UTF-8 is C5 81 C3 B3 64 C5 BA, and 0x81 is undefined in cp1252.
    Now that byte alone is read as Latin-1 reads it, the euro signs survive,
    and the preview says something is wrong.
    """
    raw = CP1252 + "12/01/2026;Hostal Łódź;-45,00 €\r\n".encode()  # UTF-8
    text, encoding = sniffing.decode(raw)
    assert encoding == "cp1252"
    assert "-3,50 €" in text
    assert "Café “Sol” – Plaza Mayor" in text
    assert "\x80" not in text
    assert "Hostal Å\x81Ã³dÅº" in text

    sniffed = sniffing.sniff(raw)
    assert sniffing.C1_CONTROLS in sniffed.warnings
    rows = parsing.parse(raw, sniffed.format)
    assert [r.payee for r in rows][:3] == CP1252_PAYEES
    assert [str(r.amount) for r in rows] == ["-3.50", "-12.40", "20.00", "-45.00"]


def test_a_clean_file_has_no_control_code_warning():
    assert sniffing.C1_CONTROLS not in sniffing.sniff(CP1252).warnings
    assert sniffing.C1_CONTROLS not in sniffing.sniff(UTF16_LE).warnings


def test_a_plain_latin_1_file_reads_the_same_under_cp1252():
    """With no byte in 0x80-0x9F the two agree on every character."""
    raw = "Fecha;Concepto;Importe\n05/01/2026;NÓMINA Ærø;1.000,00\n".encode("iso-8859-1")
    text, encoding = sniffing.decode(raw)
    assert encoding == "cp1252"
    assert text == raw.decode("iso-8859-1")


# --------------------------------------------------------------------------- #
# UTF-16 (#263)
# --------------------------------------------------------------------------- #


def test_a_utf_16_le_export_with_a_byte_order_mark_is_read():
    text, encoding = sniffing.decode(UTF16_LE)
    assert encoding == "utf-16"
    assert "\x00" not in text
    assert not text.startswith("﻿"), "the byte-order mark is not part of the header"
    assert text.splitlines()[0] == "Date,Description,Amount"

    sniffed = sniffing.sniff(UTF16_LE)
    assert sniffed.format.date_column == "Date"
    assert sniffed.format.payee_column == "Description"
    assert sniffed.format.amount_column == "Amount"
    assert sniffed.warnings == []

    rows = parsing.parse(UTF16_LE, sniffed.format)
    assert [r.problem for r in rows] == [None, None, None]
    assert [r.payee for r in rows] == UTF16_PAYEES
    assert [r.when for r in rows] == [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 9)]


def test_the_issues_own_reproduction_reads_utf_16():
    raw = "Date,Description,Amount\n2026-01-02,Shop,-3.50\n".encode("utf-16")
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.date_column == "Date"
    assert sniffed.format.amount_column == "Amount"
    rows = parsing.parse(raw, sniffed.format)
    assert [(r.payee, str(r.amount)) for r in rows] == [("Shop", "-3.50")]


def test_a_utf_16_be_export_with_a_byte_order_mark_is_read():
    body = "Date,Description,Amount\n2026-01-02,Bakery “North Lane”,-12.40\n"
    raw = codecs.BOM_UTF16_BE + body.encode("utf-16-be")
    text, encoding = sniffing.decode(raw)
    assert encoding == "utf-16"
    assert text == body
    sniffed = sniffing.sniff(raw)
    rows = parsing.parse(raw, sniffed.format)
    assert [(r.payee, str(r.amount)) for r in rows] == [("Bakery “North Lane”", "-12.40")]


@pytest.mark.parametrize(
    "bom, codec", [(codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be")]
)
def test_a_utf_32_byte_order_mark_is_not_mistaken_for_utf_16(bom: bytes, codec: str):
    """UTF-32 LE opens with FF FE 00 00 -- the UTF-16 LE mark and then a NUL.

    Checked first, or it would decode as UTF-16 with a NUL in every other
    character.
    """
    body = "Date,Description,Amount\n2026-01-02,Grey Wharf Café,-3.50\n"
    text, encoding = sniffing.decode(bom + body.encode(codec))
    assert encoding == "utf-32"
    assert text == body


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
def test_utf_16_or_32_without_a_byte_order_mark_is_refused_with_a_reason(codec: str):
    """Refused, not guessed. See `decode` for why refusing is the safer choice.

    UTF-32 needed its own look: ASCII in it is valid UTF-8, so it decoded
    "cleanly" with three NULs per letter and the warning blamed the header row.
    """
    raw = "Date,Description,Amount\n2026-01-02,Café,-3.50\n".encode(codec)
    with pytest.raises(UnreadableStatement, match="UTF-16 or UTF-32 text without a byte-order mark"):
        sniffing.decode(raw)
    with pytest.raises(UnreadableStatement, match="UTF-16 or UTF-32 text without a byte-order mark"):
        sniffing.sniff(raw)


def test_a_stray_nul_in_an_ordinary_file_is_not_called_utf_16():
    """One NUL is a damaged byte, not a pattern."""
    raw = b"Date,Description,Amount\n2026-01-02,Shop\x00,-3.50\n"
    text, encoding = sniffing.decode(raw)
    assert encoding == "utf-8-sig"  # the first of the strict list, which reads plain UTF-8 too
    assert text.startswith("Date,Description,Amount")


def test_a_truncated_utf_16_file_is_flagged_not_read_as_latin_1():
    """An odd byte at the end cannot be strict UTF-16. The rest still is, so it
    is read with a replacement character and the preview warns, rather than
    falling through to Latin-1 and losing every column."""
    raw = UTF16_LE + b"\x00"
    text, encoding = sniffing.decode(raw)
    assert encoding == "utf-16 (with unreadable characters)"
    assert text.splitlines()[0] == "Date,Description,Amount"
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.amount_column == "Amount"
    assert any("could not be decoded" in w for w in sniffed.warnings)


# --------------------------------------------------------------------------- #
# OFX in UTF-16
# --------------------------------------------------------------------------- #

UTF16_OFX_BODY = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
CHARSET:NONE

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS>
<CURDEF>GBP</CURDEF>
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260102</DTPOSTED><TRNAMT>-3.50</TRNAMT>
<FITID>A1</FITID><NAME>Grey Wharf Café</NAME></STMTTRN>
<STMTTRN><TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20260109</DTPOSTED><TRNAMT>20.00</TRNAMT>
<FITID>A2</FITID><NAME>Refund — Hilltop Books</NAME></STMTTRN>
</BANKTRANLIST>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-be-bom"])
def test_a_utf_16_ofx_file_is_read_as_ofx_not_as_a_table(codec: str):
    """The OFX check looked at raw bytes, where `<OFX>` has a NUL inside it."""
    if codec == "utf-16-be-bom":
        raw = codecs.BOM_UTF16_BE + UTF16_OFX_BODY.encode("utf-16-be")
    else:
        raw = UTF16_OFX_BODY.encode("utf-16")
    assert ofx.looks_like_ofx(raw)
    sniffed = sniffing.sniff(raw)
    assert sniffed.format.kind == "ofx"
    rows = parsing.parse(raw, sniffed.format)
    assert [(r.payee, str(r.amount), r.fitid) for r in rows] == [
        ("Grey Wharf Café", "-3.50", "A1"),
        ("Refund — Hilltop Books", "20.00", "A2"),
    ]
    assert ofx.read(raw).currency == "GBP"


# --------------------------------------------------------------------------- #
# What reaches the staged import
# --------------------------------------------------------------------------- #


def test_both_files_stage_their_payees_and_minor_unit_amounts(session, owner, household, accounts):
    """Two files, two encodings, two accounts in two currencies.

    The cp1252 file goes to the euro account and the UTF-16 file to the pound
    one, so a mix-up between them shows as a payee or an amount in the wrong
    place, not as a pass.
    """
    _, euro_lines = _stage(
        session, owner, household, accounts["checking"], CP1252, "windows_1252_curly_quotes.csv"
    )
    _, pound_lines = _stage(session, owner, household, accounts["pounds"], UTF16_LE, "utf16_le_bom.csv")

    assert [line.outcome for line in euro_lines] == [ImportOutcome.created] * 3
    assert [line.outcome for line in pound_lines] == [ImportOutcome.created] * 3
    assert [(line.parsed["payee"], line.parsed["amount"]) for line in euro_lines] == [
        ("Café “Sol” – Plaza Mayor", -350),
        ("Librería ‘El Faro’…", -1240),
        ("Devolución “Mercado Norte”", 2000),
    ]
    assert [(line.parsed["payee"], line.parsed["amount"]) for line in pound_lines] == [
        ("Grey Wharf Café", -350),
        ("Bakery “North Lane”", -1240),
        ("Refund — Hilltop Books", 2000),
    ]
