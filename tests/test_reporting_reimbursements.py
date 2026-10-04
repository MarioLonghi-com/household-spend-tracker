"""What the Reimbursements report must get right.

Every test asserts a **figure**. The defects this report can have all answer
200 with a plausible number in them: EUR added to GBP, a mixed-currency claim
reported as balanced, a written-off dinner still "owed", an advance nobody
spent reported as money work owes.

Rows are set up through the ORM inside a batch, with the two columns written
directly -- the service writer is its own piece of work, and what is under test
here is what the report makes of rows in each state.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date

from sqlalchemy import select

from app.audit.batch import batch
from app.models import (
    BatchKind,
    Category,
    CategoryGroup,
    Receipt,
    ReimbursementState,
    Transaction,
    User,
)
from app.services import payees as payee_service
from app.services import reporting as report_service
from tests.conftest import HEADERS, _setup_owner

EXPECTED = ReimbursementState.expected
WRITTEN_OFF = ReimbursementState.written_off


def _add(session, household, owner, account, *, when, amount, payee=None, state=None, paid_by=None):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn = Transaction(
            household_id=household.id,
            account_id=account.id,
            date=when,
            amount=amount,
            payee_id=payee.id if payee else None,
            reimbursement=state,
            reimbursed_by_id=paid_by.id if paid_by else None,
        )
        session.add(txn)
    return txn


def _payee(session, household, owner, name):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        return payee_service.get_or_create(session, household.id, name)


def _report(session, household, currency="EUR", **kw):
    return report_service.reimbursements(session, household.id, currency=currency, **kw)


# --------------------------------------------------------------------------- #
# T22-T26: the figures the spec names
# --------------------------------------------------------------------------- #


def test_t22_two_currencies_are_two_answers_and_never_one_sum(
    session, household, owner, accounts
):
    """The single most important assertion in the file.

    One outstanding expense in each currency: each answer carries its own
    figure, and the sum of the two is nowhere in either payload.
    """
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 18),
         amount=-2_340, state=EXPECTED)
    _add(session, household, owner, accounts["pounds"], when=date(2026, 9, 19),
         amount=-900, state=EXPECTED)

    euros = _report(session, household, "EUR")
    pounds = _report(session, household, "GBP")

    assert (euros.outstanding, euros.outstanding_count) == (2_340, 1)
    assert (pounds.outstanding, pounds.outstanding_count) == (900, 1)
    assert [r.amount for r in euros.outstanding_rows] == [2_340]
    assert [r.amount for r in pounds.outstanding_rows] == [900]
    for answer in (euros, pounds):
        payload = json.dumps(asdict(answer), default=str)
        assert "3240" not in payload, "EUR and GBP were added together"


def test_t23_an_advance_covering_less_than_it_paid_leaves_a_difference(
    session, household, owner, accounts
):
    """EUR 500 advanced, EUR 340 spent against it: EUR 160 unmatched."""
    advance = _add(session, household, owner, accounts["checking"],
                   when=date(2026, 9, 1), amount=50_000)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 10),
         amount=-20_000, state=EXPECTED, paid_by=advance)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 12),
         amount=-14_000, state=EXPECTED, paid_by=advance)

    answer = _report(session, household)

    [claim] = answer.claims
    assert claim.settlement.id == advance.id
    assert claim.settlement.amount == 50_000
    assert claim.covered == 34_000
    assert claim.difference == 16_000
    assert claim.currencies == ["EUR"]
    assert answer.unmatched == 16_000
    assert (answer.recovered, answer.recovered_count) == (34_000, 2)
    assert answer.outstanding == 0


def test_t24_a_mixed_currency_claim_has_no_difference_in_either_currency(
    session, household, owner, accounts
):
    """A GBP hotel repaid in EUR: listed under both, balanced under neither.

    None, not zero. Zero would say it balanced, and there is no rate in this
    ledger to say whether it did.
    """
    repaid = _add(session, household, owner, accounts["checking"],
                  when=date(2026, 9, 30), amount=11_000)
    hotel = _add(session, household, owner, accounts["pounds"], when=date(2026, 9, 5),
                 amount=-9_500, state=EXPECTED, paid_by=repaid)

    for currency in ("EUR", "GBP"):
        answer = _report(session, household, currency)
        [claim] = answer.claims
        assert claim.settlement.id == repaid.id
        assert claim.settlement.currency == "EUR"
        assert [e.id for e in claim.expenses] == [hotel.id]
        assert claim.expenses[0].currency == "GBP"
        assert claim.expenses[0].amount == 9_500
        assert claim.currencies == ["EUR", "GBP"]
        assert claim.covered is None
        assert claim.difference is None
        assert answer.unmatched == 0, "a difference that does not exist was added up"

    # The expense itself is recovered in its own currency and nowhere else.
    assert _report(session, household, "GBP").recovered == 9_500
    assert _report(session, household, "EUR").recovered == 0


def test_t25_written_off_is_not_outstanding_and_is_written_off(
    session, household, owner, accounts
):
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 3),
         amount=-6_000, state=WRITTEN_OFF)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 4),
         amount=-2_340, state=EXPECTED)

    answer = _report(session, household)

    assert (answer.outstanding, answer.outstanding_count) == (2_340, 1)
    assert (answer.written_off, answer.written_off_count) == (6_000, 1)
    assert [r.amount for r in answer.outstanding_rows] == [2_340]
    assert answer.recovered == 0


def test_t26_an_unattached_advance_is_not_outstanding(session, household, owner, accounts):
    """Money in that nothing points at yet is not a debt in either direction."""
    _add(session, household, owner, accounts["checking"], when=date(2026, 9, 1), amount=50_000)

    answer = _report(session, household)

    assert answer.outstanding == 0
    assert answer.outstanding_count == 0
    assert answer.outstanding_rows == []
    assert answer.claims == []
    assert answer.unmatched == 0
    assert answer.available_currencies == []


# --------------------------------------------------------------------------- #
# The rest of the shape
# --------------------------------------------------------------------------- #


def test_the_outstanding_table_says_what_the_screen_needs(
    session, household, owner, accounts
):
    cabify = _payee(session, household, owner, "Cabify")
    older = _add(session, household, owner, accounts["card"], when=date(2026, 8, 2),
                 amount=-1_100, state=EXPECTED)
    newer = _add(session, household, owner, accounts["checking"], when=date(2026, 9, 18),
                 amount=-2_340, payee=cabify, state=EXPECTED)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        session.add(
            Receipt(
                household_id=household.id,
                transaction_id=newer.id,
                content_sha256="a" * 64,
                blob_sha256="a" * 64,
                media_type="image/jpeg",
                byte_size=1024,
            )
        )

    answer = _report(session, household)

    assert answer.oldest_outstanding == date(2026, 8, 2)
    assert [
        (r.id, r.date, r.payee_name, r.account_name, r.amount, r.has_receipt)
        for r in answer.outstanding_rows
    ] == [
        (older.id, date(2026, 8, 2), None, "Visa", 1_100, False),
        (newer.id, date(2026, 9, 18), "Cabify", "Checking", 2_340, True),
    ]
    assert answer.outstanding_rows[1].account_id == accounts["checking"].id


def test_every_listed_row_carries_its_memo_and_category_in_both_currencies(
    session, household, owner, accounts
):
    """The Memo column and the detail dialog read these (#142).

    One of each listed kind -- an owed expense, a payment, an expense it
    repaid -- in each currency, each with a different memo and category, so a
    join that took the payment's memo for its expense, or one currency's rows
    for the other's, reads as the wrong word rather than as a plausible one.
    """
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        group = CategoryGroup(household_id=household.id, name="Work")
        session.add(group)
        session.flush()
        travel = Category(household_id=household.id, group_id=group.id, name="Travel")
        income = Category(household_id=household.id, group_id=group.id, name="Salary")
        session.add_all([travel, income])

    def described(txn, memo, category):
        with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
            txn.memo = memo
            txn.category_id = category.id if category else None
        return txn

    owed_eur = described(
        _add(session, household, owner, accounts["card"], when=date(2026, 9, 18),
             amount=-2_340, state=EXPECTED),
        "Taxi to the client", travel,
    )
    owed_gbp = described(
        _add(session, household, owner, accounts["pounds"], when=date(2026, 9, 19),
             amount=-900, state=EXPECTED),
        "Tea at Euston", None,
    )
    paid_eur = described(
        _add(session, household, owner, accounts["checking"], when=date(2026, 9, 30),
             amount=8_450),
        "September expenses", income,
    )
    described(
        _add(session, household, owner, accounts["card"], when=date(2026, 9, 2),
             amount=-8_450, state=EXPECTED, paid_by=paid_eur),
        "Hotel, two nights", travel,
    )
    paid_gbp = described(
        _add(session, household, owner, accounts["pounds"], when=date(2026, 9, 29),
             amount=1_200),
        "Advance", None,
    )
    described(
        _add(session, household, owner, accounts["pounds"], when=date(2026, 9, 3),
             amount=-1_200, state=EXPECTED, paid_by=paid_gbp),
        "Train to Leeds", travel,
    )

    euros = _report(session, household, "EUR")
    pounds = _report(session, household, "GBP")

    assert [(r.id, r.memo, r.category_name) for r in euros.outstanding_rows] == [
        (owed_eur.id, "Taxi to the client", "Travel")
    ]
    assert [(r.id, r.memo, r.category_name) for r in pounds.outstanding_rows] == [
        (owed_gbp.id, "Tea at Euston", None)
    ]
    [eur_claim] = euros.claims
    assert (eur_claim.settlement.memo, eur_claim.settlement.category_name) == (
        "September expenses", "Salary",
    )
    assert [(e.memo, e.category_name) for e in eur_claim.expenses] == [
        ("Hotel, two nights", "Travel")
    ]
    [gbp_claim] = pounds.claims
    assert (gbp_claim.settlement.memo, gbp_claim.settlement.category_name) == ("Advance", None)
    assert [(e.memo, e.category_name) for e in gbp_claim.expenses] == [
        ("Train to Leeds", "Travel")
    ]


def test_months_group_by_expense_month_in_the_currency_only(
    session, household, owner, accounts
):
    """Ascending, only months with a flagged row, and flagged = the three parts."""
    repaid = _add(session, household, owner, accounts["checking"],
                  when=date(2026, 10, 5), amount=32_450)
    _add(session, household, owner, accounts["card"], when=date(2026, 7, 3),
         amount=-8_450, state=EXPECTED, paid_by=repaid)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 2),
         amount=-24_000, state=EXPECTED, paid_by=repaid)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 18),
         amount=-2_340, state=EXPECTED)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 20),
         amount=-600, state=WRITTEN_OFF)
    # Another currency in a month of its own: must not produce a EUR row.
    _add(session, household, owner, accounts["pounds"], when=date(2026, 8, 1),
         amount=-900, state=EXPECTED)

    months = [asdict(m) for m in _report(session, household).months]

    assert months == [
        {"month": "2026-07", "flagged": 8_450, "recovered": 8_450,
         "written_off": 0, "outstanding": 0},
        {"month": "2026-09", "flagged": 26_940, "recovered": 24_000,
         "written_off": 600, "outstanding": 2_340},
    ]


def test_claims_are_newest_payment_first_with_expenses_oldest_first(
    session, household, owner, accounts
):
    acme = _payee(session, household, owner, "Acme Ltd")
    first = _add(session, household, owner, accounts["checking"],
                 when=date(2026, 8, 30), amount=5_000, payee=acme)
    second = _add(session, household, owner, accounts["checking"],
                  when=date(2026, 9, 30), amount=32_450)
    late = _add(session, household, owner, accounts["card"], when=date(2026, 9, 20),
                amount=-24_000, state=EXPECTED, paid_by=second)
    early = _add(session, household, owner, accounts["card"], when=date(2026, 9, 2),
                 amount=-8_450, state=EXPECTED, paid_by=second)
    _add(session, household, owner, accounts["card"], when=date(2026, 8, 20),
         amount=-5_000, state=EXPECTED, paid_by=first)

    claims = _report(session, household).claims

    assert [c.settlement.id for c in claims] == [second.id, first.id]
    assert [e.id for e in claims[0].expenses] == [early.id, late.id]
    assert claims[0].covered == 32_450
    assert claims[0].difference == 0
    assert claims[1].settlement.payee_name == "Acme Ltd"
    assert claims[1].settlement.account_name == "Checking"
    assert claims[1].difference == 0


def test_an_overpaid_claim_is_unmatched_and_an_underpaid_one_is_not(
    session, household, owner, accounts
):
    """`unmatched` is money received that no expense accounts for."""
    over = _add(session, household, owner, accounts["checking"],
                when=date(2026, 9, 30), amount=10_000)
    under = _add(session, household, owner, accounts["checking"],
                 when=date(2026, 9, 29), amount=3_000)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 1),
         amount=-7_000, state=EXPECTED, paid_by=over)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 2),
         amount=-5_000, state=EXPECTED, paid_by=under)

    answer = _report(session, household)

    assert {c.settlement.id: c.difference for c in answer.claims} == {
        over.id: 3_000,
        under.id: -2_000,
    }
    assert answer.unmatched == 3_000


def test_available_currencies_are_where_work_rows_are_busiest_first(
    session, household, owner, accounts
):
    """Includes a currency that only holds a payment; ignores ordinary rows."""
    for day in (1, 2, 3):
        _add(session, household, owner, accounts["checking"], when=date(2026, 9, day),
             amount=-100)  # ordinary spend: no bearing on the list
    pounds_payment = _add(session, household, owner, accounts["pounds"],
                          when=date(2026, 9, 28), amount=3_000)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 5),
         amount=-1_000, state=EXPECTED)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 6),
         amount=-2_000, state=EXPECTED, paid_by=pounds_payment)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 7),
         amount=-500, state=WRITTEN_OFF)

    answer = _report(session, household, "GBP")

    assert answer.available_currencies == ["EUR", "GBP"]
    # The GBP answer holds the claim (its payment is in GBP), no GBP expenses.
    assert answer.outstanding == answer.recovered == answer.written_off == 0
    assert [c.settlement.amount for c in answer.claims] == [3_000]
    assert answer.claims[0].difference is None


def test_the_window_is_on_the_expense_date_not_the_payment(
    session, household, owner, accounts
):
    """An expense in the window stays in it however late work pays."""
    late_payment = _add(session, household, owner, accounts["checking"],
                        when=date(2026, 12, 15), amount=24_000)
    in_window = _add(session, household, owner, accounts["card"], when=date(2026, 9, 10),
                     amount=-24_000, state=EXPECTED, paid_by=late_payment)
    early_payment = _add(session, household, owner, accounts["checking"],
                         when=date(2026, 9, 15), amount=4_000)
    _add(session, household, owner, accounts["card"], when=date(2026, 6, 1),
         amount=-4_000, state=EXPECTED, paid_by=early_payment)
    _add(session, household, owner, accounts["card"], when=date(2026, 10, 2),
         amount=-700, state=EXPECTED)
    _add(session, household, owner, accounts["card"], when=date(2026, 8, 31),
         amount=-300, state=WRITTEN_OFF)

    answer = _report(session, household, since=date(2026, 9, 1), until=date(2026, 9, 30))

    assert (answer.since, answer.until) == (date(2026, 9, 1), date(2026, 9, 30))
    assert (answer.recovered, answer.recovered_count) == (24_000, 1)
    assert answer.outstanding == 0, "October's expense is outside the window"
    assert answer.written_off == 0, "August's write-off is outside the window"
    assert [c.settlement.id for c in answer.claims] == [late_payment.id], (
        "a payment in the window whose expenses are not is not listed"
    )
    assert [e.id for e in answer.claims[0].expenses] == [in_window.id]
    assert [m.month for m in answer.months] == ["2026-09"]


def test_another_households_work_rows_never_appear(
    session, household, owner, member, accounts, other_household
):
    from app.models import Account, AccountType

    with batch(session, kind=BatchKind.admin, actor_id=member.id,
               household_id=other_household.id):
        theirs = Account(household_id=other_household.id, name="Theirs",
                         type=AccountType.checking, currency="EUR")
        session.add(theirs)
    _add(session, other_household, member, theirs, when=date(2026, 9, 1),
         amount=-99_900, state=EXPECTED)
    _add(session, household, owner, accounts["card"], when=date(2026, 9, 1),
         amount=-100, state=EXPECTED)

    answer = _report(session, household)

    assert answer.outstanding == 100
    assert [r.amount for r in answer.outstanding_rows] == [100]


# --------------------------------------------------------------------------- #
# T30 and the HTTP shape
# --------------------------------------------------------------------------- #


def _http_world(client) -> dict:
    """A household over HTTP, with its work rows flagged through the ORM."""
    from app.audit.guard import AuditedSession

    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Doe-Smith"}, headers=HEADERS).json()
    visa = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Visa", "type": "credit_card", "currency": "EUR", "country": "ES"},
        headers=HEADERS,
    ).json()
    pounds = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "UK Savings", "type": "savings", "currency": "GBP", "country": "GB"},
        headers=HEADERS,
    ).json()

    def post(account, when, amount, payee):
        answer = client.post(
            f"/api/households/{house['id']}/transactions",
            json={"account_id": account["id"], "date": when, "amount": amount,
                  "payee_name": payee},
            headers=HEADERS,
        )
        assert answer.status_code in (200, 201), answer.text
        return answer.json()["id"]

    taxi = post(visa, "2026-09-18", -2_340, "Cabify")
    hotel = post(visa, "2026-09-02", -8_450, "Hotel")
    repaid = post(visa, "2026-09-30", 8_450, "Acme Ltd")
    tea = post(pounds, "2026-09-03", -900, "Tea")

    with AuditedSession(bind=client.app_module.db_engine, expire_on_commit=False) as own:
        actor = own.execute(select(User.id)).scalars().first()
        with batch(own, kind=BatchKind.manual, actor_id=actor, household_id=house["id"]):
            own.get(Transaction, taxi).reimbursement = EXPECTED
            expense = own.get(Transaction, hotel)
            expense.reimbursement = EXPECTED
            expense.reimbursed_by_id = repaid
            own.get(Transaction, tea).reimbursement = EXPECTED
        own.commit()

    return {"house": house, "taxi": taxi, "hotel": hotel, "repaid": repaid, "visa": visa}


def test_the_report_over_http_carries_the_contract_shape(client):
    world = _http_world(client)
    house = world["house"]["id"]

    response = client.get(f"/api/households/{house}/reports/reimbursements?currency=eur")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["currency"] == "EUR"
    assert body["since"] is None and body["until"] is None
    assert sorted(body["available_currencies"]) == ["EUR", "GBP"]
    assert (body["outstanding"], body["outstanding_count"]) == (2_340, 1)
    assert body["oldest_outstanding"] == "2026-09-18"
    assert (body["recovered"], body["recovered_count"]) == (8_450, 1)
    assert (body["written_off"], body["written_off_count"]) == (0, 0)
    assert body["unmatched"] == 0
    assert body["outstanding_rows"] == [
        {
            "id": world["taxi"],
            "date": "2026-09-18",
            "payee_name": "Cabify",
            "account_id": world["visa"]["id"],
            "account_name": "Visa",
            "amount": 2_340,
            "has_receipt": False,
            "memo": None,
            "category_name": None,
        }
    ]
    [claim] = body["claims"]
    assert claim["settlement"] == {
        "id": world["repaid"],
        "date": "2026-09-30",
        "amount": 8_450,
        "currency": "EUR",
        "account_id": world["visa"]["id"],
        "account_name": "Visa",
        "payee_name": "Acme Ltd",
        "memo": None,
        "category_name": None,
    }
    assert [(e["id"], e["amount"], e["currency"]) for e in claim["expenses"]] == [
        (world["hotel"], 8_450, "EUR")
    ]
    assert (claim["covered"], claim["difference"], claim["currencies"]) == (8_450, 0, ["EUR"])
    assert body["months"] == [
        {"month": "2026-09", "flagged": 10_790, "recovered": 8_450,
         "written_off": 0, "outstanding": 2_340}
    ]
    # GBP's 9.00 is in the GBP answer, not this one.
    assert "900" not in json.dumps([body["outstanding"], body["months"]])

    windowed = client.get(
        f"/api/households/{house}/reports/reimbursements"
        "?currency=EUR&since=2026-09-10&until=2026-09-30"
    ).json()
    assert (windowed["since"], windowed["until"]) == ("2026-09-10", "2026-09-30")
    assert windowed["recovered"] == 0, "the hotel's date is before the window"
    assert windowed["outstanding"] == 2_340


def test_the_income_expense_answer_counts_the_work_rows_it_left_out(client):
    world = _http_world(client)
    body = client.get(
        f"/api/households/{world['house']['id']}/reports/income-expense"
        "?currency=EUR&since=2026-09-01&until=2026-09-30"
    ).json()

    assert body["excluded"]["reimbursements"] == 3, "taxi, hotel and its repayment"
    assert body["expense"]["total_minor"] == 0
    assert body["income"]["total_minor"] == 0


def test_t30_another_household_answers_404_and_a_currency_is_required(client):
    world = _http_world(client)

    stranger = client.get(f"/api/households/{'0' * 32}/reports/reimbursements?currency=EUR")
    assert stranger.status_code == 404
    assert "outstanding" not in stranger.text

    missing = client.get(f"/api/households/{world['house']['id']}/reports/reimbursements")
    assert missing.status_code == 422
    too_long = client.get(
        f"/api/households/{world['house']['id']}/reports/reimbursements?currency=EURO"
    )
    assert too_long.status_code == 422


def test_reading_the_report_writes_nothing(client):
    """A report is a question, not an act: no batch appears in History."""
    from sqlalchemy import func
    from sqlalchemy.orm import Session

    from app.models import Batch

    world = _http_world(client)

    def batches() -> int:
        with Session(client.app_module.db_engine) as own:
            return own.execute(select(func.count()).select_from(Batch)).scalar_one()

    before = batches()
    client.get(f"/api/households/{world['house']['id']}/reports/reimbursements?currency=EUR")
    assert batches() == before
