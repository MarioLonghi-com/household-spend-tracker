"""One-time Import from YNAB: the file, the API, the mapping and the one batch (#183).

Every fixture is built here and is nobody's: made-up accounts ("Bank
Current", "Rainy Day Pot"), made-up payees, round amounts. The shape of the
file -- its columns, its "£1,234.56" amounts, its "Transfer : <Account>"
payees, its "Split (1/2)" memos -- is YNAB's.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import urllib.error
import zipfile
from datetime import date

import pytest
from sqlalchemy import func, select

from app import schemas
from app.api import deps
from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.errors import Forbidden, ValidationError
from app.models import (
    Account,
    Batch,
    BatchKind,
    BatchStatus,
    Category,
    CategoryGroup,
    Change,
    ClearedState,
    LinkSource,
    Payee,
    Transaction,
)
from app.services import categories as category_service
from app.services import describing
from app.services import payees as payee_service
from app.services import transactions as transaction_service
from app.services.one_time_import import engine, ynab_api, ynab_source
from tests.conftest import HEADERS, _setup_owner

HEADER = [
    "Account", "Flag", "Date", "Payee", "Category Group/Category", "Category Group",
    "Category", "Memo", "Outflow", "Inflow", "Cleared",
]


def _row(account, date_text, payee, category="", group="", memo="", out="£0.00", inn="£0.00",
         flag="", cleared="Cleared"):
    combined = f"{group}: {category}" if group and category else ""
    return [account, flag, date_text, payee, combined, group, category, memo, out, inn, cleared]


#: Eleven rows, three accounts, one of everything the importer handles.
ROWS = [
    _row("Bank Current", "2026-01-05", "Starting Balance", "Ready to Assign", "Inflow",
         inn="£1,000.00", cleared="Reconciled"),
    _row("Rainy Day Pot", "2026-01-05", "Starting Balance", "Ready to Assign", "Inflow",
         inn="£50.00", cleared="Reconciled"),
    _row("Bank Current", "2026-01-10", "Corner Shop", "Groceries", "Everyday", memo="milk",
         out="£12.34", flag="Orange - REVIEW"),
    _row("Bank Current", "2026-01-12", "Transfer : Rainy Day Pot", out="£100.00"),
    _row("Rainy Day Pot", "2026-01-12", "Transfer : Bank Current", inn="£100.00"),
    _row("Bank Current", "2026-02-01", "Jane Doe Ltd", "Salary", "Income", inn="£2,500.00",
         flag="Blue", cleared="Uncleared"),
    _row("Bank Current", "2026-02-03", "Big Store", "Household", "Everyday",
         memo="Split (1/2) towels", out="£20.00"),
    _row("Bank Current", "2026-02-03", "Big Store", "Groceries", "Everyday",
         memo="Split (2/2) bread", out="£5.50"),
    _row("Old Card", "2026-02-10", "Transfer : Bank Current", inn="£30.00"),
    _row("Bank Current", "2026-02-10", "Transfer : Old Card", out="£30.00"),
    _row("Bank Current", "2026-03-01", "Mystery", "Uncategorized", "Internal Master Category",
         out="£7.00"),
]


def _register(rows=ROWS, header=HEADER) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return ("﻿" + buffer.getvalue()).encode()


PLAN_CSV = (
    '"Month","Category Group/Category","Category Group","Category","Assigned","Activity","Available"\r\n'
    '"Jan 2026","Everyday: Groceries","Everyday","Groceries",£100.00,-£12.34,£87.66\r\n'
).encode()


def _zip(**members: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def _source(raw: bytes | None = None, name="Budget - Register.csv"):
    return ynab_source.from_file(raw if raw is not None else _register(), name)


def _groceries(session, household, owner) -> Category:
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        group = category_service.create_group(session, household.id, "Everyday")
        found = category_service.create_category(
            session, household.id, group_id=group.id, name="Groceries"
        )
    session.commit()
    return found


def _plan(fixture, groceries: Category | None = None, **over) -> engine.Plan:
    """Bank Current -> UK Savings, the pot created, the card skipped."""
    fields = {
        "currency": "GBP",
        "date_format": "YYYY-MM-DD",
        "accounts": {
            "Bank Current": {"kind": "existing", "account_id": fixture["pounds"].id},
            "Rainy Day Pot": {"kind": "create", "name": "Rainy Day Pot", "type": "savings"},
            "Old Card": {"kind": "skip"},
        },
        "categories": {
            "Groceries": (
                {"kind": "existing", "category_id": groceries.id}
                if groceries is not None
                else {"kind": "create", "name": "Groceries"}
            ),
            "Salary": {"kind": "create", "name": "Salary"},
            "Household": {"kind": "uncategorised"},
        },
        "acknowledge_cleared_reset": True,
    }
    fields.update(over)
    return engine.Plan(**fields)


def _run(session, household, owner, plan, *, source=None, commit=True):
    return engine.run(
        session, household, actor_id=owner.id, source=source or _source(), plan=plan, commit=commit
    )


def _rows(session, account_id) -> list[Transaction]:
    return list(
        session.execute(
            select(Transaction).where(Transaction.account_id == account_id).order_by(
                Transaction.date, Transaction.amount
            )
        ).scalars()
    )


def _account(session, household, name) -> Account:
    return session.execute(
        select(Account).where(Account.household_id == household.id, Account.name == name)
    ).scalar_one()


def _counts(session) -> dict[str, int]:
    return {
        model.__name__: session.execute(select(func.count()).select_from(model)).scalar_one()
        for model in (Account, Transaction, Payee, Category, CategoryGroup)
    }


# --------------------------------------------------------------------------- #
# Reading the file
# --------------------------------------------------------------------------- #


def test_a_register_csv_is_read_row_by_row():
    source = _source()
    assert len(source.rows) == len(ROWS)
    assert list(source.accounts) == ["Bank Current", "Rainy Day Pot", "Old Card"]
    assert source.filename == "Budget - Register.csv"
    assert source.via == "csv"

    shop = source.rows[2]
    assert (shop.payee, shop.category, shop.memo, shop.flag) == (
        "Corner Shop", "Groceries", "milk", "Orange - REVIEW"
    )
    assert shop.amount_text == ("£12.34", "£0.00")
    assert source.rows[0].category == ynab_source.READY_TO_ASSIGN
    assert source.rows[0].starting_balance is True
    assert source.rows[3].transfer_account_key == "Rainy Day Pot"
    assert [row.split for row in source.rows[6:8]] == [True, True]
    # Stable per row, and different for two rows that differ only in place.
    assert len({row.ref for row in source.rows}) == len(ROWS)
    assert _source().rows[5].ref == source.rows[5].ref


def test_a_zip_is_read_for_its_register_and_its_plan_is_ignored():
    raw = _zip(**{
        "Budget as of 2026 - Plan.csv": PLAN_CSV,
        "Budget as of 2026 - Register.csv": _register(),
    })
    source = ynab_source.from_file(raw, "export.zip")
    assert len(source.rows) == len(ROWS)
    assert source.filename == "export.zip"


@pytest.mark.parametrize(
    "raw",
    [PLAN_CSV, _zip(**{"Budget - Plan.csv": PLAN_CSV})],
    ids=["plan-csv", "zip-with-only-a-plan"],
)
def test_a_plan_on_its_own_is_refused_with_the_reason(raw):
    with pytest.raises(ValidationError) as refused:
        ynab_source.from_file(raw, "Plan.csv")
    assert "Only the YNAB Register.csv is needed, not the Plan.csv" in str(refused.value)


def test_a_file_that_is_not_a_register_is_refused():
    with pytest.raises(ValidationError, match="not a YNAB Register.csv"):
        ynab_source.from_file(b"name,type\nPot,savings\n", "accounts.csv")


def test_currency_and_decimals_are_read_from_the_symbol():
    source = _source()
    assert (source.currency, source.symbol, source.currency_confirmed_needed) == ("GBP", "£", True)
    assert engine.amount_of(source.rows[5], "GBP", source) == 250000

    euros = _source(_register([
        _row("Bank Current", "2026-01-10", "Corner Shop", out="1.234,56 €", inn="0,00 €"),
        _row("Bank Current", "2026-01-11", "Corner Shop", out="0,00 €", inn="12,30 €"),
    ]))
    assert (euros.currency, euros.decimal_comma) == ("EUR", True)
    assert [engine.amount_of(row, "EUR", euros) for row in euros.rows] == [-123456, 1230]


def test_an_amount_with_more_decimals_than_the_currency_is_refused_not_rounded():
    source = _source(_register([_row("Bank Current", "2026-01-10", "Shop", out="£1.234")]))
    with pytest.raises(ValidationError, match="more decimals"):
        engine.amount_of(source.rows[0], "GBP", source)


def test_a_huge_amount_cell_is_refused_quickly_rather_than_regexed(monkeypatch):
    """131,072 characters is one csv field at the default limit. The old
    trailing-non-digits regex did not finish it in twenty seconds (#220)."""
    import time

    cell = "£" + "x" * 131_070 + "1"
    raw = _register([_row("Bank Current", "2026-01-10", "Shop", out=cell), ROWS[2]])
    started = time.monotonic()
    with pytest.raises(ValidationError, match="amount longer than 64 characters"):
        _source(raw)
    assert time.monotonic() - started < 1


def test_the_digit_tail_is_linear_and_still_reads_the_decimal_mark():
    import time

    started = time.monotonic()
    assert ynab_source._digit_tail("£" + " " * 131_072) == ""
    assert time.monotonic() - started < 1
    assert ynab_source._digit_tail("£1,234.56 ") == "£1,234.56"
    assert ynab_source._digit_tail("1.234,56 €") == "1.234,56"
    assert ynab_source._decimal_comma(["1.234,56 €", "0,00 €"]) is True
    assert ynab_source._decimal_comma(["£1,234.56", "£0.00"]) is False


def test_date_formats_are_detected_and_an_ambiguous_one_is_offered_both_ways():
    assert _source().date_formats == ["YYYY-MM-DD"]

    day_first = _source(_register([
        _row("Bank Current", "25/01/2026", "Shop", out="£1.00"),
        _row("Bank Current", "03/02/2026", "Shop", out="£1.00"),
    ]))
    assert day_first.date_formats == ["DD/MM/YYYY"]

    either = _source(_register([_row("Bank Current", "03/02/2026", "Shop", out="£1.00")]))
    assert either.date_formats == ["DD/MM/YYYY", "MM/DD/YYYY"]


# --------------------------------------------------------------------------- #
# Analyse
# --------------------------------------------------------------------------- #


def test_analyse_counts_suggests_and_lists_targets(session, owner, household, accounts):
    groceries = _groceries(session, household, owner)
    found = engine.analyse(session, household, _source())

    assert found["currency"] == {"detected": "GBP", "symbol": "£", "confirmed_needed": True}
    assert found["date_format"] == {"detected": "YYYY-MM-DD", "ambiguous": False, "options": ["YYYY-MM-DD"]}
    totals = found["totals"]
    assert totals["rows"] == 11
    assert (totals["date_min"], totals["date_max"]) == (date(2026, 1, 5), date(2026, 3, 1))
    assert (totals["transfers"], totals["splits"], totals["starting_balance_rows"]) == (4, 2, 2)
    assert totals["cleared"] == {"reconciled": 2, "cleared": 8, "uncleared": 1}
    assert {one["label"]: one["count"] for one in found["flags"]} == {"Orange - REVIEW": 1, "Blue": 1}

    by_key = {one["key"]: one for one in found["accounts"]}
    assert by_key["Bank Current"]["rows"] == 8
    assert by_key["Bank Current"]["balance_minor"] == 100000 - 1234 - 10000 + 250000 - 2000 - 550 - 3000 - 700
    assert by_key["Old Card"]["suggestion"] == {"kind": "create", "name": "Old Card", "type": "credit_card"}

    targets = {one["name"]: one for one in found["targets"]["accounts"]}
    assert targets["UK Savings"]["eligible"] is True
    assert targets["Checking"]["eligible"] is False  # EUR, and the plan is GBP

    cats = {one["key"]: one for one in found["categories"]}
    assert cats["Groceries"]["suggestion"] == {"kind": "existing", "category_id": groceries.id, "score": 1.0}
    assert cats["Groceries"]["groups"] == ["Everyday"]
    assert cats["Salary"]["suggestion"] == {"kind": "create", "name": "Salary"}
    for fixed in (ynab_source.READY_TO_ASSIGN, "Uncategorized"):
        assert cats[fixed]["fixed_uncategorised"] is True
        assert cats[fixed]["suggestion"] == {"kind": "uncategorised"}
    assert found["previous_imports"] == []


def test_an_account_suggestion_picks_the_similar_name_in_the_right_currency(session, owner, household):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        near = Account(household_id=household.id, name="Bank Current Acct", type="checking", currency="GBP")
        wrong = Account(household_id=household.id, name="Bank Current", type="checking", currency="EUR")
        session.add_all([near, wrong])
    session.commit()
    found = engine.analyse(session, household, _source())
    by_key = {one["key"]: one["suggestion"] for one in found["accounts"]}
    assert by_key["Bank Current"]["kind"] == "existing"
    assert by_key["Bank Current"]["account_id"] == near.id
    # Nothing near enough for the pot, so it is to be created.
    assert by_key["Rainy Day Pot"] == {"kind": "create", "name": "Rainy Day Pot", "type": "savings"}


# --------------------------------------------------------------------------- #
# Commit: what arrives, and where
# --------------------------------------------------------------------------- #


def test_a_commit_maps_creates_skips_and_resets_cleared(session, owner, household, accounts):
    groceries = _groceries(session, household, owner)
    report = _run(session, household, owner, _plan(accounts, groceries))

    assert report["committed"] is True and report["batch_id"]
    counts = report["counts"]
    assert counts["rows_in_file"] == 11
    assert counts["skipped_account"] == 1  # the card's one row
    assert counts["imported"] == 10
    assert counts["transfers_linked"] == 1
    assert counts["failed"] == 0

    pounds = _rows(session, accounts["pounds"].id)
    assert len(pounds) == 8
    assert {row.cleared for row in pounds} == {ClearedState.uncleared}
    assert {row.import_source for row in pounds} == {"one-time-import:ynab-csv"}
    assert all(row.import_id.startswith("ynab:") for row in pounds)
    assert sum(row.amount for row in pounds) == 100000 - 1234 - 10000 + 250000 - 2000 - 550 - 3000 - 700

    shop = next(row for row in pounds if row.amount == -1234)
    assert shop.category_id == groceries.id
    assert shop.memo == "milk Flag: Orange - REVIEW"
    assert shop.payee.name == "Corner Shop"

    salary = next(row for row in pounds if row.amount == 250000)
    assert salary.memo == "Flag: Blue"
    made_salary = session.get(Category, salary.category_id)
    assert made_salary.name == "Salary"
    assert session.get(CategoryGroup, made_salary.group_id).name == "Imported from YNAB"

    # Ready to Assign and Uncategorized are no category at all; so is a
    # category mapped to Uncategorised.
    opening = next(row for row in pounds if row.amount == 100000)
    mystery = next(row for row in pounds if row.amount == -700)
    towels = next(row for row in pounds if row.amount == -2000)
    assert (opening.category_id, mystery.category_id, towels.category_id) == (None, None, None)

    # The split arrived as two ordinary rows.
    parts = [row for row in pounds if row.date == date(2026, 2, 3)]
    assert sorted(row.amount for row in parts) == [-2000, -550]
    assert {row.split_id for row in parts} == {None}

    pot = _account(session, household, "Rainy Day Pot")
    assert pot.currency == "GBP" and pot.type.value == "savings"
    assert report["created"]["accounts"] == [
        {"id": pot.id, "name": "Rainy Day Pot", "opening_date": date(2026, 1, 4)}
    ]
    assert [one["name"] for one in report["created"]["categories"]] == ["Salary"]
    assert report["created"]["payees"] >= 4
    assert sum(row.amount for row in _rows(session, pot.id)) == 5000 + 10000


def test_transfers_with_both_sides_imported_are_linked(session, owner, household, accounts):
    _run(session, household, owner, _plan(accounts))
    pot = _account(session, household, "Rainy Day Pot")
    out_leg = next(row for row in _rows(session, accounts["pounds"].id) if row.amount == -10000)
    in_leg = next(row for row in _rows(session, pot.id) if row.amount == 10000)

    assert out_leg.transfer_transaction_id == in_leg.id
    assert in_leg.transfer_transaction_id == out_leg.id
    assert (out_leg.transfer_account_id, in_leg.transfer_account_id) == (pot.id, accounts["pounds"].id)
    assert out_leg.link_source is LinkSource.named
    assert out_leg.payee.name == "Transfer : Rainy Day Pot"
    # YNAB's name for the transfer is not what the bank sent (#265).
    assert out_leg.import_payee_original is None
    assert out_leg.category_id is None


def test_a_transfer_whose_other_side_is_skipped_is_a_plain_row(session, owner, household, accounts):
    report = _run(session, household, owner, _plan(accounts))
    to_card = next(row for row in _rows(session, accounts["pounds"].id) if row.amount == -3000)
    assert to_card.transfer_transaction_id is None
    assert to_card.transfer_account_id is None
    assert to_card.payee.name == "Transfer : Old Card"
    assert [one["amount_minor"] for one in report["unpaired_transfers"]] == [-3000]
    assert "Transfers imported as ordinary transactions" in report["report_text"]
    skipped = [one for one in report["not_imported"] if one["reason"] == "its account is skipped"]
    assert [(one["account"], one["amount_minor"]) for one in skipped] == [("Old Card", 3000)]


def test_flags_can_be_ignored(session, owner, household, accounts):
    _run(session, household, owner, _plan(accounts, flags="ignore"))
    shop = next(row for row in _rows(session, accounts["pounds"].id) if row.amount == -1234)
    assert shop.memo == "milk"


def test_a_date_range_leaves_out_what_is_outside_it(session, owner, household, accounts):
    report = _run(
        session, household, owner,
        _plan(accounts, date_from=date(2026, 1, 11), date_to=date(2026, 2, 5)),
    )
    assert report["counts"]["skipped_date_range"] == 5
    assert sorted(row.date for row in _rows(session, accounts["pounds"].id)) == [
        date(2026, 1, 12), date(2026, 2, 1), date(2026, 2, 3), date(2026, 2, 3)
    ]
    # The pot opens the day before its earliest imported row, not its earliest row.
    pot = _account(session, household, "Rainy Day Pot")
    assert report["created"]["accounts"][0]["opening_date"] == date(2026, 1, 11)
    assert [row.amount for row in _rows(session, pot.id)] == [10000]


def test_starting_balances_can_be_skipped(session, owner, household, accounts):
    report = _run(session, household, owner, _plan(accounts, starting_balance="skip"))
    assert report["counts"]["skipped_starting_balance"] == 2
    assert 100000 not in [row.amount for row in _rows(session, accounts["pounds"].id)]


# --------------------------------------------------------------------------- #
# Duplicates, and running it again
# --------------------------------------------------------------------------- #


def _already_there(session, household, owner, accounts) -> Transaction:
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        row = transaction_service.create(
            session, account=accounts["pounds"], date=date(2026, 1, 11), amount=-1234, category=None
        )
    session.commit()
    return row


def test_a_duplicate_is_skipped_by_default_and_reported(session, owner, household, accounts):
    twin = _already_there(session, household, owner, accounts)
    preview = _run(session, household, owner, _plan(accounts), commit=False)
    assert [(one["decision"], one["existing"]["id"]) for one in preview["duplicates"]] == [
        ("pending", twin.id)
    ]

    report = _run(session, household, owner, _plan(accounts))
    assert report["counts"]["duplicates_skipped"] == 1
    assert report["duplicates"][0]["decision"] == "skip"
    amounts = [row.amount for row in _rows(session, accounts["pounds"].id)]
    assert amounts.count(-1234) == 1


def test_a_duplicate_can_be_imported_one_by_one_or_all(session, owner, household, accounts):
    _already_there(session, household, owner, accounts)
    ref = _source().rows[2].ref
    one = _run(session, household, owner, _plan(accounts, duplicates_import={ref}), commit=False)
    assert one["counts"]["duplicates_imported"] == 1
    assert one["duplicates"][0]["decision"] == "import"

    report = _run(session, household, owner, _plan(accounts, duplicates_all="import"))
    assert report["counts"]["duplicates_imported"] == 1
    assert [row.amount for row in _rows(session, accounts["pounds"].id)].count(-1234) == 2


def test_a_second_run_warns_and_skips_what_the_first_one_brought(session, owner, household, accounts):
    first = _run(session, household, owner, _plan(accounts))
    found = engine.analyse(session, household, _source())
    assert [(one["batch_id"], one["via"], one["filename"], one["status"]) for one in found["previous_imports"]] == [
        (first["batch_id"], "csv", "Budget - Register.csv", "applied")
    ]

    pot = _account(session, household, "Rainy Day Pot")
    again = _plan(accounts, accounts={
        "Bank Current": {"kind": "existing", "account_id": accounts["pounds"].id},
        "Rainy Day Pot": {"kind": "existing", "account_id": pot.id},
        "Old Card": {"kind": "skip"},
    })
    second = _run(session, household, owner, again)
    assert second["counts"]["imported"] == 0
    assert second["counts"]["duplicates_skipped"] == 10
    assert len(_rows(session, accounts["pounds"].id)) == 8


def test_a_second_run_after_a_row_was_redated_skips_it_rather_than_failing(
    session, owner, household, accounts
):
    """The ledger's import ids are read whole, not from the dates the file
    covers: a row the first run brought in and someone then re-dated a month
    away would otherwise reach the unique index and refuse the lot."""
    _run(session, household, owner, _plan(accounts))
    shop = next(row for row in _rows(session, accounts["pounds"].id) if row.amount == -1234)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        transaction_service.update(session, shop, date=date(2026, 6, 30))
    session.commit()

    pot = _account(session, household, "Rainy Day Pot")
    again = _plan(accounts, accounts={
        "Bank Current": {"kind": "existing", "account_id": accounts["pounds"].id},
        "Rainy Day Pot": {"kind": "existing", "account_id": pot.id},
        "Old Card": {"kind": "skip"},
    })
    second = _run(session, household, owner, again)
    assert second["counts"]["imported"] == 0
    assert second["counts"]["failed"] == 0
    assert second["counts"]["duplicates_skipped"] == 10
    assert len(_rows(session, accounts["pounds"].id)) == 8


# --------------------------------------------------------------------------- #
# A Register.csv row's id is what it says, not where it sits (#267)
# --------------------------------------------------------------------------- #

ALREADY = "already imported by an earlier one-time import"

#: Two accounts, one GBP plan: the shop, the pot's interest, two bus fares
#: identical in every cell, and a later card payment.
REEXPORT = [
    _row("Bank Current", "2026-04-02", "Corner Shop", memo="bread", out="£3.45"),
    _row("Rainy Day Pot", "2026-04-03", "Interest", inn="£1.20"),
    _row("Bank Current", "2026-04-04", "Bus Fare", out="£2.50"),
    _row("Bank Current", "2026-04-04", "Bus Fare", out="£2.50"),
    _row("Rainy Day Pot", "2026-04-09", "Pot Fee", out="£0.75"),
]


def _two_accounts_plan(fixture, pot: Account | None = None) -> engine.Plan:
    return engine.Plan(
        currency="GBP",
        date_format="YYYY-MM-DD",
        accounts={
            "Bank Current": {"kind": "existing", "account_id": fixture["pounds"].id},
            "Rainy Day Pot": (
                {"kind": "existing", "account_id": pot.id}
                if pot is not None
                else {"kind": "create", "name": "Rainy Day Pot", "type": "savings"}
            ),
        },
        categories={},
        acknowledge_cleared_reset=True,
    )


def _ledger(session, account_id) -> list[tuple[date, int, str]]:
    return [
        (row.date, row.amount, row.payee.name if row.payee else "")
        for row in _rows(session, account_id)
    ]


def test_a_register_row_id_does_not_move_when_a_row_is_added_above_it():
    first = _source(_register(REEXPORT))
    earlier = _row("Bank Current", "2026-04-01", "Newsagent", out="£1.10")
    second = _source(_register([earlier, *REEXPORT]))
    assert [row.ref for row in second.rows[1:]] == [row.ref for row in first.rows]
    # The two bus fares are the same in every cell and still two rows.
    assert first.rows[2].ref != first.rows[3].ref
    assert len({row.ref for row in first.rows}) == len(REEXPORT)
    # What changes after an import -- cleared, a flag -- is not the row's identity.
    reconciled = [list(one) for one in REEXPORT]
    reconciled[0][HEADER.index("Cleared")] = "Reconciled"
    reconciled[0][HEADER.index("Flag")] = "Red"
    assert _source(_register(reconciled)).rows[0].ref == first.rows[0].ref


def test_a_re_export_with_an_earlier_row_skips_everything_already_imported(
    session, owner, household, accounts
):
    first = _run(session, household, owner, _two_accounts_plan(accounts), source=_source(_register(REEXPORT)))
    assert first["counts"]["imported"] == 5
    pot = _account(session, household, "Rainy Day Pot")
    assert _ledger(session, accounts["pounds"].id) == [
        (date(2026, 4, 2), -345, "Corner Shop"),
        (date(2026, 4, 4), -250, "Bus Fare"),
        (date(2026, 4, 4), -250, "Bus Fare"),
    ]
    assert _ledger(session, pot.id) == [
        (date(2026, 4, 3), 120, "Interest"),
        (date(2026, 4, 9), -75, "Pot Fee"),
    ]

    # YNAB's next export: one row added before all of them, one more bus fare
    # the same as the other two, and the shop reconciled since.
    reexport = [list(one) for one in REEXPORT]
    reexport[0][HEADER.index("Cleared")] = "Reconciled"
    reexport = [
        _row("Bank Current", "2026-04-01", "Newsagent", out="£1.10"),
        *reexport[:4],
        _row("Bank Current", "2026-04-04", "Bus Fare", out="£2.50"),
        reexport[4],
    ]
    preview = _run(
        session, household, owner, _two_accounts_plan(accounts, pot),
        source=_source(_register(reexport)), commit=False,
    )
    # Nothing to decide: every earlier row is recognised by its id.
    assert preview["duplicates"] == []

    second = _run(
        session, household, owner, _two_accounts_plan(accounts, pot), source=_source(_register(reexport))
    )
    assert second["counts"]["imported"] == 2
    assert second["counts"]["duplicates_skipped"] == 5
    assert second["counts"]["duplicates_imported"] == 0
    assert second["counts"]["failed"] == 0
    assert second["duplicates"] == []
    assert sorted(
        (one["date_text"], one["account"], one["amount_minor"], one["reason"])
        for one in second["not_imported"]
    ) == [
        ("2026-04-02", "Bank Current", -345, ALREADY),
        ("2026-04-03", "Rainy Day Pot", 120, ALREADY),
        ("2026-04-04", "Bank Current", -250, ALREADY),
        ("2026-04-04", "Bank Current", -250, ALREADY),
        ("2026-04-09", "Rainy Day Pot", -75, ALREADY),
    ]
    assert _ledger(session, accounts["pounds"].id) == [
        (date(2026, 4, 1), -110, "Newsagent"),
        (date(2026, 4, 2), -345, "Corner Shop"),
        (date(2026, 4, 4), -250, "Bus Fare"),
        (date(2026, 4, 4), -250, "Bus Fare"),
        (date(2026, 4, 4), -250, "Bus Fare"),
    ]
    assert _ledger(session, pot.id) == [
        (date(2026, 4, 3), 120, "Interest"),
        (date(2026, 4, 9), -75, "Pot Fee"),
    ]
    # The other currency's accounts were never touched.
    assert _rows(session, accounts["checking"].id) == []
    assert _rows(session, accounts["card"].id) == []


def test_two_identical_rows_in_one_file_import_as_two(session, owner, household, accounts):
    report = _run(session, household, owner, _two_accounts_plan(accounts), source=_source(_register(REEXPORT)))
    assert report["counts"]["imported"] == 5
    assert report["counts"]["duplicates_skipped"] == 0
    fares = [row for row in _rows(session, accounts["pounds"].id) if row.payee.name == "Bus Fare"]
    assert [(row.date, row.amount) for row in fares] == [(date(2026, 4, 4), -250)] * 2
    assert len({row.import_id for row in fares}) == 2


def _euro_rows(fmt) -> list[list[str]]:
    """One EUR plan over two accounts, its amounts written by ``fmt``."""
    return [
        _row("Joint Current", "2026-06-01", "Landlord", out=fmt("1,234.50"), inn=fmt("0.00")),
        _row("Travel Card", "2026-06-02", "Train", out=fmt("18.00"), inn=fmt("0.00")),
        _row("Joint Current", "2026-06-03", "Bakery", out=fmt("2.50"), inn=fmt("0.00")),
        _row("Joint Current", "2026-06-03", "Bakery", out=fmt("2.50"), inn=fmt("0.00")),
        _row("Travel Card", "2026-06-04", "Refund", out=fmt("0.00"), inn=fmt("7.05")),
    ]


def _euro_plan(fixture) -> engine.Plan:
    return engine.Plan(
        currency="EUR",
        date_format="YYYY-MM-DD",
        accounts={
            "Joint Current": {"kind": "existing", "account_id": fixture["checking"].id},
            "Travel Card": {"kind": "existing", "account_id": fixture["card"].id},
        },
        categories={},
        acknowledge_cleared_reset=True,
    )


def _symbol_first(figure: str) -> str:
    return "€" + figure


def _symbol_after_with_a_decimal_comma(figure: str) -> str:
    # "1,234.50" -> "1.234,50 €"
    return figure.replace(",", "_").replace(".", ",").replace("_", ".") + " €"


def test_a_plan_re_exported_in_another_currency_format_keeps_its_row_ids(
    session, owner, household, accounts
):
    first = _source(_register(_euro_rows(_symbol_first)))
    second = _source(_register(_euro_rows(_symbol_after_with_a_decimal_comma)))
    assert (first.decimal_comma, second.decimal_comma) == (False, True)
    assert second.rows[0].amount_text == ("1.234,50 €", "0,00 €")
    assert [row.ref for row in second.rows] == [row.ref for row in first.rows]
    assert len({row.ref for row in first.rows}) == 5
    # And the same for pounds written "£2.50" and "2.50£".
    pounds = [_row("Bank Current", "2026-06-05", "Bus Fare", out="£2.50")]
    after = [_row("Bank Current", "2026-06-05", "Bus Fare", out="2.50£", inn="0.00£")]
    assert _source(_register(pounds)).rows[0].ref == _source(_register(after)).rows[0].ref

    imported = _run(session, household, owner, _euro_plan(accounts), source=first)
    assert imported["counts"]["imported"] == 5
    again = _run(session, household, owner, _euro_plan(accounts), source=second)
    assert again["counts"]["imported"] == 0
    assert again["counts"]["duplicates_skipped"] == 5
    assert again["duplicates"] == []
    assert sorted(
        (one["date_text"], one["account"], one["amount_minor"], one["reason"])
        for one in again["not_imported"]
    ) == [
        ("2026-06-01", "Joint Current", -123450, ALREADY),
        ("2026-06-02", "Travel Card", -1800, ALREADY),
        ("2026-06-03", "Joint Current", -250, ALREADY),
        ("2026-06-03", "Joint Current", -250, ALREADY),
        ("2026-06-04", "Travel Card", 705, ALREADY),
    ]
    assert [(row.date, row.amount) for row in _rows(session, accounts["checking"].id)] == [
        (date(2026, 6, 1), -123450),
        (date(2026, 6, 3), -250),
        (date(2026, 6, 3), -250),
    ]
    assert [(row.date, row.amount) for row in _rows(session, accounts["card"].id)] == [
        (date(2026, 6, 2), -1800),
        (date(2026, 6, 4), 705),
    ]
    assert _rows(session, accounts["pounds"].id) == []


def test_an_amount_cell_is_keyed_by_its_figure():
    figure = ynab_source._figure
    assert figure("£2.50", decimal_comma=False) == "2.5"
    assert figure("2,50 €", decimal_comma=True) == "2.5"
    assert figure("€1.234,00", decimal_comma=True) == "1234"
    assert figure("-£0.00", decimal_comma=False) == figure("", decimal_comma=False) == "0"
    assert figure("(£3.10)", decimal_comma=False) == "-3.1"
    # Different figures stay different: a cent is not lost to the stripping.
    assert figure("£2.05", decimal_comma=False) != figure("£2.50", decimal_comma=False)
    assert figure("£20.00", decimal_comma=False) == "20"


def _old_line_ref(raw: bytes, line: int) -> str:
    """The id an earlier build gave a Register.csv row: its line and its text."""
    text = raw.decode().lstrip("﻿")
    return hashlib.sha256(f"{line}\x00{text.splitlines()[line - 1]}".encode()).hexdigest()[:32]


def test_a_ledger_imported_with_the_old_line_ids_is_still_recognised(
    session, owner, household, accounts
):
    euro_rows = [
        _row("Joint Current", "2026-05-02", "Bakery", out="€4.20"),
        _row("Travel Card", "2026-05-03", "Train", out="€18.00"),
        _row("Joint Current", "2026-05-04", "Bakery", out="€4.20"),
        _row("Joint Current", "2026-05-04", "Bakery", out="€4.20"),
    ]
    raw = _register(euro_rows)
    plan = engine.Plan(
        currency="EUR",
        date_format="YYYY-MM-DD",
        accounts={
            "Joint Current": {"kind": "existing", "account_id": accounts["checking"].id},
            "Travel Card": {"kind": "existing", "account_id": accounts["card"].id},
        },
        categories={},
        acknowledge_cleared_reset=True,
    )
    # The import as a build before #267 wrote it: each row under its line's id.
    old = _source(raw)
    for row in old.rows:
        row.ref = _old_line_ref(raw, row.line)
    first = _run(session, household, owner, plan, source=old)
    assert first["counts"]["imported"] == 4
    stored = {row.import_id for row in _rows(session, accounts["checking"].id)}
    assert stored == {"ynab:" + _old_line_ref(raw, line) for line in (2, 4, 5)}

    # The same file, read by this build: every row is already there.
    again = _run(session, household, owner, plan, source=_source(raw))
    assert again["counts"]["imported"] == 0
    assert again["counts"]["duplicates_skipped"] == 4
    assert again["duplicates"] == []
    assert {one["reason"] for one in again["not_imported"]} == {ALREADY}
    assert [(row.date, row.amount) for row in _rows(session, accounts["checking"].id)] == [
        (date(2026, 5, 2), -420),
        (date(2026, 5, 4), -420),
        (date(2026, 5, 4), -420),
    ]
    assert [(row.date, row.amount) for row in _rows(session, accounts["card"].id)] == [
        (date(2026, 5, 3), -1800)
    ]
    assert _rows(session, accounts["pounds"].id) == []


# --------------------------------------------------------------------------- #
# Partial, and one batch
# --------------------------------------------------------------------------- #


def test_a_bad_row_is_reported_and_the_rest_still_commit(session, owner, household, accounts, monkeypatch):
    rows = [*ROWS, _row("Bank Current", "2026-03-02", "Shop", out="£1.234"),
            _row("Bank Current", "not a date", "Shop", out="£1.00")]
    source = _source(_register(rows))

    real = transaction_service.create

    def refusing(session, **kwargs):
        if kwargs.get("amount") == 250000:
            raise ValidationError("refused for the test")
        return real(session, **kwargs)

    monkeypatch.setattr(transaction_service, "create", refusing)
    report = _run(session, household, owner, _plan(accounts), source=source)

    assert report["counts"]["failed"] == 3
    assert report["counts"]["imported"] == 9
    reasons = sorted(one["reason"] for one in report["not_imported"] if one["reason"] != "its account is skipped")
    assert any("more decimals" in one for one in reasons)
    assert any("'not a date' is not a YYYY-MM-DD date" in one for one in reasons)
    assert "refused for the test" in reasons
    assert len(_rows(session, accounts["pounds"].id)) == 7
    assert session.get(Batch, report["batch_id"]).summary["failed"] == 3


def test_a_preview_leaves_no_trace(session, owner, household, accounts):
    before = _counts(session)
    batches = session.execute(select(func.count()).select_from(Batch)).scalar_one()
    report = _run(session, household, owner, _plan(accounts), commit=False)
    assert report["committed"] is False and report["batch_id"] is None
    assert report["counts"]["imported"] == 10
    assert _counts(session) == before
    assert session.execute(select(func.count()).select_from(Batch)).scalar_one() == batches


def test_one_undo_takes_the_whole_import_back(session, owner, household, accounts):
    before = _counts(session)
    report = _run(session, household, owner, _plan(accounts))
    assert _counts(session) != before
    assert _counts(session)["Account"] == before["Account"] + 1

    undo_batch(session, report["batch_id"], actor_id=owner.id)
    session.commit()
    assert _counts(session) == before  # accounts, categories, groups and payees included
    assert session.get(Batch, report["batch_id"]).status is BatchStatus.undone


def test_the_plan_is_refused_whole_when_it_cannot_be_right(session, owner, household, accounts):
    two_to_one = _plan(accounts, accounts={
        "Bank Current": {"kind": "existing", "account_id": accounts["pounds"].id},
        "Rainy Day Pot": {"kind": "existing", "account_id": accounts["pounds"].id},
        "Old Card": {"kind": "skip"},
    })
    with pytest.raises(ValidationError, match="each YNAB account needs an account of its own"):
        _run(session, household, owner, two_to_one)

    wrong_currency = _plan(accounts, accounts={
        "Bank Current": {"kind": "existing", "account_id": accounts["checking"].id},
        "Rainy Day Pot": {"kind": "skip"}, "Old Card": {"kind": "skip"},
    })
    with pytest.raises(ValidationError, match="is in EUR"):
        _run(session, household, owner, wrong_currency)

    with pytest.raises(ValidationError, match="reset"):
        _run(session, household, owner, _plan(accounts, acknowledge_cleared_reset=False))
    assert _rows(session, accounts["pounds"].id) == []


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #


def test_history_names_the_workflow_for_the_batch_and_for_each_row(session, owner, household, accounts):
    report = _run(session, household, owner, _plan(accounts))
    row = session.get(Batch, report["batch_id"])
    assert row.kind is BatchKind.imported
    assert row.source == {
        "one_time_import": "ynab", "via": "csv", "filename": "Budget - Register.csv",
        "sha256": _source().sha256,
    }
    said = describing.describe(session, row)
    assert said.headline == "One-time Import · YNAB (CSV)"
    assert said.detail == "10 transactions, 1 transfer linked from Budget - Register.csv."

    shop = next(one for one in _rows(session, accounts["pounds"].id) if one.amount == -1234)
    changes = list(
        session.execute(
            select(Change).where(Change.table_name == "transactions", Change.row_id == shop.id)
        ).scalars()
    )
    lines = describing.describe_changes(session, household.id, changes)
    assert any(line.endswith("by One-time Import · YNAB (CSV)") for line in lines.values())


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #

TOKEN = "synthetic-token-0123456789abcdefghijklmnop"
PLAN_ID = "plan-0001"


def _api_world() -> dict[str, dict]:
    accounts = [
        {"id": "acc-bank", "name": "Bank Current", "type": "checking", "closed": False, "deleted": False},
        {"id": "acc-card", "name": "Shop Card", "type": "creditCard", "closed": True, "deleted": False},
        {"id": "acc-gone", "name": "Removed", "type": "cash", "closed": False, "deleted": True},
    ]
    groups = [
        {"id": "g1", "name": "Everyday", "deleted": False, "categories": [
            {"id": "c1", "name": "Groceries", "deleted": False},
            {"id": "c2", "name": "Household", "deleted": False},
        ]},
    ]
    transactions = [
        {"id": "t-shop", "date": "2026-01-10", "amount": -12340, "memo": "milk", "cleared": "reconciled",
         "flag_color": "orange", "flag_name": "REVIEW", "account_id": "acc-bank",
         "payee_name": "Corner Shop", "category_id": "c1", "category_name": "Groceries",
         "transfer_account_id": None, "transfer_transaction_id": None, "deleted": False,
         "subtransactions": []},
        {"id": "t-deleted", "date": "2026-01-11", "amount": -99000, "memo": None, "cleared": "cleared",
         "flag_color": None, "flag_name": None, "account_id": "acc-bank", "payee_name": "Gone",
         "category_id": None, "category_name": None, "transfer_account_id": None,
         "transfer_transaction_id": None, "deleted": True, "subtransactions": []},
        {"id": "t-split", "date": "2026-01-15", "amount": -25500, "memo": "shop", "cleared": "cleared",
         "flag_color": "blue", "flag_name": None, "account_id": "acc-bank", "payee_name": "Big Store",
         "category_id": None, "category_name": "Split", "transfer_account_id": None,
         "transfer_transaction_id": None, "deleted": False, "subtransactions": [
             {"id": "s-1", "transaction_id": "t-split", "amount": -20000, "memo": "towels",
              "payee_name": None, "category_id": "c2", "category_name": "Household",
              "transfer_account_id": None, "transfer_transaction_id": None, "deleted": False},
             {"id": "s-2", "transaction_id": "t-split", "amount": -5500, "memo": "bread",
              "payee_name": None, "category_id": "c1", "category_name": "Groceries",
              "transfer_account_id": None, "transfer_transaction_id": None, "deleted": False},
             {"id": "s-3", "transaction_id": "t-split", "amount": -1000, "memo": "gone",
              "payee_name": None, "category_id": "c1", "category_name": "Groceries",
              "transfer_account_id": None, "transfer_transaction_id": None, "deleted": True},
         ]},
        {"id": "t-pay", "date": "2026-01-20", "amount": -30000, "memo": None, "cleared": "uncleared",
         "flag_color": None, "flag_name": None, "account_id": "acc-bank",
         "payee_name": "Transfer : Shop Card", "category_id": None, "category_name": None,
         "transfer_account_id": "acc-card", "transfer_transaction_id": "t-paid", "deleted": False,
         "subtransactions": []},
        {"id": "t-paid", "date": "2026-01-20", "amount": 30000, "memo": None, "cleared": "cleared",
         "flag_color": None, "flag_name": None, "account_id": "acc-card",
         "payee_name": "Transfer : Bank Current", "category_id": None, "category_name": None,
         "transfer_account_id": "acc-bank", "transfer_transaction_id": "t-pay", "deleted": False,
         "subtransactions": []},
    ]
    plans = [{"id": PLAN_ID, "name": "Household Plan", "last_modified_on": "2026-03-01T00:00:00Z",
              "first_month": "2026-01-01", "last_month": "2026-03-01",
              "currency_format": {"iso_code": "GBP", "decimal_digits": 2, "currency_symbol": "£"}}]
    return {
        "/plans": {"plans": plans},
        f"/plans/{PLAN_ID}/accounts": {"accounts": accounts},
        f"/plans/{PLAN_ID}/categories": {"category_groups": groups},
        # Asked for with a since_date, or YNAB answers only the last year.
        f"/plans/{PLAN_ID}/transactions?since_date={ynab_api.SINCE_DATE}": {"transactions": transactions},
    }


@pytest.fixture()
def fake_ynab(monkeypatch):
    world = _api_world()
    seen: list[str] = []

    def get(token, path):
        assert token == TOKEN
        seen.append(path)
        return world[path]

    monkeypatch.setattr(ynab_api, "get", get)
    return seen


def test_the_api_is_read_into_the_same_rows(fake_ynab):
    source = ynab_source.from_api(TOKEN, PLAN_ID)
    assert (source.via, source.plan_name, source.currency) == ("api", "Household Plan", "GBP")
    assert source.currency_confirmed_needed is False
    assert list(source.accounts) == ["acc-bank", "acc-card"]  # the deleted one is gone
    assert source.accounts["acc-card"].closed is True
    assert source.accounts["acc-card"].type_hint.value == "credit_card"
    assert [row.ref for row in source.rows] == ["t-shop", "s-1", "s-2", "t-pay", "t-paid"]
    assert [row.milliunits for row in source.rows] == [-12340, -20000, -5500, -30000, 30000]
    assert source.rows[0].flag == "Orange - REVIEW"
    assert source.rows[1].flag == "Blue"
    assert source.rows[1].payee == "Big Store"
    assert (source.rows[1].split, source.rows[1].category, source.rows[1].group) == (True, "Household", "Everyday")
    assert source.rows[3].transfer_ref == "t-paid"
    assert [engine.amount_of(row, "GBP", source) for row in source.rows] == [-1234, -2000, -550, -3000, 3000]


def test_milliunits_that_do_not_convert_exactly_are_refused(fake_ynab):
    source = ynab_source.from_api(TOKEN, PLAN_ID)
    source.rows[0].milliunits = -12345
    with pytest.raises(ValidationError, match="not a whole number"):
        engine.amount_of(source.rows[0], "GBP", source)
    assert engine.amount_of(source.rows[0], "BHD", source) == -12345
    source.rows[0].milliunits = -12000
    assert engine.amount_of(source.rows[0], "JPY", source) == -12


def test_an_api_import_pairs_by_transaction_id(session, owner, household, accounts, fake_ynab):
    source = ynab_source.from_api(TOKEN, PLAN_ID)
    plan = engine.Plan(
        currency="GBP", date_format="YYYY-MM-DD",
        accounts={
            "acc-bank": {"kind": "existing", "account_id": accounts["pounds"].id},
            "acc-card": {"kind": "create", "name": "Shop Card", "type": "credit_card"},
        },
        categories={"Groceries": {"kind": "create", "name": "Groceries"},
                    "Household": {"kind": "uncategorised"}},
        acknowledge_cleared_reset=True,
    )
    report = _run(session, household, owner, plan, source=source)
    assert report["counts"]["imported"] == 5
    assert report["counts"]["transfers_linked"] == 1
    pounds = _rows(session, accounts["pounds"].id)
    assert sorted(row.amount for row in pounds) == [-3000, -2000, -1234, -550]
    assert {row.import_source for row in pounds} == {"one-time-import:ynab-api"}
    assert {row.cleared for row in pounds} == {ClearedState.uncleared}
    assert "ynab:t-shop" in {row.import_id for row in pounds}
    card = _account(session, household, "Shop Card")
    paid = _rows(session, card.id)[0]
    pay = next(row for row in pounds if row.amount == -3000)
    assert (pay.transfer_transaction_id, paid.transfer_transaction_id) == (paid.id, pay.id)
    assert describing.describe(session, session.get(Batch, report["batch_id"])).headline == (
        "One-time Import · YNAB (API)"
    )


def test_the_whole_history_is_asked_for_not_ynabs_default_year(monkeypatch):
    """Without since_date YNAB's /transactions answers about a year: a live
    plan gave 1,101 rows where its export had 10,420 (2026-09-30)."""
    asked: list[str] = []
    monkeypatch.setattr(ynab_api, "get", lambda token, path: asked.append(path) or {"transactions": []})
    ynab_api.transactions(TOKEN, "plan/0001")
    assert asked == ["/plans/plan%2F0001/transactions?since_date=1970-01-01"]


def test_a_pair_found_from_one_side_is_not_also_reported_unpaired(
    session, owner, household, accounts, fake_ynab
):
    """The leg read first may point at an id that is not a row (YNAB points a
    split's transfer at the part, the part's mirror at the parent); the other
    leg still pairs with it, and then it is a linked transfer, not a note."""
    source = ynab_source.from_api(TOKEN, PLAN_ID)
    pay = next(row for row in source.rows if row.ref == "t-pay")
    pay.transfer_ref = "t-not-a-row"  # looked at first, and finds nobody
    assert [row.ref for row in source.rows].index("t-pay") < [row.ref for row in source.rows].index("t-paid")
    plan = engine.Plan(
        currency="GBP", date_format="YYYY-MM-DD",
        accounts={
            "acc-bank": {"kind": "existing", "account_id": accounts["pounds"].id},
            "acc-card": {"kind": "create", "name": "Shop Card", "type": "credit_card"},
        },
        categories={"Groceries": {"kind": "create", "name": "Groceries"},
                    "Household": {"kind": "uncategorised"}},
        acknowledge_cleared_reset=True,
    )
    report = _run(session, household, owner, plan, source=source)
    assert report["counts"]["transfers_linked"] == 1
    assert report["unpaired_transfers"] == []
    assert "(none)" in report["report_text"].split("Transfers imported as ordinary transactions:")[1]


# --------------------------------------------------------------------------- #
# The bank's own words (#265)
# --------------------------------------------------------------------------- #

#: Synthetic bank text, in the shape a card line arrives in: nobody's shop.
SHOP_BANK_TEXT = "CARD 0001 CORNER SHOP TESTVILLE"
SPLIT_BANK_TEXT = "BIG STORE 0042 TESTVILLE"
PART_BANK_TEXT = "BIG STORE ONLINE 0042"


def _world_with_bank_text(currency: str) -> dict[str, dict]:
    """The API world, in ``currency``, with the bank's text where YNAB had it.

    ``t-shop`` was imported by YNAB from the bank, and carries all three
    names and YNAB's own ``import_id``. ``t-cash`` was typed into YNAB by
    hand on the second account and carries none. The split's parent carries
    the bank's text, one part has its own and the other has none.
    """
    world = _api_world()
    world["/plans"]["plans"][0]["currency_format"]["iso_code"] = currency
    listed = world[f"/plans/{PLAN_ID}/transactions?since_date={ynab_api.SINCE_DATE}"]["transactions"]
    by_id = {one["id"]: one for one in listed}
    by_id["t-shop"].update(
        import_payee_name="Corner Shop Renamed",
        import_payee_name_original=SHOP_BANK_TEXT,
        import_id="YNAB:-12340:2026-01-10:1",
    )
    by_id["t-split"].update(
        import_payee_name="Big Store",
        import_payee_name_original=SPLIT_BANK_TEXT,
        import_id="YNAB:-25500:2026-01-15:1",
    )
    by_id["t-split"]["subtransactions"][1].update(import_payee_name_original=PART_BANK_TEXT)
    listed.append(
        {"id": "t-cash", "date": "2026-01-18", "amount": -4000, "memo": None, "cleared": "cleared",
         "flag_color": None, "flag_name": None, "account_id": "acc-card",
         "payee_name": "Market Stall", "category_id": "c1", "category_name": "Groceries",
         "transfer_account_id": None, "transfer_transaction_id": None, "deleted": False,
         "import_payee_name": None, "import_payee_name_original": None, "import_id": None,
         "subtransactions": []}
    )
    return world


def _serve(monkeypatch, world: dict[str, dict]) -> None:
    monkeypatch.setattr(ynab_api, "get", lambda token, path: world[path])


def test_the_api_reads_the_banks_text_and_ynabs_import_id(monkeypatch):
    _serve(monkeypatch, _world_with_bank_text("GBP"))
    rows = {row.ref: row for row in ynab_source.from_api(TOKEN, PLAN_ID).rows}

    assert (rows["t-shop"].payee, rows["t-shop"].payee_original) == ("Corner Shop", SHOP_BANK_TEXT)
    assert rows["t-shop"].source_import_id == "YNAB:-12340:2026-01-10:1"
    # A part has no bank line of its own: it takes its parent's, unless it
    # says otherwise.
    assert (rows["s-1"].payee, rows["s-1"].payee_original) == ("Big Store", SPLIT_BANK_TEXT)
    assert rows["s-2"].payee_original == PART_BANK_TEXT
    assert rows["s-1"].source_import_id == rows["s-2"].source_import_id == "YNAB:-25500:2026-01-15:1"
    # Typed into YNAB by hand, and a transfer: no bank text, no import id.
    for ref in ("t-cash", "t-pay", "t-paid"):
        assert (rows[ref].payee_original, rows[ref].source_import_id) == (None, None)


@pytest.mark.parametrize(("currency", "target"), [("GBP", "pounds"), ("EUR", "checking")])
def test_an_api_import_stores_the_banks_text_not_ynabs_clean_name(
    session, owner, household, accounts, monkeypatch, currency, target
):
    _serve(monkeypatch, _world_with_bank_text(currency))
    source = ynab_source.from_api(TOKEN, PLAN_ID)
    plan = engine.Plan(
        currency=currency, date_format="YYYY-MM-DD",
        accounts={
            "acc-bank": {"kind": "existing", "account_id": accounts[target].id},
            "acc-card": {"kind": "create", "name": "Shop Card", "type": "credit_card"},
        },
        categories={"Groceries": {"kind": "create", "name": "Groceries"},
                    "Household": {"kind": "uncategorised"}},
        acknowledge_cleared_reset=True,
    )
    report = _run(session, household, owner, plan, source=source)
    assert report["counts"]["imported"] == 6

    bank = {row.amount: row for row in _rows(session, accounts[target].id)}
    assert {row.account.currency for row in bank.values()} == {currency}
    shop, towels, bread = bank[-1234], bank[-2000], bank[-550]
    assert (shop.import_payee_original, shop.payee.name) == (SHOP_BANK_TEXT, "Corner Shop")
    assert (towels.import_payee_original, towels.payee.name) == (SPLIT_BANK_TEXT, "Big Store")
    assert (bread.import_payee_original, bread.payee.name) == (PART_BANK_TEXT, "Big Store")
    assert bank[-3000].import_payee_original is None  # a transfer leg

    card = {row.amount: row for row in _rows(session, _account(session, household, "Shop Card").id)}
    assert (card[-400].import_payee_original, card[-400].payee.name) == (None, "Market Stall")
    assert card[3000].import_payee_original is None

    # Three rows, but two bank lines -- the shop and the split, counted once --
    # brought the bank's words, and the report points at what they are for.
    assert report["bank_text_rows"] == 2
    assert "Bank text kept for 2 transactions" in report["report_text"]
    assert "Payee Naming Rules" in report["report_text"]


#: Two splits of one bank line across two payees: the market hall's stalls.
#: Two references, so `suggest_rules` would group the strings if it saw them.
HALL_FIRST, HALL_SECOND = "MARKET HALL 4471", "MARKET HALL 9902"


def _world_with_split_payees() -> dict[str, dict]:
    """``t-m1``: the parent has no payee, and each part names its own stall.
    ``t-m2``: the parent is the fish stall, its first part keeps that payee
    and its second is the bread stall's."""
    world = _api_world()

    def part(ref, parent, amount, payee):
        return {"id": ref, "transaction_id": parent, "amount": amount, "memo": None,
                "payee_name": payee, "category_id": "c1", "category_name": "Groceries",
                "transfer_account_id": None, "transfer_transaction_id": None, "deleted": False}

    def split(ref, day, payee, text, parts):
        return {"id": ref, "date": day, "amount": sum(one["amount"] for one in parts), "memo": None,
                "cleared": "cleared", "flag_color": None, "flag_name": None, "account_id": "acc-bank",
                "payee_name": payee, "import_payee_name": payee, "import_payee_name_original": text,
                "import_id": f"YNAB:{sum(one['amount'] for one in parts)}:{day}:1",
                "category_id": None, "category_name": "Split", "transfer_account_id": None,
                "transfer_transaction_id": None, "deleted": False, "subtransactions": parts}

    world[f"/plans/{PLAN_ID}/transactions?since_date={ynab_api.SINCE_DATE}"]["transactions"] = [
        split("t-m1", "2026-02-01", None, HALL_FIRST, [
            part("m1-fish", "t-m1", -30000, "Fish Stall"),
            part("m1-bread", "t-m1", -20000, "Bread Stall"),
        ]),
        split("t-m2", "2026-02-08", "Fish Stall", HALL_SECOND, [
            part("m2-fish", "t-m2", -40000, None),
            part("m2-bread", "t-m2", -10000, "Bread Stall"),
        ]),
    ]
    return world


@pytest.mark.parametrize(("currency", "target"), [("GBP", "pounds"), ("EUR", "checking")])
def test_a_split_across_payees_keeps_bank_text_only_on_the_parents_payee(
    session, owner, household, accounts, monkeypatch, currency, target
):
    world = _world_with_split_payees()
    world["/plans"]["plans"][0]["currency_format"]["iso_code"] = currency
    _serve(monkeypatch, world)
    source = ynab_source.from_api(TOKEN, PLAN_ID)
    assert {row.ref: row.payee_original for row in source.rows} == {
        "m1-fish": None, "m1-bread": None,  # the parent named nobody
        "m2-fish": HALL_SECOND, "m2-bread": None,
    }
    assert {row.ref: row.parent_ref for row in source.rows} == {
        "m1-fish": "t-m1", "m1-bread": "t-m1", "m2-fish": "t-m2", "m2-bread": "t-m2",
    }

    plan = engine.Plan(
        currency=currency, date_format="YYYY-MM-DD",
        accounts={"acc-bank": {"kind": "existing", "account_id": accounts[target].id},
                  "acc-card": {"kind": "skip"}},
        categories={"Groceries": {"kind": "create", "name": "Groceries"}},
        acknowledge_cleared_reset=True,
    )
    report = _run(session, household, owner, plan, source=source)
    assert report["counts"]["imported"] == 4
    stored = {
        row.amount: (row.payee.name, row.import_payee_original)
        for row in _rows(session, accounts[target].id)
    }
    assert stored == {
        -3000: ("Fish Stall", None), -2000: ("Bread Stall", None),
        -4000: ("Fish Stall", HALL_SECOND), -1000: ("Bread Stall", None),
    }
    assert report["bank_text_rows"] == 1

    # One bank string under two payees is what `suggest_rules` offers to fold
    # into one. Nothing here may look like that.
    assert payee_service.suggest_rules(session, household.id) == []


def test_the_split_shapes_would_have_fed_suggest_rules_a_false_group(session, owner, household, accounts):
    """The control for the test above: the same bank strings stored the old way,
    on every part, are a suggestion -- so its empty list is the fix, not a
    grouping that could never fire."""
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        for amount, payee, text in [
            (-3000, "Fish Stall", HALL_FIRST), (-2000, "Bread Stall", HALL_FIRST),
            (-4000, "Fish Stall", HALL_SECOND), (-1000, "Bread Stall", HALL_SECOND),
        ]:
            transaction_service.create(
                session, account=accounts["pounds"], date=date(2026, 2, 1), amount=amount,
                payee=payee_service.get_or_create(session, household.id, payee),
                import_payee_original=text,
            )
    session.commit()
    found = payee_service.suggest_rules(session, household.id)
    assert [(one.pattern, one.payees, one.strings) for one in found] == [("MARKET HALL", 2, 2)]


def test_an_api_import_without_bank_text_says_nothing_about_rules(
    session, owner, household, accounts, fake_ynab
):
    source = ynab_source.from_api(TOKEN, PLAN_ID)
    plan = engine.Plan(
        currency="GBP", date_format="YYYY-MM-DD",
        accounts={
            "acc-bank": {"kind": "existing", "account_id": accounts["pounds"].id},
            "acc-card": {"kind": "create", "name": "Shop Card", "type": "credit_card"},
        },
        categories={"Groceries": {"kind": "create", "name": "Groceries"},
                    "Household": {"kind": "uncategorised"}},
        acknowledge_cleared_reset=True,
    )
    report = _run(session, household, owner, plan, source=source)
    pounds = _rows(session, accounts["pounds"].id)
    assert len(pounds) == 4
    assert {row.import_payee_original for row in pounds} == {None}
    assert next(row for row in pounds if row.amount == -1234).payee.name == "Corner Shop"
    assert report["bank_text_rows"] == 0
    assert "Payee Naming Rules" not in report["report_text"]


def test_a_register_csv_has_no_bank_text_so_none_is_stored(session, owner, household, accounts):
    """The Register holds YNAB's clean names only. Storing one as the bank's
    text would feed `suggest_rules` names no bank ever sends."""
    source = _source()
    assert {row.payee_original for row in source.rows} == {None}
    assert {row.source_import_id for row in source.rows} == {None}

    report = _run(session, household, owner, _plan(accounts), source=source)
    pot = _account(session, household, "Rainy Day Pot")
    pounds = _rows(session, accounts["pounds"].id)
    both = pounds + _rows(session, pot.id)
    assert len(both) == 10
    assert {row.import_payee_original for row in both} == {None}
    shop = next(row for row in pounds if row.amount == -1234)
    assert shop.payee.name == "Corner Shop"
    assert report["bank_text_rows"] == 0
    assert "Payee Naming Rules" not in report["report_text"]


# --------------------------------------------------------------------------- #
# Checked against YNAB's own balance (#266)
# --------------------------------------------------------------------------- #

_TRANSACTIONS = f"/plans/{PLAN_ID}/transactions?since_date={ynab_api.SINCE_DATE}"
_ACCOUNTS = f"/plans/{PLAN_ID}/accounts"


def _world_with_balances(currency: str = "GBP", scale: int = 1) -> dict[str, dict]:
    """The API world in ``currency``, each account carrying YNAB's balance.

    ``scale`` multiplies every milliunit figure, so the same rows can be whole
    yen. At ``scale=1`` the bank's rows are -12,340 + -20,000 + -5,500 +
    -30,000 = -67,840 milliunits (the deleted row and part are not in it), of
    which the reconciled and cleared ones make -37,840; the card's one row is
    +30,000.
    """
    world = _api_world()
    world["/plans"]["plans"][0]["currency_format"]["iso_code"] = currency
    for one in world[_TRANSACTIONS]["transactions"]:
        one["amount"] *= scale
        for part in one["subtransactions"]:
            part["amount"] *= scale
    figures = {"acc-bank": (-67840, -37840), "acc-card": (30000, 30000), "acc-gone": (0, 0)}
    for account in world[_ACCOUNTS]["accounts"]:
        balance, cleared = figures[account["id"]]
        account.update(balance=balance * scale, cleared_balance=cleared * scale)
    return world


def _balance_plan(currency: str, bank: dict, **over) -> engine.Plan:
    fields = {
        "currency": currency,
        "date_format": "YYYY-MM-DD",
        "accounts": {
            "acc-bank": bank,
            "acc-card": {"kind": "create", "name": "Shop Card", "type": "credit_card"},
        },
        "categories": {"Groceries": {"kind": "create", "name": "Groceries"},
                       "Household": {"kind": "uncategorised"}},
        "acknowledge_cleared_reset": True,
    }
    fields.update(over)
    return engine.Plan(**fields)


def _differences(report: dict) -> list[tuple]:
    # Through the response schema, so the field is proved to reach the client.
    shown = schemas.OneTimeImportReport.model_validate(report).balance_differences
    return [
        (one.account, one.currency, one.ynab_balance_minor, one.imported_minor, one.difference_minor,
         one.sentence)
        for one in shown
    ]


def test_the_api_reads_each_accounts_balance_and_a_csv_has_none(monkeypatch):
    _serve(monkeypatch, _world_with_balances())
    accounts = ynab_source.from_api(TOKEN, PLAN_ID).accounts
    assert (accounts["acc-bank"].balance_milliunits, accounts["acc-bank"].cleared_balance_milliunits) == (
        -67840, -37840
    )
    assert (accounts["acc-card"].balance_milliunits, accounts["acc-card"].cleared_balance_milliunits) == (
        30000, 30000
    )
    assert {(one.balance_milliunits, one.cleared_balance_milliunits) for one in _source().accounts.values()} == {
        (None, None)
    }


@pytest.mark.parametrize(
    ("date_to", "out_of_range"), [(None, 0), (date(2026, 1, 15), 2)], ids=["whole", "range"]
)
def test_an_import_that_adds_up_says_nothing(
    session, owner, household, accounts, monkeypatch, date_to, out_of_range
):
    """Rows outside the range are counted in: a balance needs no full range."""
    _serve(monkeypatch, _world_with_balances())
    plan = _balance_plan("GBP", {"kind": "existing", "account_id": accounts["pounds"].id}, date_to=date_to)
    report = _run(session, household, owner, plan, source=ynab_source.from_api(TOKEN, PLAN_ID))

    assert report["counts"]["skipped_date_range"] == out_of_range
    assert report["counts"]["imported"] == 5 - out_of_range
    assert sum(row.amount for row in _rows(session, accounts["pounds"].id)) == -6784 + 3000 * (out_of_range // 2)
    assert _differences(report) == []
    assert report["balance_unchecked"] == []
    assert "YNAB's balance" not in report["report_text"]


def test_a_skipped_duplicate_and_starting_balance_are_counted_in(
    session, owner, household, accounts, monkeypatch
):
    """The account held nothing in the range, only a row the day before it,
    which an incoming row duplicates. The duplicate, the starting balance and
    the row before the range are left out on purpose, so they are counted."""
    world = _world_with_balances()
    world[_TRANSACTIONS]["transactions"].append(
        {"id": "t-start", "date": "2026-01-14", "amount": 100000, "memo": None, "cleared": "reconciled",
         "flag_color": None, "flag_name": None, "account_id": "acc-bank", "payee_name": "Starting Balance",
         "category_id": None, "category_name": None, "transfer_account_id": None,
         "transfer_transaction_id": None, "deleted": False, "subtransactions": []}
    )
    world[_ACCOUNTS]["accounts"][0]["balance"] = -67840 + 100000
    _serve(monkeypatch, world)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        transaction_service.create(
            session, account=accounts["pounds"], date=date(2026, 1, 13), amount=-2000, category=None
        )
    session.commit()

    plan = _balance_plan(
        "GBP", {"kind": "existing", "account_id": accounts["pounds"].id},
        date_from=date(2026, 1, 14), starting_balance="skip",
    )
    report = _run(session, household, owner, plan, source=ynab_source.from_api(TOKEN, PLAN_ID))

    assert report["counts"]["duplicates_skipped"] == 1
    assert report["counts"]["skipped_starting_balance"] == 1
    assert report["counts"]["skipped_date_range"] == 1
    assert sorted(row.amount for row in _rows(session, accounts["pounds"].id)) == [-3000, -2000, -550]
    assert _differences(report) == []


@pytest.mark.parametrize(
    ("currency", "target", "sign"), [("GBP", "pounds", "£"), ("EUR", "checking", "€")]
)
def test_a_row_that_failed_is_a_sentence_with_both_figures(
    session, owner, household, accounts, monkeypatch, currency, target, sign
):
    _serve(monkeypatch, _world_with_balances(currency))
    real = transaction_service.create

    def refusing(session, **kwargs):
        if kwargs.get("amount") == -550:
            raise ValidationError("refused for the test")
        return real(session, **kwargs)

    monkeypatch.setattr(transaction_service, "create", refusing)
    plan = _balance_plan(currency, {"kind": "existing", "account_id": accounts[target].id})
    source = ynab_source.from_api(TOKEN, PLAN_ID)

    for commit in (False, True):  # the preview says it too
        report = _run(session, household, owner, plan, source=source, commit=commit)
        assert report["counts"]["failed"] == 1
        sentence = (
            f"Bank Current: YNAB's balance is -{sign}67.84, and this import accounts for "
            f"-{sign}62.34, {sign}5.50 more."
        )
        # The card adds up, so only the bank is named.
        assert _differences(report) == [("Bank Current", currency, -6784, -6234, 550, sentence)]
        assert f"  {sentence}\n" in report["report_text"]
    assert sum(row.amount for row in _rows(session, accounts[target].id)) == -6234


def test_yen_are_compared_in_whole_yen(session, owner, household, accounts, monkeypatch):
    """JPY has no minor unit: 1,000 milliunits is one yen, not ten."""
    world = _world_with_balances("JPY", scale=100)
    by_id = {one["id"]: one for one in world[_TRANSACTIONS]["transactions"]}
    by_id["t-shop"]["date"] = "2026-02-30"  # fails: no such day
    _serve(monkeypatch, world)
    plan = _balance_plan("JPY", {"kind": "create", "name": "Yen Current", "type": "checking"})
    report = _run(session, household, owner, plan, source=ynab_source.from_api(TOKEN, PLAN_ID))

    assert report["counts"]["failed"] == 1
    yen = _account(session, household, "Yen Current")
    assert (yen.currency, sum(row.amount for row in _rows(session, yen.id))) == ("JPY", -5550)
    assert _differences(report) == [
        ("Bank Current", "JPY", -6784, -5550, 1234,
         "Bank Current: YNAB's balance is -¥6,784, and this import accounts for -¥5,550, ¥1,234 more.")
    ]


def test_an_account_with_its_own_history_is_checked_too(session, owner, household, accounts, monkeypatch):
    """Holding a row of its own already, it is checked all the same, and on
    YNAB's rows alone: the ledger's -12.34 does not hide the row that YNAB
    counts in its balance and this import never received."""
    _serve(monkeypatch, _world_with_balances())
    _already_there(session, household, owner, accounts)  # 2026-01-11, no range: in it
    world_rows = ynab_source.from_api(TOKEN, PLAN_ID)
    world_rows.rows = [row for row in world_rows.rows if row.ref != "t-shop"]  # lost on the way
    plan = _balance_plan("GBP", {"kind": "existing", "account_id": accounts["pounds"].id})
    report = _run(session, household, owner, plan, source=world_rows)

    assert report["counts"]["imported"] == 4
    assert sum(row.amount for row in _rows(session, accounts["pounds"].id)) == -1234 - 5550
    assert [one[:5] for one in _differences(report)] == [("Bank Current", "GBP", -6784, -5550, 1234)]

    # The same loss into a created account is named the same way.
    created = _balance_plan("GBP", {"kind": "create", "name": "Bank Current", "type": "checking"},
                            accounts={"acc-bank": {"kind": "create", "name": "Bank Current", "type": "checking"},
                                      "acc-card": {"kind": "skip"}})
    again = _run(session, household, owner, created, source=world_rows, commit=False)
    assert [one[:5] for one in _differences(again)] == [("Bank Current", "GBP", -6784, -5550, 1234)]


def test_a_repeat_import_into_the_same_accounts_still_adds_up(
    session, owner, household, accounts, monkeypatch
):
    """The second run finds every row already imported. Those are skipped as
    duplicates, which are counted in, so a full account says nothing."""
    _serve(monkeypatch, _world_with_balances())
    plan = _balance_plan("GBP", {"kind": "existing", "account_id": accounts["pounds"].id})
    _run(session, household, owner, plan, source=ynab_source.from_api(TOKEN, PLAN_ID))
    card = _account(session, household, "Shop Card")
    again = _balance_plan("GBP", {"kind": "existing", "account_id": accounts["pounds"].id},
                          accounts={"acc-bank": {"kind": "existing", "account_id": accounts["pounds"].id},
                                    "acc-card": {"kind": "existing", "account_id": card.id}})
    report = _run(session, household, owner, again, source=ynab_source.from_api(TOKEN, PLAN_ID))

    assert report["counts"]["imported"] == 0
    assert report["counts"]["duplicates_skipped"] == 5
    assert sum(row.amount for row in _rows(session, accounts["pounds"].id)) == -6784
    assert _differences(report) == []
    assert report["balance_unchecked"] == []


def test_a_balance_the_currency_cannot_hold_is_said_not_skipped(
    session, owner, household, accounts, monkeypatch
):
    """12,345 thousandths is not a whole number of pence: no exact figure to
    compare, so the report says the balance went unchecked, and why."""
    world = _world_with_balances()
    world[_ACCOUNTS]["accounts"][0]["balance"] = -67845
    _serve(monkeypatch, world)
    plan = _balance_plan("GBP", {"kind": "existing", "account_id": accounts["pounds"].id})
    report = _run(session, household, owner, plan, source=ynab_source.from_api(TOKEN, PLAN_ID))

    assert sum(row.amount for row in _rows(session, accounts["pounds"].id)) == -6784
    assert _differences(report) == []  # the card adds up; the bank has no figure to compare
    shown = schemas.OneTimeImportReport.model_validate(report).balance_unchecked
    sentence = (
        "Bank Current: YNAB's balance could not be checked: "
        "-67845 thousandths is not a whole number of GBP minor units."
    )
    assert [(one.account_key, one.account, one.sentence) for one in shown] == [
        ("acc-bank", "Bank Current", sentence)
    ]
    assert f"  {sentence}\n" in report["report_text"]


def test_a_register_csv_has_no_balance_so_nothing_is_said(session, owner, household, accounts):
    rows = [*ROWS, _row("Bank Current", "2026-03-02", "Shop", out="£1.234")]
    report = _run(session, household, owner, _plan(accounts), source=_source(_register(rows)))
    assert report["counts"]["failed"] == 1
    assert _differences(report) == []
    assert report["balance_unchecked"] == []
    assert "YNAB's balance" not in report["report_text"]


# --------------------------------------------------------------------------- #
# Over HTTP: the doors, who may use them, and the token
# --------------------------------------------------------------------------- #


def _house(client) -> tuple[str, str]:
    _setup_owner(client)
    house = client.post(
        "/api/households", json={"name": "Doe", "base_currency": "GBP"}, headers=HEADERS
    ).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Bank Current", "type": "checking"},
        headers=HEADERS,
    ).json()
    return house["id"], account["id"]


def _http_plan(account_id: str) -> str:
    return json.dumps({
        "currency": "GBP",
        "date_format": "YYYY-MM-DD",
        "accounts": {
            "Bank Current": {"kind": "existing", "account_id": account_id},
            "Rainy Day Pot": {"kind": "create", "name": "Rainy Day Pot", "type": "savings"},
            "Old Card": {"kind": "skip"},
        },
        "categories": {
            "Groceries": {"kind": "create", "name": "Groceries"},
            "Salary": {"kind": "create", "name": "Salary"},
            "Household": {"kind": "uncategorised"},
        },
        "flags": "memo",
        "starting_balance": "import",
        "acknowledge_cleared_reset": True,
        "duplicates": {"all": None, "import": []},
    })


def test_over_http_analyse_preview_and_commit_a_file(client):
    house_id, account_id = _house(client)
    base = f"/api/households/{house_id}/one-time-import/ynab"
    upload = {"file": ("export.zip", _zip(**{"B - Register.csv": _register(), "B - Plan.csv": PLAN_CSV}), "application/zip")}

    found = client.post(f"{base}/analyse", data={"via": "csv"}, files=upload, headers=HEADERS)
    assert found.status_code == 200, found.text
    assert found.json()["totals"]["rows"] == 11
    assert found.json()["accounts"][0]["suggestion"]["account_id"] == account_id

    preview = client.post(
        f"{base}/preview", data={"via": "csv", "plan": _http_plan(account_id)}, files=upload, headers=HEADERS
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["committed"] is False
    assert preview.json()["counts"]["imported"] == 10

    done = client.post(
        f"{base}/commit", data={"via": "csv", "plan": _http_plan(account_id)}, files=upload, headers=HEADERS
    )
    assert done.status_code == 201, done.text
    assert done.json()["committed"] is True
    listed = client.get(f"/api/households/{house_id}/accounts", headers=HEADERS).json()
    assert {one["name"] for one in listed} == {"Bank Current", "Rainy Day Pot"}

    history = client.get(f"/api/households/{house_id}/batches", headers=HEADERS).json()
    assert history[0]["headline"] == "One-time Import · YNAB (CSV)"

    plan_only = client.post(
        f"{base}/analyse", data={"via": "csv"},
        files={"file": ("Plan.csv", PLAN_CSV, "text/csv")}, headers=HEADERS,
    )
    assert plan_only.status_code == 422
    assert "Only the YNAB Register.csv is needed, not the Plan.csv" in plan_only.text


def test_the_token_is_never_kept_or_echoed(client, fake_ynab):
    house_id, account_id = _house(client)
    base = f"/api/households/{house_id}/one-time-import/ynab"
    plans = client.post(f"{base}/plans", json={"token": TOKEN}, headers=HEADERS)
    assert plans.status_code == 200, plans.text
    assert plans.json()["plans"][0] == {
        "id": PLAN_ID, "name": "Household Plan", "currency": "GBP",
        "last_modified_on": "2026-03-01T00:00:00Z", "first_month": "2026-01-01", "last_month": "2026-03-01",
    }

    plan = json.dumps({
        "currency": "GBP", "date_format": "YYYY-MM-DD",
        "accounts": {"acc-bank": {"kind": "existing", "account_id": account_id},
                     "acc-card": {"kind": "create", "name": "Shop Card", "type": "credit_card"}},
        "categories": {"Groceries": {"kind": "create", "name": "Groceries"},
                       "Household": {"kind": "uncategorised"}},
        "acknowledge_cleared_reset": True,
    })
    done = client.post(
        f"{base}/commit", data={"via": "api", "token": TOKEN, "plan_id": PLAN_ID, "plan": plan}, headers=HEADERS
    )
    assert done.status_code == 201, done.text
    assert done.json()["counts"]["imported"] == 5
    assert TOKEN not in done.text

    from app.db import SessionLocal

    with SessionLocal() as session:
        stored = [
            json.dumps([row.source, row.summary], default=str)
            for row in session.execute(select(Batch)).scalars()
        ] + [
            json.dumps([row.before, row.after], default=str)
            for row in session.execute(select(Change)).scalars()
        ]
    assert stored, "nothing was written, so the check below is vacuous"
    assert not [one for one in stored if TOKEN in one]
    assert any('"plan_name": "Household Plan"' in one for one in stored)


def test_a_rejected_token_says_so_without_repeating_it(client, monkeypatch, caplog):
    house_id, _ = _house(client)

    def refuse(request, **kwargs):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(ynab_api, "_open", refuse)
    answer = client.post(
        f"/api/households/{house_id}/one-time-import/ynab/plans", json={"token": TOKEN}, headers=HEADERS
    )
    assert answer.status_code == 422
    assert answer.json()["detail"] == "YNAB rejected the token"
    assert TOKEN not in answer.text
    assert TOKEN not in caplog.text


def test_the_token_does_not_follow_a_redirect(monkeypatch):
    """urllib's own redirect handler copies Authorization to whatever host the
    Location names, over http if it says so (#219). The second server must see
    nothing at all."""
    from tests.test_outbound import body, local_server, redirect_to

    with (
        local_server(body(b'{"data": {"plans": []}}')) as (elsewhere, reached),
        local_server(redirect_to(elsewhere)) as (redirector, asked),
    ):
        monkeypatch.setattr(ynab_api, "BASE", f"{redirector}/v1")
        with pytest.raises(ynab_api.YnabError) as refused:
            ynab_api.plans(TOKEN)
    assert len(asked) == 1
    assert asked[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"  # it was sent here
    assert reached == []
    assert str(refused.value) == "YNAB answered 302; try again later"
    assert TOKEN not in str(refused.value)


def test_a_dripping_answer_is_cut_off_at_the_deadline(monkeypatch):
    """TIMEOUT_SECONDS bounds one recv; a byte every so often never trips it."""
    import time

    from tests.test_outbound import drip, local_server

    monkeypatch.setattr(ynab_api, "DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(ynab_api, "TIMEOUT_SECONDS", 2)
    with local_server(drip(every=0.05, total=10_000)) as (server, _):
        monkeypatch.setattr(ynab_api, "BASE", f"{server}/v1")
        started = time.monotonic()
        with pytest.raises(ynab_api.YnabError, match="took too long"):
            ynab_api.plans(TOKEN)
        took = time.monotonic() - started
    assert took < 3, took


def test_an_oversized_answer_is_refused_before_it_is_parsed(monkeypatch):
    from tests.test_outbound import body, local_server

    monkeypatch.setattr(ynab_api, "MAX_RESPONSE_BYTES", 1024)
    with local_server(body(b" " * 4096)) as (server, _):
        monkeypatch.setattr(ynab_api, "BASE", f"{server}/v1")
        with pytest.raises(ynab_api.YnabError, match="larger than this import will read"):
            ynab_api.plans(TOKEN)


def _hostile(path_end: str, change) -> dict:
    world = _api_world()
    key = next(key for key in world if key.split("?")[0].endswith(path_end))
    world[key] = change(json.loads(json.dumps(world[key])))
    return world


def _set(where: str, value):
    def change(answer):
        target = answer
        *steps, last = where.split(".")
        for step in steps:
            target = target[int(step)] if step.isdigit() else target[step]
        if value is KeyError:
            del target[last]
        else:
            target[last] = value
        return answer

    return change


HOSTILE_SHAPES = {
    "amount as text": ("/transactions", _set("transactions.0.amount", "1000")),
    "amount as a boolean": ("/transactions", _set("transactions.0.amount", True)),
    "amount as a float": ("/transactions", _set("transactions.0.amount", 12.5)),
    "transactions as numbers": ("/transactions", _set("transactions", [1])),
    "transactions as an object": ("/transactions", _set("transactions", {})),
    "transactions as a number": ("/transactions", _set("transactions", 7)),
    "date as a number": ("/transactions", _set("transactions.0.date", 20260101)),
    "a part with no id": ("/transactions", _set("transactions.2.subtransactions.0.id", KeyError)),
    "a transaction with no account": ("/transactions", _set("transactions.0.account_id", KeyError)),
    "an account id as a number": ("/accounts", _set("accounts.0.id", 1)),
    "a balance as text": ("/accounts", _set("accounts.0.balance", "1000")),
    "a balance as a float": ("/accounts", _set("accounts.0.balance", 12.5)),
    "a cleared balance as a boolean": ("/accounts", _set("accounts.0.cleared_balance", True)),
    "categories as text": ("/categories", _set("category_groups.0.categories", "abc")),
    "plans as text": ("/plans", _set("plans", "abc")),
    "a plan as a list": ("/plans", _set("plans", [list((PLAN_ID,))])),
}


@pytest.mark.parametrize("shape", HOSTILE_SHAPES)
def test_a_hostile_api_shape_is_a_sentence_not_a_500(client, monkeypatch, shape):
    """Each of these raised TypeError, KeyError or AttributeError past the
    engine's `except DomainError` (#223)."""
    house_id, _ = _house(client)
    path_end, change = HOSTILE_SHAPES[shape]
    world = _hostile(path_end, change)
    monkeypatch.setattr(ynab_api, "get", lambda token, path: world[path])
    answer = client.post(
        f"/api/households/{house_id}/one-time-import/ynab/analyse",
        data={"via": "api", "token": TOKEN, "plan_id": PLAN_ID},
        headers=HEADERS,
    )
    assert answer.status_code == 422, answer.text
    assert answer.json()["detail"] == "YNAB's answer could not be read"
    listed = client.post(
        f"/api/households/{house_id}/one-time-import/ynab/plans", json={"token": TOKEN}, headers=HEADERS
    )
    if path_end == "/plans":
        assert listed.status_code == 422, listed.text
        assert listed.json()["detail"] == "YNAB's answer could not be read"
    else:
        assert listed.status_code == 200, listed.text
        assert [one["id"] for one in listed.json()["plans"]] == [PLAN_ID]


def test_the_untouched_world_still_analyses(client, fake_ynab):
    """The control for the test above: the same door, a well-formed answer."""
    house_id, _ = _house(client)
    answer = client.post(
        f"/api/households/{house_id}/one-time-import/ynab/analyse",
        data={"via": "api", "token": TOKEN, "plan_id": PLAN_ID},
        headers=HEADERS,
    )
    assert answer.status_code == 200, answer.text


def test_json_nested_past_the_parser_is_a_sentence(monkeypatch):
    """`json.loads` raises RecursionError, not ValueError, on `[[[[...`."""
    from tests.test_outbound import body, local_server

    with local_server(body(b"[" * 200_000)) as (server, _):
        monkeypatch.setattr(ynab_api, "BASE", f"{server}/v1")
        with pytest.raises(ynab_api.YnabError, match="could not be read"):
            ynab_api.plans(TOKEN)


def test_a_token_too_long_is_refused_without_being_echoed(client):
    """The schema constraint made FastAPI's own 422 put the token in `input` (#224)."""
    house_id, _ = _house(client)
    long_token = "secret-" + "t" * 593
    answer = client.post(
        f"/api/households/{house_id}/one-time-import/ynab/plans",
        json={"token": long_token},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    assert answer.json()["detail"] == "that is not a YNAB personal access token"
    assert "secret-" not in answer.text


def test_no_schema_error_echoes_the_value_that_failed(client):
    """The app-wide handler: the error says where and why, never what (#224)."""
    house_id, _ = _house(client)
    answer = client.post(
        f"/api/households/{house_id}/one-time-import/ynab/plans",
        json={"token": ["secret-in-a-list-0123"]},
        headers=HEADERS,
    )
    assert answer.status_code == 422
    errors = answer.json()["detail"]
    assert [error["loc"] for error in errors] == [["body", "token"]]
    assert all("input" not in error for error in errors)
    assert "secret-in-a-list-0123" not in answer.text


def test_a_register_csv_error_names_the_line_not_the_csv_module(caplog):
    raw = _register([*ROWS, _row("Bank Current", "2026-03-02", "Shop", memo="n" * 140_000)])
    with caplog.at_level("DEBUG"), pytest.raises(ValidationError) as refused:
        _source(raw)
    assert str(refused.value).startswith("line 13 of this file cannot be read as CSV.")
    assert "field limit" not in str(refused.value)
    assert any("field limit" in str(record.exc_info[1]) for record in caplog.records if record.exc_info)


def test_only_the_owner_and_never_a_key_reaches_these_doors(client, member):
    """Owner-only by `require_owner`, and a cookie door only -- the floor test
    in test_agent_access walks the same table for `current_agent`."""
    with pytest.raises(Forbidden):
        deps.require_owner(member)

    from tests.test_agent_access import _api_routes, _dependencies_of

    ours = [route for route in _api_routes(client.app_module.app) if "/one-time-import/" in route.served_path]
    assert len(ours) == 5
    for route in ours:
        needs = _dependencies_of(route)
        # The list of imports already done is every member's, like History.
        if route.served_path.endswith("/one-time-import/history"):
            assert deps.require_owner not in needs, route.served_path
        else:
            assert deps.require_owner in needs, route.served_path
        assert deps.current_household in needs, route.served_path
        assert deps.current_agent not in needs, route.served_path


# --------------------------------------------------------------------------- #
# Where a row came from, and what has been imported before
# --------------------------------------------------------------------------- #


def test_the_batch_that_created_a_row_is_found_only_for_rows_it_created(
    session, owner, household, accounts, other_household
):
    already = _already_there(session, household, owner, accounts)
    report = _run(session, household, owner, _plan(accounts))
    salary = next(one for one in _rows(session, accounts["pounds"].id) if one.amount == 250000)

    found = engine.batch_that_created(session, salary.id)
    assert found is not None and found.id == report["batch_id"]
    # A row that was there first -- which the import then skipped as a
    # duplicate of its own -- did not come from it.
    assert report["counts"]["duplicates_skipped"] == 1
    assert engine.batch_that_created(session, already.id) is None


def test_the_list_of_earlier_imports_covers_every_workflow_and_marks_an_undo(
    session, owner, household, accounts, other_household
):
    assert engine.previous_imports(session, household, workflow=None) == []
    first = _run(session, household, owner, _plan(accounts))
    undo_batch(session, first["batch_id"], actor_id=owner.id)
    session.commit()
    second = _run(session, household, owner, _plan(accounts))

    listed = engine.previous_imports(session, household, workflow=None)
    assert [(one["batch_id"], one["status"], one["workflow"]) for one in listed] == [
        (second["batch_id"], "applied", "ynab"),
        (first["batch_id"], "undone", "ynab"),
    ]
    assert engine.previous_imports(session, other_household, workflow=None) == []


def test_over_http_a_one_time_row_says_where_it_came_from(client):
    house_id, account_id = _house(client)
    base = f"/api/households/{house_id}/one-time-import"
    upload = {"file": ("export.zip", _zip(**{"B - Register.csv": _register()}), "application/zip")}

    typed = client.post(
        f"/api/households/{house_id}/transactions",
        json={"account_id": account_id, "date": "2026-01-05", "amount": -500},
        headers=HEADERS,
    ).json()

    assert client.get(f"{base}/history", headers=HEADERS).json() == {"imports": []}

    done = client.post(
        f"{base}/ynab/commit", data={"via": "csv", "plan": _http_plan(account_id)},
        files=upload, headers=HEADERS,
    )
    assert done.status_code == 201, done.text
    batch_id = done.json()["batch_id"]

    rows = client.get(f"/api/households/{house_id}/transactions?account_id={account_id}", headers=HEADERS).json()
    rows = rows["transactions"]
    shop = next(one for one in rows if one["amount"] == -1234)
    origin = client.get(f"/api/transactions/{shop['id']}/origin", headers=HEADERS)
    assert origin.status_code == 200, origin.text
    said = origin.json()
    assert (said["kind"], said["workflow"], said["workflow_via"], said["filename"], said["batch_id"]) == (
        "one_time_import", "YNAB", "csv", "export.zip", batch_id
    )
    assert said["line_no"] is None and said["raw"] is None
    # The Register has no bank text, and a clean name is not one (#265).
    assert said["payee_original"] is None
    assert said["import_id"].startswith("ynab:")

    # The row somebody typed is still somebody's.
    by_hand = client.get(f"/api/transactions/{typed['id']}/origin", headers=HEADERS)
    assert by_hand.status_code == 404

    listed = client.get(f"{base}/history", headers=HEADERS).json()["imports"]
    assert [(one["batch_id"], one["workflow"], one["workflow_name"], one["via"], one["filename"], one["status"])
            for one in listed] == [(batch_id, "ynab", "YNAB", "csv", "export.zip", "applied")]

    # An undone one stays listed, marked undone: see
    # test_the_list_of_earlier_imports_covers_every_workflow_and_marks_an_undo.

    # A household that is not yours is not there at all.
    assert client.get("/api/households/0123456789abcdef0123456789abcdef/one-time-import/history",
                      headers=HEADERS).status_code == 404


def test_over_http_an_api_import_names_the_plan(client, fake_ynab):
    house_id, account_id = _house(client)
    plan = json.dumps({
        "currency": "GBP", "date_format": "YYYY-MM-DD",
        "accounts": {"acc-bank": {"kind": "existing", "account_id": account_id},
                     "acc-card": {"kind": "create", "name": "Shop Card", "type": "credit_card"}},
        "categories": {"Groceries": {"kind": "create", "name": "Groceries"},
                       "Household": {"kind": "uncategorised"}},
        "acknowledge_cleared_reset": True,
    })
    done = client.post(
        f"/api/households/{house_id}/one-time-import/ynab/commit",
        data={"via": "api", "token": TOKEN, "plan_id": PLAN_ID, "plan": plan}, headers=HEADERS,
    )
    assert done.status_code == 201, done.text
    rows = client.get(f"/api/households/{house_id}/transactions?account_id={account_id}", headers=HEADERS).json()
    rows = rows["transactions"]
    said = client.get(f"/api/transactions/{rows[0]['id']}/origin", headers=HEADERS).json()
    assert (said["kind"], said["workflow"], said["workflow_via"], said["plan_name"], said["filename"]) == (
        "one_time_import", "YNAB", "api", "Household Plan", None
    )
    listed = client.get(f"/api/households/{house_id}/one-time-import/history", headers=HEADERS).json()
    assert [(one["via"], one["plan_name"]) for one in listed["imports"]] == [("api", "Household Plan")]


def test_over_http_an_api_import_reports_and_shows_the_banks_text(client, monkeypatch):
    _serve(monkeypatch, _world_with_bank_text("GBP"))
    house_id, account_id = _house(client)
    plan = json.dumps({
        "currency": "GBP", "date_format": "YYYY-MM-DD",
        "accounts": {"acc-bank": {"kind": "existing", "account_id": account_id},
                     "acc-card": {"kind": "create", "name": "Shop Card", "type": "credit_card"}},
        "categories": {"Groceries": {"kind": "create", "name": "Groceries"},
                       "Household": {"kind": "uncategorised"}},
        "acknowledge_cleared_reset": True,
    })
    base = f"/api/households/{house_id}/one-time-import/ynab"
    form = {"via": "api", "token": TOKEN, "plan_id": PLAN_ID, "plan": plan}
    preview = client.post(f"{base}/preview", data=form, headers=HEADERS)
    assert preview.status_code == 200, preview.text
    assert preview.json()["bank_text_rows"] == 2
    done = client.post(f"{base}/commit", data=form, headers=HEADERS)
    assert done.status_code == 201, done.text
    assert done.json()["bank_text_rows"] == 2

    rows = client.get(f"/api/households/{house_id}/transactions?account_id={account_id}", headers=HEADERS).json()
    shop = next(one for one in rows["transactions"] if one["amount"] == -1234)
    said = client.get(f"/api/transactions/{shop['id']}/origin", headers=HEADERS).json()
    assert said["payee_original"] == SHOP_BANK_TEXT



def test_over_http_an_import_undoes_cleanly_every_time_on_a_file_database(client):
    """Commit then undo, again and again, on the app's own file-backed SQLite.

    Undoing an import that created an account and linked a transfer into it
    failed about half the time with "FOREIGN KEY constraint failed": the flush
    deleted the new account before the "Transfer : Rainy Day Pot" payee that
    names it, and `payees.transfer_account_id` is RESTRICT, which SQLite checks
    at the DELETE even with foreign keys deferred. The order came out of the
    unit of work's sort, so one round could pass; twelve do not by luck.
    """
    import app.db as db

    assert db.engine.url.database not in (None, "", ":memory:")
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

    def counts() -> dict[str, int]:
        with db.SessionLocal() as s:
            return _counts(s)

    house_id, account_id = _house(client)
    base = f"/api/households/{house_id}/one-time-import/ynab"
    upload = {"file": ("export.zip", _zip(**{"B - Register.csv": _register()}), "application/zip")}
    # The new household's default categories already hold Groceries and
    # Salary; a name and a group it does not have make the run create both.
    plan = json.loads(_http_plan(account_id))
    plan["categories"]["Salary"] = {"kind": "create", "name": "Pay Packet", "group_name": "Earnings"}
    before = counts()

    for _ in range(12):
        done = client.post(
            f"{base}/commit", data={"via": "csv", "plan": json.dumps(plan)}, files=upload,
            headers=HEADERS,
        )
        assert done.status_code == 201, done.text
        assert done.json()["counts"]["transfers_linked"] == 1
        made = counts()
        assert made["Account"] == before["Account"] + 1
        assert made["Category"] == before["Category"] + 1
        assert made["CategoryGroup"] == before["CategoryGroup"] + 1
        assert made["Payee"] > before["Payee"]

        undone = client.post(
            f"/api/households/{house_id}/batches/{done.json()['batch_id']}/undo", headers=HEADERS
        )
        assert undone.status_code == 200, undone.text
        assert counts() == before


# --------------------------------------------------------------------------- #
# Two commits at once, and a refusal only the database can make (#235)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("chunk", [engine.CHUNK_ROWS, 3])
def test_a_database_refusal_fails_its_row_not_the_whole_import(
    session, owner, household, accounts, monkeypatch, chunk
):
    """A constraint only the flush can see used to take every row with it.

    Two rows claiming one import id is the refusal used here because it is the
    one constraint a row of this import can meet; the point is that it is the
    database saying no, not a service check.
    """
    import dataclasses

    monkeypatch.setattr(engine, "CHUNK_ROWS", chunk)
    source = _source()
    corner = next(i for i, one in enumerate(source.rows) if one.payee == "Corner Shop")
    mystery = next(i for i, one in enumerate(source.rows) if one.payee == "Mystery")
    source.rows[mystery] = dataclasses.replace(source.rows[mystery], ref=source.rows[corner].ref)

    report = _run(session, household, owner, _plan(accounts), source=source)

    assert report["counts"]["failed"] == 1
    assert report["counts"]["imported"] == 9, "one row refused, the other nine kept"
    refused = [one for one in report["not_imported"] if one["reason"].startswith("the database refused")]
    assert [one["row_ref"] for one in refused] == [source.rows[mystery].ref]
    amounts = [row.amount for row in _rows(session, accounts["pounds"].id)]
    assert -1234 in amounts and -700 not in amounts, "Corner Shop kept, Mystery refused"
    assert session.get(Batch, report["batch_id"]).summary["imported"] == 9


def test_a_second_commit_racing_the_first_is_a_409_and_the_rows_land_once(client, monkeypatch):
    """Two commits of one plan: one 201, one 409, and the ledger holds the rows once.

    Both requests are held until each has looked for duplicates -- so neither
    can see the other's rows -- and then let go to write. Without the
    translation the loser is a 500 carrying `sqlite3.IntegrityError`.
    """
    import threading

    house_id, account_id = _house(client)
    base = f"/api/households/{house_id}/one-time-import/ynab"
    upload = {"file": ("Register.csv", _register(), "text/csv")}
    plan = json.loads(_http_plan(account_id))
    plan["accounts"]["Rainy Day Pot"] = {"kind": "skip"}

    both_looked = threading.Barrier(2, timeout=10)
    real_pair = engine._pair_transfers

    def pair_then_wait(rows, source):
        real_pair(rows, source)
        both_looked.wait()

    monkeypatch.setattr(engine, "_pair_transfers", pair_then_wait)

    answers: list = []

    def commit():
        answers.append(
            client.post(
                f"{base}/commit", data={"via": "csv", "plan": json.dumps(plan)},
                files=upload, headers=HEADERS,
            )
        )

    workers = [threading.Thread(target=commit) for _ in range(2)]
    for one in workers:
        one.start()
    for one in workers:
        one.join(30)

    assert sorted(one.status_code for one in answers) == [201, 409], [one.text for one in answers]
    refused = next(one for one in answers if one.status_code == 409)
    assert "imported by another request a moment ago" in refused.json()["detail"]
    imported = next(one for one in answers if one.status_code == 201).json()["counts"]["imported"]
    register = client.get(
        f"/api/households/{house_id}/transactions?account_id={account_id}", headers=HEADERS
    ).json()["transactions"]
    assert imported == 8
    assert len(register) == imported, "each row is in the ledger once"
