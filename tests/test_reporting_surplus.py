"""What a repayment differs from its expenses by is counted (0.13.1).

Work reimbursements stay apart from income and expenses (#372), but the
household is better off by whatever work paid beyond what was spent, and worse
off by whatever it paid short. So each repayment in the Reimbursement category
is compared with the expenses linked to it, and the difference is a row of its
own: *Reimbursement surplus* in income, *Reimbursement shortfall* in expenses,
in the repayment's month. Every figure drills into the rows that make it.
"""

from __future__ import annotations

from datetime import date

from app.api.routers import reporting as report_routes
from app.services import reporting as report_service
from tests import test_reporting as base
from tests.test_reporting import _add, _flag, _report

#: The same two groups and roles as the main report tests.
categorised = base.categorised

WINDOW = dict(currency="EUR", since=date(2026, 1, 1), until=date(2026, 3, 31))


def _claim(session, household, owner, accounts, categorised, *, spent, back, paid_on):
    """Expenses of ``spent`` on the card in January, repaid ``back`` on ``paid_on``."""
    repaid = _add(
        session, household, owner, accounts["checking"], when=paid_on, amount=back,
        category=categorised["reimbursement"],
    )
    for n, amount in enumerate(spent):
        expense = _add(
            session, household, owner, accounts["card"], when=date(2026, 1, 10 + n), amount=-amount
        )
        _flag(session, household, owner, expense, paid_by=repaid)
    return repaid


def test_a_surplus_is_income_in_the_month_the_money_came_back(
    session, household, owner, accounts, categorised
):
    """200 spent and 300 back: the 100 is income, in the repayment's month."""
    _claim(
        session, household, owner, accounts, categorised,
        spent=[12_000, 8_000], back=30_000, paid_on=date(2026, 2, 27),
    )

    answer = _report(session, household)
    surplus = [row for row in answer["income"].rows if row.line]
    assert [(row.line, row.name, row.by_month) for row in surplus] == [
        ("reimbursement_surplus", "Reimbursement surplus", {"2026-02": 10_000})
    ]
    assert surplus[0].key is None and surplus[0].group_name is None
    assert answer["income"].total_minor == 10_000
    assert answer["expense"].total_minor == 0
    assert answer["net_total_minor"] == 10_000
    # The block still shows the whole repayment and the whole spend, and the
    # difference that was counted, so the reader can see where 100 came from.
    block = answer["reimbursements"]
    assert block["income"].total_minor == 30_000
    assert block["spent"].total_minor == -20_000
    assert block["difference"].by_month == {"2026-02": 10_000}
    assert block["unmatched"].total_minor == 0


def test_a_shortfall_is_a_cost_in_the_month_the_money_came_back(
    session, household, owner, accounts, categorised
):
    """200 spent and 150 back: the 50 the household absorbed is an expense."""
    _claim(
        session, household, owner, accounts, categorised,
        spent=[20_000], back=15_000, paid_on=date(2026, 3, 5),
    )

    answer = _report(session, household)
    assert answer["income"].total_minor == 0
    assert [(row.line, row.by_month) for row in answer["expense"].rows] == [
        ("reimbursement_shortfall", {"2026-03": -5_000})
    ]
    assert answer["net_total_minor"] == -5_000
    assert answer["reimbursements"]["difference"].total_minor == -5_000


def test_an_exact_repayment_counts_nothing(session, household, owner, accounts, categorised):
    _claim(
        session, household, owner, accounts, categorised,
        spent=[9_000, 1_000], back=10_000, paid_on=date(2026, 1, 30),
    )

    answer = _report(session, household)
    assert answer["income"].rows == [] and answer["expense"].rows == []
    assert answer["net_total_minor"] == 0
    assert answer["reimbursements"]["difference"].total_minor == 0


def test_the_claim_is_compared_whole_whatever_the_window(
    session, household, owner, accounts, categorised
):
    """An expense months before its repayment is still part of the claim.

    Cut at the window, a December hotel repaid in February would make the
    whole repayment look like surplus.
    """
    repaid = _add(
        session, household, owner, accounts["checking"], when=date(2026, 2, 3), amount=30_000,
        category=categorised["reimbursement"],
    )
    hotel = _add(
        session, household, owner, accounts["card"], when=date(2025, 12, 12), amount=-25_000
    )
    _flag(session, household, owner, hotel, paid_by=repaid)

    answer = _report(session, household)
    assert answer["income"].total_minor == 5_000


def test_a_repayment_outside_the_window_counts_nothing_inside_it(
    session, household, owner, accounts, categorised
):
    _claim(
        session, household, owner, accounts, categorised,
        spent=[10_000], back=30_000, paid_on=date(2026, 4, 2),
    )
    assert _report(session, household)["income"].total_minor == 0


def test_a_repayment_linked_to_nothing_is_unmatched_and_not_counted(
    session, household, owner, accounts, categorised
):
    """Counting it whole would overstate income whenever linking lags."""
    _add(
        session, household, owner, accounts["checking"], when=date(2026, 1, 15), amount=40_000,
        category=categorised["reimbursement"],
    )

    answer = _report(session, household)
    assert answer["income"].total_minor == 0
    assert answer["reimbursements"]["unmatched"].by_month == {"2026-01": 40_000}
    assert answer["reimbursements"]["difference"].total_minor == 0

    drilled = report_service.behind(
        session, household.id, direction="reimbursement_unmatched", **WINDOW
    )
    assert [e.amount_minor for e in drilled["entries"]] == [40_000]
    assert drilled["total_minor"] == 40_000


def test_a_claim_in_two_currencies_states_no_difference(
    session, household, owner, accounts, categorised
):
    """A GBP expense repaid in EUR: no rate, so no surplus -- and it is counted."""
    repaid = _add(
        session, household, owner, accounts["checking"], when=date(2026, 2, 1), amount=30_000,
        category=categorised["reimbursement"],
    )
    pounds = _add(
        session, household, owner, accounts["pounds"], when=date(2026, 1, 20), amount=-20_000
    )
    _flag(session, household, owner, pounds, paid_by=repaid)

    answer = _report(session, household)
    assert answer["income"].total_minor == 0
    assert answer["reimbursements"]["difference"].total_minor == 0
    assert answer["reimbursement_mixed"] == 1


def test_a_written_off_expense_is_not_counted_twice(
    session, household, owner, accounts, categorised
):
    """A written-off row is an expense already; the claim compares only the
    rows still set apart, so the repayment's difference does not take it in."""
    from app.models import ReimbursementState

    repaid = _claim(
        session, household, owner, accounts, categorised,
        spent=[10_000], back=10_000, paid_on=date(2026, 2, 1),
    )
    refused = _add(session, household, owner, accounts["card"], when=date(2026, 1, 25), amount=-3_000)
    _flag(session, household, owner, refused, ReimbursementState.written_off, paid_by=repaid)

    answer = _report(session, household)
    assert answer["expense"].total_minor == -3_000
    assert answer["reimbursements"]["difference"].total_minor == 0


def test_every_new_figure_drills_into_rows_that_add_up_to_it(
    session, household, owner, accounts, categorised
):
    """The repayment and the expenses it covered, summing to the cell clicked."""
    _add(
        session, household, owner, accounts["checking"], when=date(2026, 2, 25), amount=200_000,
        category=categorised["salary"],
    )
    _claim(
        session, household, owner, accounts, categorised,
        spent=[12_000, 8_000], back=30_000, paid_on=date(2026, 2, 27),
    )
    short = _add(
        session, household, owner, accounts["checking"], when=date(2026, 3, 9), amount=15_000,
        category=categorised["reimbursement"],
    )
    taxi = _add(session, household, owner, accounts["card"], when=date(2026, 3, 1), amount=-20_000)
    _flag(session, household, owner, taxi, paid_by=short)

    answer = _report(session, household)
    surplus = report_service.behind(
        session, household.id, direction="reimbursement_surplus", **WINDOW
    )
    assert sorted(e.amount_minor for e in surplus["entries"]) == [-12_000, -8_000, 30_000]
    assert surplus["total_minor"] == 10_000

    feb = report_service.behind(
        session, household.id, direction="reimbursement_surplus", period="2026-02", **WINDOW
    )
    assert feb["total_minor"] == 10_000
    assert report_service.behind(
        session, household.id, direction="reimbursement_surplus", period="2026-01", **WINDOW
    )["count"] == 0

    shortfall = report_service.behind(
        session, household.id, direction="reimbursement_shortfall", **WINDOW
    )
    assert shortfall["total_minor"] == -5_000

    difference = report_service.behind(
        session, household.id, direction="reimbursement_difference", **WINDOW
    )
    assert difference["total_minor"] == answer["reimbursements"]["difference"].total_minor == 5_000

    # The section totals and Net take the derived rows in, so their bubbles do too.
    income = report_service.behind(session, household.id, direction="in", **WINDOW)
    assert income["total_minor"] == answer["income"].total_minor == 210_000
    expense = report_service.behind(session, household.id, direction="out", **WINDOW)
    assert expense["total_minor"] == answer["expense"].total_minor == -5_000
    net = report_service.behind(session, household.id, **WINDOW)
    assert net["total_minor"] == answer["net_total_minor"] == 205_000
    # A category row is still only that category.
    salary = report_service.behind(
        session, household.id, direction="in", row_category_id=categorised["salary"].id, **WINDOW
    )
    assert salary["total_minor"] == 200_000


def test_the_route_names_the_line_and_never_calls_it_uncategorised(
    session, household, owner, accounts, categorised
):
    _claim(
        session, household, owner, accounts, categorised,
        spent=[20_000], back=30_000, paid_on=date(2026, 2, 27),
    )

    out = report_routes.income_expense(
        household=household, session=session, currency="eur",
        since=date(2026, 1, 1), until=date(2026, 3, 31),
        account_id=None, category_id=None, include_uncategorised=True,
    )
    (row,) = out.income.rows
    assert row.line == "reimbursement_surplus"
    assert row.name_code is None
    assert row.total_minor == 10_000
    assert out.reimbursements.difference.total_minor == 10_000
    assert out.reimbursements.unmatched.total_minor == 0
    assert out.reimbursement_mixed == 0
