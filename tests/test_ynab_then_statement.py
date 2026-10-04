"""A One-time Import first, then the bank's statements for the same months (#264).

Every name and figure here is synthetic: "Everyday Account", "Travel Card",
"Corner Shop" and round amounts. Two ledger accounts in each of two currencies,
and the second account holds a row identical to the first's -- same day, same
amount, same YNAB key -- so a match that crossed accounts would show.

The statement lines and what YNAB holds for them:

- **Corner Shop**, -12.34 on 2026-03-02 (the issue's repro). YNAB imported it
  from the bank, so its key ``YNAB:-12340:2026-03-02:1`` is our
  ``ST:-1234:2026-03-02:0``.
- **Fuel Stop**, -45.00, which the bank dated 2026-03-04 and the person moved
  to 2026-03-10 in YNAB. Six days: past the twin rule's four.
- **Market Stall**, -8.00, typed into YNAB by hand on 2026-03-05 with no key;
  the bank has it on 2026-03-06. Only the twin rule can find it.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.audit.undo import undo_batch
from app.models import (
    Account,
    AccountType,
    BatchKind,
    BatchStatus,
    ClearedState,
    ImportLine,
    ImportOutcome,
    Transaction,
)
from app.money import MoneyError, from_milliunits
from app.services import accounts as account_service
from app.services import importing
from app.services.one_time_import import engine, ynab_api, ynab_source
from statements import sniffing

TOKEN = "synthetic-token-0123456789abcdefghijklmnop"
PLAN_ID = "plan-0264"

SHOP_KEY = "ST:-1234:2026-03-02:0"
FUEL_KEY = "ST:-4500:2026-03-04:0"

STATEMENT = (
    b"Date,Description,Amount\n"
    b"2026-03-02,CORNER SHOP TESTVILLE,-12.34\n"
    b"2026-03-04,FUEL STOP 0007,-45.00\n"
    b"2026-03-06,MARKET STALL,-8.00\n"
)


def _txn(ref, account, when, milliunits, payee, *, import_id=None, parts=()):
    return {
        "id": ref, "date": when, "amount": milliunits, "memo": None, "cleared": "cleared",
        "flag_color": None, "flag_name": None, "account_id": account, "payee_name": payee,
        "category_id": None, "category_name": None, "transfer_account_id": None,
        "transfer_transaction_id": None, "deleted": False,
        "import_payee_name": payee if import_id else None,
        "import_payee_name_original": payee.upper() if import_id else None,
        "import_id": import_id,
        "subtransactions": [
            {"id": f"{ref}-{n}", "transaction_id": ref, "amount": amount, "memo": None,
             "payee_name": None, "category_id": None, "category_name": None,
             "transfer_account_id": None, "transfer_transaction_id": None, "deleted": False}
            for n, amount in enumerate(parts, start=1)
        ],
    }


def _world(currency: str) -> dict[str, dict]:
    accounts = [
        {"id": "acc-a", "name": "Everyday Account", "type": "checking", "closed": False, "deleted": False},
        {"id": "acc-b", "name": "Travel Card", "type": "creditCard", "closed": False, "deleted": False},
    ]
    transactions = [
        _txn("t-shop", "acc-a", "2026-03-02", -12340, "Corner Shop",
             import_id="YNAB:-12340:2026-03-02:1"),
        # The same purchase shape in the other account, with the same YNAB key.
        _txn("t-shop-b", "acc-b", "2026-03-02", -12340, "Corner Shop",
             import_id="YNAB:-12340:2026-03-02:1"),
        _txn("t-fuel", "acc-a", "2026-03-10", -45000, "Fuel Stop",
             import_id="YNAB:-45000:2026-03-04:1"),
        _txn("t-cash", "acc-a", "2026-03-05", -8000, "Market Stall"),
        _txn("t-split", "acc-a", "2026-03-07", -25500, "Big Store",
             import_id="YNAB:-25500:2026-03-07:1", parts=(-20000, -5500)),
    ]
    plans = [{"id": PLAN_ID, "name": "Test Plan", "last_modified_on": "2026-03-31T00:00:00Z",
              "first_month": "2026-03-01", "last_month": "2026-03-01",
              "currency_format": {"iso_code": currency, "decimal_digits": 2, "currency_symbol": "?"}}]
    return {
        "/plans": {"plans": plans},
        f"/plans/{PLAN_ID}/accounts": {"accounts": accounts},
        f"/plans/{PLAN_ID}/categories": {"category_groups": []},
        f"/plans/{PLAN_ID}/transactions?since_date={ynab_api.SINCE_DATE}": {"transactions": transactions},
    }


@pytest.fixture(params=["EUR", "GBP"])
def ledger(request, session, owner, household, accounts, monkeypatch):
    """Two accounts in one currency, YNAB served for it, and the plan mapping both."""
    currency = request.param
    if currency == "EUR":
        first, second = accounts["checking"], accounts["card"]
    else:
        first = accounts["pounds"]
        with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
            second = Account(
                household_id=household.id, name="UK Card", type=AccountType.credit_card,
                currency="GBP",
            )
            session.add(second)
        session.commit()
    world = _world(currency)
    monkeypatch.setattr(ynab_api, "get", lambda token, path: world[path])
    plan = engine.Plan(
        currency=currency, date_format="YYYY-MM-DD",
        accounts={
            "acc-a": {"kind": "existing", "account_id": first.id},
            "acc-b": {"kind": "existing", "account_id": second.id},
        },
        categories={},
        acknowledge_cleared_reset=True,
    )
    return {"currency": currency, "first": first, "second": second, "plan": plan, "world": world}


def _one_time(session, household, owner, ledger, *, commit=True):
    source = ynab_source.from_api(TOKEN, PLAN_ID)
    return engine.run(
        session, household, actor_id=owner.id, source=source, plan=ledger["plan"], commit=commit
    )


def _stage(session, owner, household, account, raw=STATEMENT):
    sniffed = sniffing.sniff(raw)
    with batch(
        session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id,
        source={"filename": "statement.csv", "sha256": importing.file_digest(raw),
                "bytes": len(raw), "account_id": account.id, "format": sniffed.format.describe()},
    ) as staged:
        lines = importing.stage_file(
            session, account=account, raw=raw, fmt=sniffed.format, batch_row=staged
        )
        staged.status = BatchStatus.preview
    session.commit()
    return staged, {line.parsed["payee"]: line for line in lines}


def _commit(session, owner, household, account, staged):
    with batch(
        session, kind=BatchKind.imported, actor_id=owner.id, household_id=household.id
    ) as applying:
        summary = importing.commit(session, batch_row=staged, account=account)
        staged.status = BatchStatus.applied
    session.commit()
    return applying, summary


def _rows(session, account) -> dict[str, Transaction]:
    """The account's rows by payee name (each payee once in these fixtures)."""
    rows = session.execute(
        select(Transaction).where(Transaction.account_id == account.id)
    ).scalars().all()
    return {(row.payee.name if row.payee else "") + f"/{row.amount}": row for row in rows}


def _count(session, account) -> int:
    return session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.account_id == account.id)
    ).scalar_one()


def _balance(session, account) -> int:
    return account_service.balances(session, account.id)["balance"]


#: Everything acc-a holds after the One-time Import, in minor units.
FIRST_BALANCE = -1234 - 4500 - 800 - 2000 - 550


# --------------------------------------------------------------------------- #
# YNAB's key, in ours
# --------------------------------------------------------------------------- #


def test_milliunits_become_minor_units_exactly_in_each_currency():
    assert from_milliunits(-12340, "EUR") == -1234
    assert from_milliunits(-12340, "GBP") == -1234
    assert from_milliunits(-12000, "JPY") == -12
    assert from_milliunits(-12345, "BHD") == -12345
    with pytest.raises(MoneyError):
        from_milliunits(-12345, "EUR")


@pytest.mark.parametrize(
    ("ynab", "currency", "ours"),
    [
        ("YNAB:-12340:2026-03-02:1", "EUR", "ST:-1234:2026-03-02:0"),
        ("YNAB:-12340:2026-03-02:3", "GBP", "ST:-1234:2026-03-02:2"),
        ("YNAB:250000:2026-03-02:1", "JPY", "ST:250:2026-03-02:0"),
        ("YNAB:-12345:2026-03-02:1", "BHD", "ST:-12345:2026-03-02:0"),
        # Not a whole cent, not counted from 1, not YNAB's shape: no key at all.
        ("YNAB:-12345:2026-03-02:1", "EUR", None),
        ("YNAB:-12340:2026-03-02:0", "EUR", None),
        ("YNAB:-12340:2026-02-30:1", "EUR", None),
        ("YNAB:-12340:2026-03-02", "EUR", None),
        ("ST:-1234:2026-03-02:0", "EUR", None),
        (None, "EUR", None),
    ],
)
def test_ynabs_import_id_is_translated_to_the_statement_key(ynab, currency, ours):
    assert engine.statement_key_for(ynab, currency) == ours


def test_the_written_row_keeps_the_key_and_a_split_part_does_not(session, owner, household, ledger):
    _one_time(session, household, owner, ledger)
    first, second = _rows(session, ledger["first"]), _rows(session, ledger["second"])

    assert first["Corner Shop/-1234"].import_id == "ynab:t-shop"
    assert first["Corner Shop/-1234"].import_alt_ids == [SHOP_KEY]
    assert first["Fuel Stop/-4500"].import_alt_ids == [FUEL_KEY]
    assert first["Fuel Stop/-4500"].date == date(2026, 3, 10)
    assert second["Corner Shop/-1234"].import_alt_ids == [SHOP_KEY]
    # Typed by hand in YNAB: nothing to key on.
    assert first["Market Stall/-800"].import_alt_ids is None
    # Each part carries its parent's YNAB id, and neither part is the -25.50
    # the bank sent, so neither takes the key.
    assert first["Big Store/-2000"].import_alt_ids is None
    assert first["Big Store/-550"].import_alt_ids is None
    assert _balance(session, ledger["first"]) == FIRST_BALANCE


# --------------------------------------------------------------------------- #
# YNAB, then the statement
# --------------------------------------------------------------------------- #


def test_ynab_then_statement_leaves_one_row_per_transaction(session, owner, household, ledger):
    _one_time(session, household, owner, ledger)
    first, second = ledger["first"], ledger["second"]
    rows_before, second_before = _count(session, first), _rows(session, second)
    assert _balance(session, first) == FIRST_BALANCE

    staged, lines = _stage(session, owner, household, first)
    shop = _rows(session, first)["Corner Shop/-1234"]
    assert lines["CORNER SHOP TESTVILLE"].outcome is ImportOutcome.matched_existing
    assert lines["CORNER SHOP TESTVILLE"].transaction_id == shop.id
    assert lines["MARKET STALL"].outcome is ImportOutcome.matched_existing

    _, summary = _commit(session, owner, household, first, staged)
    assert (summary["created"], summary["absorbed"]) == (0, 3)

    after = _rows(session, first)
    assert _count(session, first) == rows_before == 5
    assert _balance(session, first) == FIRST_BALANCE
    shop = after["Corner Shop/-1234"]
    # The statement's key is the row's now, and the YNAB key survives beside it.
    assert shop.import_id == SHOP_KEY
    assert shop.import_alt_ids == ["ynab:t-shop"]
    assert shop.import_source == "file"
    assert shop.cleared is ClearedState.cleared
    assert shop.import_payee_original == "CORNER SHOP TESTVILLE"
    # Typed by hand in YNAB, absorbed by the twin rule, YNAB key kept too.
    stall = after["Market Stall/-800"]
    assert (stall.import_id, stall.import_alt_ids) == ("ST:-800:2026-03-06:0", ["ynab:t-cash"])

    # The other account's identical row was not touched.
    untouched = _rows(session, second)["Corner Shop/-1234"]
    assert untouched.id == second_before["Corner Shop/-1234"].id
    assert (untouched.import_id, untouched.import_alt_ids) == ("ynab:t-shop-b", [SHOP_KEY])
    assert _balance(session, second) == -1234

    # And the same statement again is all duplicates.
    _, again = _stage(session, owner, household, first)
    assert {line.outcome for line in again.values()} == {ImportOutcome.duplicate_skipped}


def test_a_repeat_one_time_import_still_recognises_an_absorbed_row(session, owner, household, ledger):
    _one_time(session, household, owner, ledger)
    first = ledger["first"]
    staged, _ = _stage(session, owner, household, first)
    _commit(session, owner, household, first, staged)

    for commit in (False, True):
        report = _one_time(session, household, owner, ledger, commit=commit)
        # Skipped as its own -- not offered as a "looks like" duplicate to decide.
        assert report["duplicates"] == []
        assert report["counts"]["imported"] == 0
        assert report["counts"]["duplicates_skipped"] == 6
    assert _count(session, first) == 5
    assert _balance(session, first) == FIRST_BALANCE
    assert _rows(session, first)["Corner Shop/-1234"].import_id == SHOP_KEY


def test_the_exact_key_matches_a_row_the_twin_rule_would_miss(session, owner, household, ledger):
    _one_time(session, household, owner, ledger)
    first = ledger["first"]
    fuel = _rows(session, first)["Fuel Stop/-4500"]
    assert (fuel.date - date(2026, 3, 4)).days == 6 > importing.MATCH_WINDOW_DAYS

    # The twin rule alone: nothing within four days.
    twins = importing._Twins(session, first.id, [date(2026, 3, 4)])
    assert twins.find(date(2026, 3, 4), -4500, claimed=set()) is None
    assert twins.find(date(2026, 3, 4), -4500, claimed=set(), keys={FUEL_KEY}).id == fuel.id

    staged, lines = _stage(session, owner, household, first)
    line = lines["FUEL STOP 0007"]
    assert line.outcome is ImportOutcome.matched_existing
    assert line.transaction_id == fuel.id
    assert "One-time Import" in line.reason
    # Re-deciding the preview, as opening it does, keeps the match.
    assert importing.reassess(session, batch_row=staged, account=first) == []

    _commit(session, owner, household, first, staged)
    fuel = _rows(session, first)["Fuel Stop/-4500"]
    assert (fuel.import_id, fuel.import_alt_ids) == (FUEL_KEY, ["ynab:t-fuel"])
    # The date the person chose in YNAB stands.
    assert fuel.date == date(2026, 3, 10)
    assert _count(session, first) == 5
    assert _balance(session, first) == FIRST_BALANCE


def test_a_reconciled_ynab_row_is_not_absorbed(session, owner, household, ledger):
    _one_time(session, household, owner, ledger)
    first = ledger["first"]
    rows = _rows(session, first)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        for name in ("Corner Shop/-1234", "Fuel Stop/-4500"):
            rows[name].cleared = ClearedState.reconciled
    session.commit()

    staged, lines = _stage(session, owner, household, first)
    # Locked rows are not offered, by key or by date: those two lines are new.
    assert lines["CORNER SHOP TESTVILLE"].outcome is ImportOutcome.created
    assert lines["FUEL STOP 0007"].outcome is ImportOutcome.created
    assert lines["MARKET STALL"].outcome is ImportOutcome.matched_existing

    _, summary = _commit(session, owner, household, first, staged)
    assert (summary["created"], summary["absorbed"]) == (2, 1)
    locked = _rows(session, first)["Fuel Stop/-4500"]
    assert locked.import_id == "ynab:t-fuel"
    assert locked.cleared is ClearedState.reconciled
    assert _balance(session, first) == FIRST_BALANCE - 1234 - 4500


def test_undoing_the_statement_gives_the_row_back_its_ynab_key(session, owner, household, ledger):
    _one_time(session, household, owner, ledger)
    first = ledger["first"]
    staged, _ = _stage(session, owner, household, first)
    applying, _ = _commit(session, owner, household, first, staged)

    undo_batch(session, applying.id, actor_id=owner.id)
    session.commit()
    shop = _rows(session, first)["Corner Shop/-1234"]
    assert (shop.import_id, shop.import_alt_ids) == ("ynab:t-shop", [SHOP_KEY])
    assert shop.import_source == "one-time-import:ynab-api"
    assert _balance(session, first) == FIRST_BALANCE

    # And a statement staged afterwards finds it again, exactly.
    _, lines = _stage(session, owner, household, first)
    assert lines["CORNER SHOP TESTVILLE"].transaction_id == shop.id


# --------------------------------------------------------------------------- #
# The statement, then YNAB -- as before
# --------------------------------------------------------------------------- #


def test_statement_then_ynab_still_skips_what_the_statement_brought(session, owner, household, ledger):
    first = ledger["first"]
    raw = b"Date,Description,Amount\n2026-03-02,CORNER SHOP TESTVILLE,-12.34\n2026-03-06,MARKET STALL,-8.00\n"
    staged, _ = _stage(session, owner, household, first, raw)
    _commit(session, owner, household, first, staged)
    assert _balance(session, first) == -1234 - 800

    report = _one_time(session, household, owner, ledger)
    assert sorted(one["row_ref"] for one in report["duplicates"]) == ["t-cash", "t-shop"]

    rows = _rows(session, first)
    shop = rows["CORNER SHOP TESTVILLE/-1234"]
    assert (shop.import_id, shop.import_alt_ids) == (SHOP_KEY, None)
    assert "Corner Shop/-1234" not in rows
    # The statement's two, and Fuel Stop and the split's two parts from YNAB.
    assert _count(session, first) == 5
    assert _balance(session, first) == FIRST_BALANCE
    # The second account had no statement: its row came in as usual.
    assert _balance(session, ledger["second"]) == -1234



# --------------------------------------------------------------------------- #
# The whole file at once, and a window match on YNAB's history
# --------------------------------------------------------------------------- #


def _add_to_ynab(ledger, *transactions) -> None:
    listed = ledger["world"][f"/plans/{PLAN_ID}/transactions?since_date={ynab_api.SINCE_DATE}"]
    listed["transactions"].extend(transactions)


def _staged_lines(session, staged) -> list[ImportLine]:
    return session.execute(select(ImportLine).where(ImportLine.batch_id == staged.id)).scalars().all()


def test_an_exact_key_is_settled_for_the_whole_file_before_any_window_match(
    session, owner, household, ledger
):
    """A 5.00 the bank dated 03-24 that YNAB holds on 03-23, and an earlier
    5.00 on 03-21 that YNAB never saw. Line by line, the 03-21 line reached the
    row first, through the window, and the 03-24 line -- the row's own bank
    line -- was left to look new."""
    _add_to_ynab(
        ledger,
        _txn("t-five", "acc-a", "2026-03-23", -5000, "Kiosk", import_id="YNAB:-5000:2026-03-24:1"),
    )
    _one_time(session, household, owner, ledger)
    first = ledger["first"]
    kiosk = _rows(session, first)["Kiosk/-500"]
    assert kiosk.import_alt_ids == ["ST:-500:2026-03-24:0"]
    before = _balance(session, first)
    assert before == FIRST_BALANCE - 500

    raw = b"Date,Description,Amount\n2026-03-21,KIOSK 21,-5.00\n2026-03-24,KIOSK 24,-5.00\n"
    staged, lines = _stage(session, owner, household, first, raw)
    assert lines["KIOSK 24"].outcome is ImportOutcome.matched_existing
    assert lines["KIOSK 24"].transaction_id == kiosk.id
    assert lines["KIOSK 21"].outcome is ImportOutcome.created
    # Re-deciding the preview gives the same answer.
    assert importing.reassess(session, batch_row=staged, account=first) == []

    _, summary = _commit(session, owner, household, first, staged)
    assert (summary["created"], summary["absorbed"]) == (1, 1)
    fives = session.execute(
        select(Transaction).where(Transaction.account_id == first.id, Transaction.amount == -500)
    ).scalars().all()
    # One row per bank line: the YNAB row is the 03-24 line, and 03-21 is new.
    assert sorted(row.import_id for row in fives) == ["ST:-500:2026-03-21:0", "ST:-500:2026-03-24:0"]
    assert session.get(Transaction, kiosk.id).import_id == "ST:-500:2026-03-24:0"
    assert _count(session, first) == 7
    assert _balance(session, first) == before - 500


def test_a_window_match_on_a_ynab_row_asks_to_be_checked(session, owner, household, ledger):
    """The cutover: YNAB's last coffee and a new one the next day, same price.
    The window takes the first line to YNAB's row, as it would a typed one --
    and the preview says it was matched by date, not by the bank's key."""
    _add_to_ynab(ledger, _txn("t-coffee", "acc-a", "2026-03-20", -5000, "Coffee Cart"))
    _one_time(session, household, owner, ledger)
    first = ledger["first"]
    coffee = _rows(session, first)["Coffee Cart/-500"]
    assert coffee.import_alt_ids is None

    raw = (
        b"Date,Description,Amount\n"
        b"2026-03-02,CORNER SHOP TESTVILLE,-12.34\n"
        b"2026-03-20,COFFEE CART,-5.00\n"
        b"2026-03-21,COFFEE CART,-5.00\n"
    )
    staged, _ = _stage(session, owner, household, first, raw)
    by_date = {line.parsed["date"]: line for line in _staged_lines(session, staged)}
    assert by_date["2026-03-20"].outcome is ImportOutcome.matched_existing
    assert by_date["2026-03-20"].transaction_id == coffee.id
    assert "from your YNAB history" in by_date["2026-03-20"].reason
    assert "check it is the same purchase" in by_date["2026-03-20"].reason
    assert by_date["2026-03-21"].outcome is ImportOutcome.created
    # The exact match needs no such warning.
    assert by_date["2026-03-02"].outcome is ImportOutcome.matched_existing
    assert "check it" not in by_date["2026-03-02"].reason
    # Nor does reopening the preview change any of it.
    assert importing.reassess(session, batch_row=staged, account=first) == []

    before = _balance(session, first)
    _, summary = _commit(session, owner, household, first, staged)
    assert (summary["created"], summary["absorbed"]) == (1, 2)
    assert _count(session, first) == 7
    assert _balance(session, first) == before - 500
