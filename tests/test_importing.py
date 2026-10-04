"""Statement import: sniffing, preview, dedupe and absorption.

These are written as the scenarios a monthly import actually produces -- a
Spanish bank export with nothing typed, the same file twice, an extended
statement, a hand-entered twin.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.models import (
    Batch,
    BatchKind,
    BatchStatus,
    ClearedState,
    ImportLine,
    ImportOutcome,
    MatchType,
    RuleAction,
    Transaction,
)
from app.services import importing
from app.services import payees as payee_service
from app.services import transactions as txn_service
from statements import sniffing

# A realistic Santander export: semicolon, dd/mm/yyyy, comma decimals, a dotted
# thousands separator, and a running balance column we do not want.
SANTANDER = (
    b"Fecha;Concepto;Importe;Saldo\r\n"
    b"05/01/2026;MERCADONA 1234;-45,20;8.810,07\r\n"
    b"07/01/2026;NOMINA ENERO;2.100,00;10.910,07\r\n"
    b"09/01/2026;SQ *EL BAR;-12,50;10.897,57\r\n"
)

TWO_COLUMN = (
    b"Date,Description,Outflow,Inflow\n"
    b"2026-01-05,Coffee,4.20,\n"
    b"2026-01-06,Refund,,15.00\n"
)


@pytest.fixture()
def write(session, owner, household):
    def _open(kind=BatchKind.manual):
        return batch(session, kind=kind, actor_id=owner.id, household_id=household.id)

    return _open


def _stage(session, owner, household, account, raw: bytes, *, filename="statement.csv"):
    """Phase one, the way the route does it."""
    sniffed = sniffing.sniff(raw)
    digest = importing.file_digest(raw)
    with batch(
        session,
        kind=BatchKind.imported,
        actor_id=owner.id,
        household_id=household.id,
        source={
            "filename": filename,
            "sha256": digest,
            "bytes": len(raw),
            "account_id": account.id,
            "format": sniffed.format.describe(),
        },
    ) as staged:
        lines = importing.stage_file(
            session, account=account, raw=raw, fmt=sniffed.format, batch_row=staged,
        )
        staged.status = BatchStatus.preview
    return staged, lines


def _commit(session, owner, household, account, staged, **kwargs):
    with batch(
        session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id
    ):
        result = importing.commit(session, batch_row=staged, account=account, **kwargs)
        staged.status = BatchStatus.applied
    return result


# --------------------------------------------------------------------------- #
# Sniffing
# --------------------------------------------------------------------------- #


def test_a_spanish_export_reads_with_nothing_typed():
    sniffed = sniffing.sniff(SANTANDER)
    fmt = sniffed.format
    assert fmt.delimiter == ";"
    assert fmt.date_column == "Fecha"
    assert fmt.payee_column == "Concepto"
    assert fmt.amount_column == "Importe"
    assert fmt.date_format == "%d/%m/%Y"
    assert fmt.decimal_separator == ","
    assert sniffed.row_count == 3


def test_a_latin_1_file_is_decoded_rather_than_mangled():
    """The previous build hard-coded utf-8 with errors="replace", so an accented
    payee silently filled with replacement characters."""
    raw = "Fecha;Concepto;Importe\n05/01/2026;NÓMINA;1.000,00\n".encode("iso-8859-1")
    sniffed = sniffing.sniff(raw)
    # Read as cp1252, which agrees with Latin-1 on every byte outside
    # 0x80-0x9F and is tried first so that a Windows export's euro sign
    # survives (#258). The text below is what matters, not the label.
    assert sniffed.format.encoding == "cp1252"
    assert "�" not in sniffed.sample_rows[0]["Concepto"]
    assert sniffed.sample_rows[0]["Concepto"] == "NÓMINA"


def test_outflow_and_inflow_columns_become_one_signed_amount():
    sniffed = sniffing.sniff(TWO_COLUMN)
    assert sniffed.format.amount_column is None
    assert sniffed.format.outflow_column == "Outflow"
    assert sniffed.format.inflow_column == "Inflow"

    rows = importing.parse(TWO_COLUMN, sniffed.format)
    assert [str(r.amount) for r in rows] == ["-4.20", "15.00"]


def test_an_ambiguous_date_reads_day_first_but_evidence_overrules_it():
    day_first, _ = sniffing.guess_date_format(["03/04/2026", "05/04/2026"])
    assert day_first == "%d/%m/%Y", "this is a household in Spain"

    month_first, _ = sniffing.guess_date_format(["03/04/2026", "03/14/2026"])
    assert month_first == "%m/%d/%Y", "one unambiguous row settles the whole file"


def test_a_three_digit_tail_is_thousands_not_decimals():
    assert sniffing.guess_decimal_separator(["8.810"]) == "."
    assert sniffing.guess_decimal_separator(["8.810,07"]) == ","


@pytest.mark.parametrize(
    "text,separator,expected",
    [
        ("-45,20", ",", "-45.20"),
        ("1.234,56", ",", "1234.56"),
        ("(12.34)", ".", "-12.34"),
        ("€ 1 234.50", ".", "1234.50"),
        ("", ".", "0"),
    ],
)
def test_amounts_are_read_the_way_banks_write_them(text, separator, expected):
    assert str(sniffing.parse_amount(text, decimal_separator=separator)) == expected


# --------------------------------------------------------------------------- #
# Preview writes nothing
# --------------------------------------------------------------------------- #


def test_staging_classifies_every_line_and_touches_no_money(session, owner, household, accounts):
    checking = accounts["checking"]
    staged, lines = _stage(session, owner, household, checking, SANTANDER)

    assert staged.status is BatchStatus.preview
    assert len(lines) == 3
    assert {line.outcome for line in lines} == {ImportOutcome.created}
    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 0, (
        "a preview must not write to the register"
    )
    # Each line keeps its own text, so the file itself need not be retained.
    assert any("MERCADONA" in line.raw for line in lines)


def test_committing_writes_the_money(session, owner, household, accounts):
    checking = accounts["checking"]
    staged, _ = _stage(session, owner, household, checking, SANTANDER)
    result = _commit(session, owner, household, checking, staged)

    assert result["created"] == 3
    from app.services import accounts as account_service

    assert account_service.balances(session, checking.id)["balance"] == -4_520 + 210_000 - 1_250


def test_a_bad_row_is_one_rejected_line_not_a_lost_file(session, owner, household, accounts):
    """The previous build raised at parse time and lost the whole statement."""
    broken = (
        b"Fecha;Concepto;Importe\n"
        b"05/01/2026;GOOD ONE;-10,00\n"
        b"not-a-date;BROKEN;-20,00\n"
        b"07/01/2026;ANOTHER GOOD;-30,00\n"
    )
    staged, lines = _stage(session, owner, household, accounts["checking"], broken)

    outcomes = {line.outcome for line in lines}
    assert ImportOutcome.rejected in outcomes
    rejected = next(line for line in lines if line.outcome is ImportOutcome.rejected)
    assert "could not read" in rejected.reason

    result = _commit(session, owner, household, accounts["checking"], staged)
    assert result["created"] == 2, "the good rows still import"


# --------------------------------------------------------------------------- #
# Double imports
# --------------------------------------------------------------------------- #


def test_the_same_file_twice_is_caught_before_it_is_parsed(session, owner, household, accounts):
    checking = accounts["checking"]
    staged, _ = _stage(session, owner, household, checking, SANTANDER)
    _commit(session, owner, household, checking, staged)
    session.commit()

    again = importing.previous_import_of(
        session, account_id=checking.id, digest=importing.file_digest(SANTANDER)
    )
    assert again is not None
    assert again.source["filename"] == "statement.csv"


def test_re_importing_the_same_rows_finds_them_all_already_there(session, owner, household, accounts):
    """The occurrence counter restarts per file.

    Seeded from the database it would hand the second import fresh keys and let
    every row through twice -- which is the bug that shipped last time.
    """
    checking = accounts["checking"]
    first, _ = _stage(session, owner, household, checking, SANTANDER)
    _commit(session, owner, household, checking, first)

    second, lines = _stage(session, owner, household, checking, SANTANDER, filename="again.csv")
    assert {line.outcome for line in lines} == {ImportOutcome.duplicate_skipped}

    result = _commit(session, owner, household, checking, second)
    assert result["created"] == 0
    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 3


def test_an_extended_statement_imports_only_what_is_new(session, owner, household, accounts):
    checking = accounts["checking"]
    first, _ = _stage(session, owner, household, checking, SANTANDER)
    _commit(session, owner, household, checking, first)

    extended = SANTANDER + b"11/01/2026;FARMACIA;-8,00\r\n"
    second, lines = _stage(session, owner, household, checking, extended, filename="jan-full.csv")
    counts = importing.summarise(lines)
    assert counts["duplicate_skipped"] == 3
    assert counts["created"] == 1

    _commit(session, owner, household, checking, second)
    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 4


def test_two_identical_rows_on_one_day_both_survive(session, owner, household, accounts):
    """Two coffees of the same price on the same day are two purchases."""
    twice = (
        b"Fecha;Concepto;Importe\n"
        b"05/01/2026;CAFE;-2,50\n"
        b"05/01/2026;CAFE;-2,50\n"
    )
    checking = accounts["checking"]
    staged, _ = _stage(session, owner, household, checking, twice)
    result = _commit(session, owner, household, checking, staged)
    assert result["created"] == 2


# --------------------------------------------------------------------------- #
# Absorption
# --------------------------------------------------------------------------- #


def test_a_hand_entered_twin_is_absorbed_not_duplicated(session, owner, household, accounts, write):
    """Entering €45.20 and then importing the statement must not make €90.40."""
    checking = accounts["checking"]
    with write():
        typed = txn_service.create(
            session, account=checking, date=date(2026, 1, 4), amount=-4_520, memo="mine"
        )

    staged, lines = _stage(session, owner, household, checking, SANTANDER)
    matched = [line for line in lines if line.outcome is ImportOutcome.matched_existing]
    assert len(matched) == 1
    assert matched[0].transaction_id == typed.id
    assert "already have" in matched[0].reason

    result = _commit(session, owner, household, checking, staged)
    assert result["absorbed"] == 1
    assert result["created"] == 2

    session.refresh(typed)
    assert typed.import_id is not None
    assert typed.cleared is ClearedState.cleared, "the bank has now seen it"
    assert typed.memo == "mine", "the hand-typed memo wins"
    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 3


def test_a_proposed_match_can_be_rejected_in_the_preview(session, owner, household, accounts, write):
    """The matching rule ignores the payee, so two unrelated equal amounts a few
    days apart can be proposed. The preview is what makes that safe."""
    checking = accounts["checking"]
    with write():
        txn_service.create(
            session, account=checking, date=date(2026, 1, 4), amount=-4_520, memo="something else"
        )

    staged, lines = _stage(session, owner, household, checking, SANTANDER)
    matched = next(line for line in lines if line.outcome is ImportOutcome.matched_existing)

    result = _commit(
        session, owner, household, checking, staged, reject_matches={matched.id}
    )
    assert result["absorbed"] == 0
    assert result["created"] == 3
    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 4


def test_a_line_can_be_skipped_in_the_preview(session, owner, household, accounts):
    checking = accounts["checking"]
    staged, lines = _stage(session, owner, household, checking, SANTANDER)
    unwanted = lines[0]

    result = _commit(session, owner, household, checking, staged, skip_line_ids={unwanted.id})
    assert result["created"] == 2
    assert result["skipped"] == 1


def test_a_reconciled_row_is_never_absorbed(session, owner, household, accounts, write):
    checking = accounts["checking"]
    with write():
        txn_service.create(
            session,
            account=checking,
            date=date(2026, 1, 4),
            amount=-4_520,
            cleared=ClearedState.reconciled,
        )

    _, lines = _stage(session, owner, household, checking, SANTANDER)
    assert all(line.outcome is not ImportOutcome.matched_existing for line in lines)


# --------------------------------------------------------------------------- #
# Rules and undo
# --------------------------------------------------------------------------- #


def test_a_rule_renames_a_raw_bank_string(session, owner, household, accounts, write):
    checking = accounts["checking"]
    with write():
        carrefour = payee_service.get_or_create(session, household.id, "Mercadona")
        payee_service.create_rule(
            session,
            household_id=household.id,
            match_type=MatchType.contains,
            pattern="MERCADONA",
            payee=carrefour,
        )

    staged, lines = _stage(session, owner, household, checking, SANTANDER)
    renamed = next(line for line in lines if "MERCADONA" in line.raw)
    assert renamed.parsed["payee_resolved"] == "Mercadona"

    _commit(session, owner, household, checking, staged)
    txn = session.execute(
        select(Transaction).where(Transaction.import_payee_original == "MERCADONA 1234")
    ).scalar_one()
    assert txn.payee_id == carrefour.id
    assert txn.import_payee_original == "MERCADONA 1234", "the raw string is kept"


def test_undoing_a_committed_import_returns_the_balance(session, owner, household, accounts):
    from app.services import accounts as account_service

    checking = accounts["checking"]
    staged, _ = _stage(session, owner, household, checking, SANTANDER)

    with batch(
        session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id
    ) as applying:
        importing.commit(session, batch_row=staged, account=checking, )
        staged.status = BatchStatus.applied
    session.commit()

    assert account_service.balances(session, checking.id)["balance"] != 0

    undo_batch(session, applying.id, actor_id=owner.id)
    session.commit()

    assert account_service.balances(session, checking.id)["balance"] == 0
    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 0


def test_an_abandoned_preview_is_still_a_record(session, owner, household, accounts):
    """A staged import that is never committed leaves the register untouched and
    the verdicts readable."""
    staged, lines = _stage(session, owner, household, accounts["checking"], SANTANDER)
    session.commit()

    assert session.get(Batch, staged.id).status is BatchStatus.preview
    assert session.execute(select(func.count()).select_from(Transaction)).scalar_one() == 0
    assert session.execute(
        select(func.count()).select_from(ImportLine).where(ImportLine.batch_id == staged.id)
    ).scalar_one() == 3


# --------------------------------------------------------------------------- #
# Found by the review round, each fixed and pinned
# --------------------------------------------------------------------------- #


def test_two_hand_entered_twins_both_get_absorbed(session, owner, household, accounts, write):
    """Two identical purchases, both already typed in, both on the statement.

    A twin an earlier line absorbed is spoken for. Offering it to the second
    line as well made that line look new -- three rows for two purchases, and
    the balance doubled for one of them.
    """
    from app.services import accounts as account_service

    checking = accounts["checking"]
    with write():
        for _ in range(2):
            txn_service.create(session, account=checking, date=date(2026, 1, 5), amount=-1_250)
    before = account_service.balances(session, checking.id)["balance"]
    assert before == -2_500

    twice = (
        b"Fecha;Concepto;Importe\n"
        b"05/01/2026;CAFE;-12,50\n"
        b"05/01/2026;CAFE;-12,50\n"
    )
    staged, lines = _stage(session, owner, household, checking, twice)
    assert [line.outcome for line in lines] == [
        ImportOutcome.matched_existing,
        ImportOutcome.matched_existing,
    ]

    result = _commit(session, owner, household, checking, staged)
    assert result["absorbed"] == 2 and result["created"] == 0
    assert account_service.balances(session, checking.id)["balance"] == before


def test_one_unreadable_amount_is_one_rejected_line(session, owner, household, accounts):
    """It used to be an uncaught decimal error, which lost the whole file --
    the exact failure this module's docstring says was fixed."""
    absurd = (
        b"Fecha;Concepto;Importe\n"
        b"05/01/2026;FINE;-10,00\n"
        b"06/01/2026;ABSURD;-99999999999999999999999999999999999999,00\n"
        b"07/01/2026;ALSO FINE;-20,00\n"
    )
    staged, lines = _stage(session, owner, household, accounts["checking"], absurd)
    rejected = [line for line in lines if line.outcome is ImportOutcome.rejected]
    assert len(rejected) == 1
    assert "too large" in rejected[0].reason

    result = _commit(session, owner, household, accounts["checking"], staged)
    assert result["created"] == 2, "the readable rows still import"


def test_a_twin_edited_between_preview_and_commit_is_left_alone(session, owner, household, accounts, write):
    """Absorbing it would mark a row as seen by the bank while it disagrees
    with the statement line it supposedly matches."""
    checking = accounts["checking"]
    with write():
        typed = txn_service.create(
            session, account=checking, date=date(2026, 1, 4), amount=-4_520, memo="mine"
        )

    staged, lines = _stage(session, owner, household, checking, SANTANDER)
    assert any(line.outcome is ImportOutcome.matched_existing for line in lines)

    with write():
        txn_service.update(session, typed, amount=-9_999)

    result = _commit(session, owner, household, checking, staged)
    assert result["absorbed"] == 0
    session.refresh(typed)
    assert typed.amount == -9_999, "the edit stands"
    assert typed.import_id is None, "and it was not marked as seen by the bank"


def test_debe_is_money_out_and_haber_is_money_in():
    """They were mapped the wrong way round, which inverted every sign in a
    Spanish export -- the one locale this app is certain to meet."""
    spanish = (
        b"Fecha;Concepto;Debe;Haber\n"
        b"05/01/2026;MERCADONA;45,20;\n"
        b"07/01/2026;NOMINA;;2100,00\n"
    )
    sniffed = sniffing.sniff(spanish)
    assert sniffed.format.outflow_column == "Debe"
    assert sniffed.format.inflow_column == "Haber"

    rows = importing.parse(spanish, sniffed.format)
    assert [str(row.amount) for row in rows] == ["-45.20", "2100.00"]


@pytest.mark.parametrize(
    "sample,expected",
    [
        ("2,100.00", "."),
        ("1,500", "."),
        ("8.810,07", ","),
        ("-45,20", ","),
        ("1.234", "."),
    ],
)
def test_a_three_digit_tail_never_decides_the_separator(sample, expected):
    """"1,500" is the same shape as "1.500" and settles nothing. Deciding from
    it read 2,100.00 as two euros and ten cents."""
    assert sniffing.guess_decimal_separator([sample]) == expected


def test_an_ambiguous_file_says_it_guessed():
    us_style = b"Date,Description,Amount\n01/02/2026,A,-10.00\n01/03/2026,B,-20.00\n"
    sniffed = sniffing.sniff(us_style)
    assert any("day-first or month-first" in w for w in sniffed.warnings)


def test_a_file_with_no_sign_at_all_says_so():
    """All-positive amounts with no second column would import a month of
    spending as income."""
    indicator = (
        b"Date,Description,Amount,DrCr\n"
        b"05/01/2026,SHOP,45.20,DR\n"
        b"07/01/2026,SALARY,2100.00,CR\n"
    )
    sniffed = sniffing.sniff(indicator)
    assert any("every amount in this file is positive" in w for w in sniffed.warnings)


def test_a_catastrophic_rule_cannot_stall_the_import(session, owner, household):
    """`(a|a)*$` is six characters, and the user is allowed to write it.

    Against a 26-character payee string the stdlib engine took 23.8 seconds,
    doubling per additional character -- and the rules run against every row of
    the file, on a threadpool worker, so a few imports stopped the instance
    answering at all. The rule now gets a deadline, and a rule that blows it is
    set aside for the rest of the file rather than paid for on every row.
    """
    import time

    from app.models import MatchType, PayeeRule
    from app.services.payees import RegexBudget

    rule = PayeeRule(
        id="rule-under-test",
        household_id=household.id,
        match_type=MatchType.regex,
        pattern=r"(a|a)*$",
        payee_id="payee",
        priority=100,
    )

    budget = RegexBudget()
    rows = ["a" * 40 + "b"] * 300

    started = time.monotonic()
    hits = sum(1 for row in rows if budget.matches(rule, row))
    spent = time.monotonic() - started

    assert hits == 0
    assert rule.id in budget.exhausted, "the rule should have been set aside after the first row"
    # Generous, because CI is slower than a laptop -- but three orders of
    # magnitude below the 23.8s one row used to cost.
    assert spent < 5, f"300 rows took {spent:.1f}s against a rule with a deadline"


def test_an_ordinary_rule_is_unaffected_by_the_deadline(session, owner, household):
    from app.models import MatchType, PayeeRule
    from app.services.payees import matches

    rule = PayeeRule(
        id="ordinary",
        household_id=household.id,
        match_type=MatchType.regex,
        pattern=r"^MERCADONA",
        payee_id="payee",
        priority=100,
    )
    assert matches(rule, "MERCADONA MADRID 4432") is True
    assert matches(rule, "CARREFOUR MADRID") is False
    # Case-insensitive, as the rules screen promises.
    assert matches(rule, "mercadona madrid") is True


def test_every_match_type_actually_matches(session, owner, household):
    """Three of the four had no test at all: coverage showed the `equals`,
    `prefix` and `regex` branches never executing, and the one regex test only
    asserted that an *invalid* pattern was refused."""
    from app.models import MatchType, PayeeRule
    from app.services.payees import matches

    def rule(kind: MatchType, pattern: str) -> PayeeRule:
        return PayeeRule(
            id=f"r-{kind.value}",
            household_id=household.id,
            match_type=kind,
            pattern=pattern,
            payee_id="payee",
            priority=100,
        )

    raw = "MERCADONA MADRID 4432"

    contains = rule(MatchType.contains, "madrid")
    assert matches(contains, raw) is True
    assert matches(contains, "CARREFOUR BARCELONA") is False

    equals = rule(MatchType.equals, "mercadona madrid 4432")
    assert matches(equals, raw) is True, "equals is case-insensitive"
    assert matches(equals, "MERCADONA MADRID") is False, "and it is the whole string"

    prefix = rule(MatchType.prefix, "mercadona")
    assert matches(prefix, raw) is True
    assert matches(prefix, "SUPER MERCADONA") is False, "prefix is anchored at the start"

    pattern = rule(MatchType.regex, r"MERCADONA\s+\w+\s+\d{4}$")
    assert matches(pattern, raw) is True
    assert matches(pattern, "MERCADONA MADRID 44") is False


# --------------------------------------------------------------------------- #
# A rail rule, through a real import. Issue #58.
# --------------------------------------------------------------------------- #

#: Two shops behind one rail, in the bank's own wording. `stage` resolves the
#: payee and freezes the answer onto the line, so a rewrite that only affected
#: matching would be lost between the preview and the ledger -- which is the
#: bug this pair of tests exists to prevent.
BIZUM_STATEMENT = (
    b"Fecha;Concepto;Importe;Saldo\r\n"
    b"05/01/2026;PAGO MOVIL BAR MARISOL;-18,50;8.810,07\r\n"
    b"06/01/2026;PAGO MOVIL BARRIO HOT YOGA;-55,00;8.755,07\r\n"
)


def test_a_rewrite_rule_survives_staging_and_names_the_shop_in_the_ledger(
    session, owner, household, accounts, write
):
    """Two rows behind one rail become two payees, named for the shops."""
    checking = accounts["checking"]
    with write():
        payee_service.create_rule(
            session,
            household_id=household.id,
            match_type=MatchType.prefix,
            action=RuleAction.rewrite,
            pattern="PAGO MOVIL",
            priority=10,
        )

    staged, lines = _stage(session, owner, household, checking, BIZUM_STATEMENT)
    # Carried on the line, not merely computed during matching: the preview and
    # the commit are separate requests and the second one cannot re-derive it
    # without the rules the first one used.
    assert [line.parsed.get("payee_rewritten") for line in lines] == [
        "Bar Marisol", "Barrio Hot Yoga",
    ]

    _commit(session, owner, household, checking, staged)
    rows = session.execute(
        select(Transaction).where(Transaction.account_id == checking.id)
    ).scalars().all()
    assert sorted(row.payee.name for row in rows) == ["Bar Marisol", "Barrio Hot Yoga"]
    assert len({row.payee_id for row in rows}) == 2
    # The bank's own words are still on the row. A rewrite is lossy about the
    # payee *name* and must not be lossy about what arrived.
    assert sorted(row.import_payee_original for row in rows) == [
        "PAGO MOVIL BAR MARISOL", "PAGO MOVIL BARRIO HOT YOGA",
    ]


def test_without_a_rewrite_rule_the_same_statement_keeps_the_rail_in_the_name(
    session, owner, household, accounts
):
    """The other half of the pair: this is what changed, and what did not.

    Without the rule the descriptor goes through untouched -- rail and all --
    which is the behaviour every household that writes no rewrite rule keeps.
    """
    checking = accounts["checking"]
    staged, _ = _stage(session, owner, household, checking, BIZUM_STATEMENT)
    _commit(session, owner, household, checking, staged)
    rows = session.execute(
        select(Transaction).where(Transaction.account_id == checking.id)
    ).scalars().all()
    assert sorted(row.payee.name for row in rows) == [
        "PAGO MOVIL BAR MARISOL", "PAGO MOVIL BARRIO HOT YOGA",
    ]


# --------------------------------------------------------------------------- #
# A non-finite amount is one bad line (issue #218)
# --------------------------------------------------------------------------- #


def _ofx_two_rows_same_day(bad: str) -> bytes:
    rows = "".join(
        "<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260105000000</DTPOSTED>"
        f"<TRNAMT>{amount}</TRNAMT><FITID>{fitid}</FITID><NAME>{name}</NAME></STMTTRN>"
        for amount, fitid, name in ((bad, "NF-1", "ODD ROW"), ("-5.00", "NF-2", "GOOD ROW"))
    )
    return (
        '<?xml version="1.0" standalone="no"?>'
        '<?OFX OFXHEADER="200" VERSION="202" SECURITY="NONE"?>'
        "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>EUR</CURDEF>"
        "<BANKACCTFROM><ACCTID>NF|1</ACCTID></BANKACCTFROM>"
        f"<BANKTRANLIST>{rows}</BANKTRANLIST>"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>"
    ).encode()


@pytest.mark.parametrize("bad", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_non_finite_ofx_amount_is_one_rejected_line(session, owner, household, accounts, bad):
    """`Decimal("NaN")` parses, and two rows on one day made the staging sort
    compare it -- which raised, and turned the upload into a 500."""
    staged, lines = _stage(
        session, owner, household, accounts["checking"],
        _ofx_two_rows_same_day(bad), filename="odd.ofx",
    )

    by_outcome = {line.outcome: line for line in lines}
    assert len(lines) == 2
    assert by_outcome[ImportOutcome.rejected].reason == f"no usable amount in {bad!r}"
    assert ImportOutcome.created in by_outcome, "the good row on the same day still stages"

    assert _commit(session, owner, household, accounts["checking"], staged)["created"] == 1


def test_staging_survives_a_nan_from_any_parser(session, owner, household, accounts):
    """The belt: rows built by something other than the OFX reader can carry a
    NaN too, and the sort must not be what refuses the file."""
    from decimal import Decimal

    from statements.parsing import ParsedRow

    rows = [
        ParsedRow(line_no=1, raw="a", when=date(2026, 1, 5), amount=Decimal("NaN"), payee="A"),
        ParsedRow(line_no=2, raw="b", when=date(2026, 1, 5), amount=Decimal("-5.00"), payee="B"),
    ]
    with batch(
        session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id,
        source={"account_id": accounts["checking"].id},
    ) as staged:
        lines = importing.stage(session, account=accounts["checking"], rows=rows, batch_row=staged)
        staged.status = BatchStatus.preview

    outcomes = sorted((line.line_no, line.outcome.value) for line in lines)
    assert outcomes == [(1, "rejected"), (2, "created")]
