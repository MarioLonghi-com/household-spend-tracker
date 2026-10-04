"""The One-time Import's report points at the rule suggestions it made possible (#269).

A YNAB history brought through the API carries the bank's own text (#265),
and `payee_service.suggest_rules` turns that text into groups that look like
one payee each. The report says how many, once the import has committed --
and after an import that brought no bank text, that rules can be suggested
after the first statement import instead.

Every string here is made up: nobody's shops, round amounts.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.audit.batch import batch
from app.models import Account, AccountType, BatchKind, Transaction
from app.services import payees as payee_service
from app.services import transactions as transaction_service
from app.services.one_time_import import engine, ynab_api, ynab_source
from tests.conftest import HEADERS
from tests.test_one_time_import_ynab import (
    PLAN_ID,
    TOKEN,
    _api_world,
    _house,
    _plan,
    _run,
    _serve,
    _source,
    _world_with_bank_text,
)

#: (bank text, YNAB payee). Two groups whose strings differ only by a
#: reference and land on two payees each, and one group already on a single
#: payee, which needs no rule and is not a suggestion.
HISTORY = [
    ("COFFEE CART 100101", "Coffee Cart"),
    ("COFFEE CART 100102", "Coffee Kiosk"),
    ("NEWSAGENT TESTVILLE 200201", "Newsagent"),
    ("NEWSAGENT TESTVILLE 200202", "Corner Newsagent"),
    ("SAMPLE BAKERY 300301", "Sample Bakery"),
    ("SAMPLE BAKERY 300302", "Sample Bakery"),
]

TWO_GROUPS = "2 groups of bank strings look like one payee each; make rules? See Payee Naming Rules."


def _world_with_history(currency: str) -> dict[str, dict]:
    world = _api_world()
    world["/plans"]["plans"][0]["currency_format"]["iso_code"] = currency
    world[f"/plans/{PLAN_ID}/transactions?since_date={ynab_api.SINCE_DATE}"]["transactions"] = [
        {"id": f"t-{i}", "date": f"2026-02-{10 + i:02d}", "amount": -1000 * (i + 1), "memo": None,
         "cleared": "cleared", "flag_color": None, "flag_name": None, "account_id": "acc-bank",
         "payee_name": payee, "import_payee_name": payee, "import_payee_name_original": text,
         "import_id": f"YNAB:{-1000 * (i + 1)}:2026-02-{10 + i:02d}:1",
         "category_id": "c1", "category_name": "Groceries", "transfer_account_id": None,
         "transfer_transaction_id": None, "deleted": False, "subtransactions": []}
        for i, (text, payee) in enumerate(HISTORY)
    ]
    return world


def _api_plan(currency: str, account: Account) -> engine.Plan:
    return engine.Plan(
        currency=currency, date_format="YYYY-MM-DD",
        accounts={"acc-bank": {"kind": "existing", "account_id": account.id},
                  "acc-card": {"kind": "skip"}},
        categories={"Groceries": {"kind": "create", "name": "Groceries"},
                    "Household": {"kind": "uncategorised"}},
        acknowledge_cleared_reset=True,
    )


def _statement_rows(session, actor, account: Account, rows: list[tuple[str, str]]) -> None:
    """Rows as a statement import leaves them: the bank's text beside a payee."""
    with batch(session, kind=BatchKind.manual, actor_id=actor.id, household_id=account.household_id):
        for i, (text, payee) in enumerate(rows):
            transaction_service.create(
                session, account=account, date=date(2026, 1, 1) + timedelta(days=i), amount=-100 * (i + 1),
                payee=payee_service.get_or_create(session, account.household_id, payee),
                import_payee_original=text,
            )
    session.commit()


@pytest.fixture()
def elsewhere(session, member, other_household) -> Account:
    """Another household's ledger with a suggestion of its own, and the same
    COFFEE CART strings as the import -- so a count that read past the
    household would come out as three, not two."""
    with batch(session, kind=BatchKind.admin, actor_id=member.id, household_id=other_household.id):
        account = Account(
            household_id=other_household.id, name="Their Current", type=AccountType.checking,
            currency="GBP",
        )
        session.add(account)
    session.commit()
    _statement_rows(session, member, account, [
        ("TICKET OFFICE 4401", "Ticket Office"),
        ("TICKET OFFICE 4402", "Rail Tickets"),
        ("COFFEE CART 100103", "Their Coffee"),
    ])
    found = payee_service.suggest_rules(session, other_household.id)
    assert [one.pattern for one in found] == ["TICKET OFFICE"]  # a real suggestion, there
    return account


@pytest.mark.parametrize(("currency", "target"), [("GBP", "pounds"), ("EUR", "checking")])
def test_an_api_import_reports_how_many_rule_suggestions_its_bank_text_makes(
    session, owner, household, accounts, elsewhere, monkeypatch, currency, target
):
    _serve(monkeypatch, _world_with_history(currency))
    report = _run(
        session, household, owner, _api_plan(currency, accounts[target]),
        source=ynab_source.from_api(TOKEN, PLAN_ID),
    )
    assert report["committed"] is True
    assert report["bank_text_rows"] == 6

    found = payee_service.suggest_rules(session, household.id)
    assert sorted((one.pattern, one.payees) for one in found) == [
        ("COFFEE CART", 2), ("NEWSAGENT TESTVILLE", 2),
    ]
    # The other household's TICKET OFFICE, and its third COFFEE CART payee, are
    # not this household's suggestions.
    assert report["rule_suggestions"] == 2
    assert TWO_GROUPS in report["report_text"]
    assert engine.NO_BANK_TEXT_YET not in report["report_text"]
    # Below the line #265 added, not instead of it.
    lines = report["report_text"].splitlines()
    assert lines.index(TWO_GROUPS) == lines.index(
        "Bank text kept for 6 transactions: Payee Naming Rules may now suggest rules from it."
    ) + 2


def test_a_preview_counts_no_suggestions(session, owner, household, accounts, elsewhere, monkeypatch):
    """A preview's rows are thrown away, so whatever it counted would be the
    ledger before the import and read as if it were after."""
    _serve(monkeypatch, _world_with_history("GBP"))
    report = _run(
        session, household, owner, _api_plan("GBP", accounts["pounds"]),
        source=ynab_source.from_api(TOKEN, PLAN_ID), commit=False,
    )
    assert (report["committed"], report["bank_text_rows"], report["rule_suggestions"]) == (False, 6, None)
    assert "look like one payee" not in report["report_text"]
    assert engine.NO_BANK_TEXT_YET not in report["report_text"]


def test_bank_text_that_groups_into_nothing_says_nothing_about_suggestions(
    session, owner, household, accounts, elsewhere, monkeypatch
):
    _serve(monkeypatch, _world_with_bank_text("GBP"))
    plan = _api_plan("GBP", accounts["pounds"])
    report = _run(session, household, owner, plan, source=ynab_source.from_api(TOKEN, PLAN_ID))
    assert report["bank_text_rows"] == 2
    assert payee_service.suggest_rules(session, household.id) == []
    assert report["rule_suggestions"] == 0
    assert "look like one payee" not in report["report_text"]
    # Bank text did come: the next statement is not what rules are waiting on.
    assert engine.NO_BANK_TEXT_YET not in report["report_text"]


def test_a_register_csv_says_rules_come_after_the_first_statement(
    session, owner, household, accounts, elsewhere
):
    report = _run(session, household, owner, _plan(accounts), source=_source())
    assert report["counts"]["imported"] == 10
    assert (report["bank_text_rows"], report["rule_suggestions"]) == (0, 0)
    assert engine.NO_BANK_TEXT_YET in report["report_text"].splitlines()
    assert "look like one payee" not in report["report_text"]


def test_a_register_csv_into_a_ledger_with_statements_reports_their_suggestions(
    session, owner, household, accounts, elsewhere
):
    """Rules wait on bank text, not on this import's: one statement already
    here is enough for the count, and the sentence about the first statement
    would be wrong."""
    _statement_rows(session, owner, accounts["checking"], [
        ("CHEMIST TESTVILLE 5501", "Chemist"),
        ("CHEMIST TESTVILLE 5502", "High Street Chemist"),
    ])
    report = _run(session, household, owner, _plan(accounts), source=_source())
    assert (report["bank_text_rows"], report["rule_suggestions"]) == (0, 1)
    assert (
        "1 group of bank strings looks like one payee; make a rule? See Payee Naming Rules."
        in report["report_text"].splitlines()
    )
    assert engine.NO_BANK_TEXT_YET not in report["report_text"]


def test_a_count_that_fails_leaves_the_saved_import_saved(
    session, owner, household, accounts, elsewhere, monkeypatch, caplog
):
    """The count runs after the commit. Were its failure the request's, a
    retry would find every row already imported -- so it is the count's."""
    _serve(monkeypatch, _world_with_history("GBP"))

    def broken(*_args, **_kwargs):
        raise RuntimeError("suggestions unavailable")

    monkeypatch.setattr(payee_service, "suggest_rules", broken)
    report = _run(
        session, household, owner, _api_plan("GBP", accounts["pounds"]),
        source=ynab_source.from_api(TOKEN, PLAN_ID),
    )
    assert report["committed"] is True
    assert report["counts"]["imported"] == 6
    assert report["rule_suggestions"] is None
    assert "look like one payee" not in report["report_text"]
    assert engine.NO_BANK_TEXT_YET not in report["report_text"]
    # Saved, and still there after the failed count.
    session.expire_all()
    stored = session.execute(
        select(func.count()).select_from(Transaction).where(
            Transaction.account_id == accounts["pounds"].id,
            Transaction.import_payee_original.like("COFFEE CART %"),
        )
    ).scalar_one()
    assert stored == 2
    assert "rule suggestions not counted" in caplog.text


@pytest.mark.parametrize(("groups", "said"), [
    (payee_service.SUGGESTIONS_LISTED,
     "20 groups of bank strings look like one payee each; make rules?"),
    (payee_service.SUGGESTIONS_LISTED + 25,
     "45 groups of bank strings look like one payee each (the 20 largest are listed); make rules?"),
])
def test_a_count_over_what_the_rules_screen_lists_says_so(groups, said):
    """The Rules screen lists the largest twenty and never says "20 of N"."""
    assert payee_service.SUGGESTIONS_LISTED == 20
    assert engine.rule_suggestions_sentence(groups) == said


def test_the_text_report_says_only_the_largest_are_listed(
    session, owner, household, accounts, elsewhere
):
    """Twenty-one groups from statements, then an import: the report counts
    all of them and the screen's list is capped, and the report says so."""
    _statement_rows(session, owner, accounts["checking"], [
        (f"SAMPLE SHOP {chr(65 + i)}{chr(65 + i)} {7000 + 2 * i + n}", f"Shop {i}-{n}")
        for i in range(21)
        for n in range(2)
    ])
    assert len(payee_service.suggest_rules(session, household.id)) == 20
    report = _run(session, household, owner, _plan(accounts), source=_source())
    assert report["rule_suggestions"] == 21
    assert (
        "21 groups of bank strings look like one payee each (the 20 largest are listed); "
        "make rules? See Payee Naming Rules."
    ) in report["report_text"].splitlines()


def test_over_http_the_commit_carries_the_count_and_the_preview_does_not(client, monkeypatch):
    _serve(monkeypatch, _world_with_history("GBP"))
    house_id, account_id = _house(client)
    plan = json.dumps({
        "currency": "GBP", "date_format": "YYYY-MM-DD",
        "accounts": {"acc-bank": {"kind": "existing", "account_id": account_id},
                     "acc-card": {"kind": "skip"}},
        "categories": {"Groceries": {"kind": "create", "name": "Groceries"}},
        "acknowledge_cleared_reset": True,
    })
    base = f"/api/households/{house_id}/one-time-import/ynab"
    form = {"via": "api", "token": TOKEN, "plan_id": PLAN_ID, "plan": plan}

    preview = client.post(f"{base}/preview", data=form, headers=HEADERS)
    assert preview.status_code == 200, preview.text
    assert preview.json()["rule_suggestions"] is None

    done = client.post(f"{base}/commit", data=form, headers=HEADERS)
    assert done.status_code == 201, done.text
    assert done.json()["rule_suggestions"] == 2
    assert TWO_GROUPS in done.json()["report_text"]
    listed = client.get(f"/api/households/{house_id}/payee-suggestions", headers=HEADERS).json()
    assert sorted(one["pattern"] for one in listed) == ["COFFEE CART", "NEWSAGENT TESTVILLE"]
