"""What the Income vs Expense report must get right.

Every test here asserts a **figure**, not a status code. The three defects this
module exists to prevent all produce a 200 with a wrong number in it:

- a transfer leg counted as spend (41.5% of outflow on the real ledger),
- an opening balance counted as income (a whole phantom first month),
- two currencies added together (three wrong reports in the previous build).

So a test that only proved the endpoint answered would not have caught any of
them.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.audit.batch import batch
from app.models import (
    BatchKind,
    Category,
    CategoryGroup,
    Payee,
    ReimbursementState,
    SystemPayee,
    Transaction,
)
from app.services import accounts as account_service
from app.services import reporting as report_service


@pytest.fixture()
def categorised(session, household, owner):
    """Two categories under one group, so the breakdown has something to break."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        group = CategoryGroup(household_id=household.id, name="Frequent")
        session.add(group)
        session.flush()
        groceries = Category(household_id=household.id, group_id=group.id, name="Groceries")
        salary_cat = Category(household_id=household.id, group_id=group.id, name="Salary")
        session.add_all([groceries, salary_cat])
    return {"group": group, "groceries": groceries, "salary": salary_cat}


def _add(session, household, owner, account, *, when, amount, category=None, payee=None):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn = Transaction(
            household_id=household.id,
            account_id=account.id,
            date=when,
            amount=amount,
            category_id=category.id if category else None,
            payee_id=payee.id if payee else None,
        )
        session.add(txn)
    return txn


def _report(session, household, **kw):
    kw.setdefault("currency", "EUR")
    kw.setdefault("since", date(2026, 1, 1))
    kw.setdefault("until", date(2026, 3, 31))
    return report_service.income_expense(session, household.id, **kw)


# --------------------------------------------------------------------------- #
# The three ways this report can be quietly wrong
# --------------------------------------------------------------------------- #


def test_a_transfer_leg_is_not_spending(session, household, owner, accounts):
    """The largest error available to this feature, and it is silent.

    Moving money from the current account to the card is not spending it. The
    assertion is on the *figure*: expense must be the grocery shop alone, not
    the grocery shop plus the transfer.
    """
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 5), amount=-5_000)

    # A mirrored pair, the way transfers are really stored.
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        out = Transaction(
            household_id=household.id,
            account_id=accounts["checking"].id,
            date=date(2026, 1, 10),
            amount=-40_000,
            transfer_account_id=accounts["card"].id,
        )
        back = Transaction(
            household_id=household.id,
            account_id=accounts["card"].id,
            date=date(2026, 1, 10),
            amount=40_000,
            transfer_account_id=accounts["checking"].id,
        )
        session.add_all([out, back])
        session.flush()
        out.transfer_transaction_id = back.id
        back.transfer_transaction_id = out.id

    answer = _report(session, household)

    assert answer["expense"].total_minor == -5_000, "the transfer leg was counted as spend"
    assert answer["income"].total_minor == 0, "the other leg was counted as income"
    assert answer["excluded"]["transfers"] == 2
    assert answer["net_total_minor"] == -5_000


def test_an_opening_balance_is_not_income(session, household, owner):
    """A freshly created account must not report its balance as money earned."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session,
            household=household,
            name="Santander",
            type="checking",
            currency="EUR",
            opening_balance=250_000,
            opening_date=date(2026, 1, 1),
        )
    _add(session, household, owner, account, when=date(2026, 2, 3), amount=-1_200)

    answer = _report(session, household)

    assert answer["income"].total_minor == 0, "the opening balance was reported as income"
    assert answer["expense"].total_minor == -1_200
    assert answer["excluded"]["opening_balances"] == 1


def test_renaming_the_opening_balance_payee_does_not_resurrect_it(
    session, household, owner
):
    """The reason this is a column and not a string match.

    A person is free to rename "Opening balance" to anything. Before
    `Payee.system` that rename turned the account's starting balance back into
    a month of income, and nothing on screen would have said so.
    """
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account_service.create_account(
            session,
            household=household,
            name="Santander",
            type="checking",
            currency="EUR",
            opening_balance=250_000,
            opening_date=date(2026, 1, 1),
        )

    payee = session.query(Payee).filter_by(system=SystemPayee.opening_balance).one()
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        payee.name = "Startsaldo"
        payee.name_folded = "startsaldo"

    answer = _report(session, household)
    assert answer["income"].total_minor == 0, "a rename brought the opening balance back"


def test_two_currencies_are_never_added(session, household, owner, accounts, categorised):
    """One currency in, one currency out. The GBP row must not appear at all."""
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 5), amount=-3_000, category=categorised["groceries"],
    )
    _add(
        session, household, owner, accounts["pounds"],
        when=date(2026, 1, 6), amount=-9_900, category=categorised["groceries"],
    )

    euros = _report(session, household, currency="EUR")
    pounds = _report(session, household, currency="GBP")

    assert euros["expense"].total_minor == -3_000
    assert pounds["expense"].total_minor == -9_900
    # The sum of the two is -12,900 and must appear in neither answer.
    assert euros["expense"].total_minor != -12_900
    assert pounds["expense"].total_minor != -12_900


# --------------------------------------------------------------------------- #
# The shape of the answer
# --------------------------------------------------------------------------- #


def test_the_sign_decides_the_side_not_the_category(
    session, household, owner, accounts, categorised
):
    """A refund against Groceries is income for the month it arrived.

    A category is classification and carries no direction, so the only honest
    answer is the sign of the row. This is also why "Company refunds" can be
    both and neither side has to be told about it.
    """
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 5), amount=-8_000, category=categorised["groceries"],
    )
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 20), amount=2_500, category=categorised["groceries"],
    )

    answer = _report(session, household)

    assert answer["expense"].total_minor == -8_000
    assert answer["income"].total_minor == 2_500
    assert [row.name for row in answer["income"].rows] == ["Groceries"]
    assert answer["net_total_minor"] == -5_500


def test_an_empty_month_is_a_column_not_a_gap(session, household, owner, accounts):
    """The calendar names the columns, not the data.

    February has nothing in it. It must still be a column, because a missing
    column is read as "the months are consecutive" and the reader supplies the
    wrong explanation for the step.
    """
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 5), amount=-1_000)
    _add(session, household, owner, accounts["checking"], when=date(2026, 3, 5), amount=-1_000)

    answer = _report(session, household)

    assert answer["months"] == ["2026-01", "2026-02", "2026-03"]
    assert answer["coverage"] == {"months_in_range": 3, "months_with_activity": 2}
    assert answer["net_by_month"]["2026-02"] == 0


def test_uncategorised_is_a_row_of_its_own(session, household, owner, accounts, categorised):
    """99.2% of outflow value on the real ledger. Never folded into "Other"."""
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 5), amount=-3_000, category=categorised["groceries"],
    )
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 6), amount=-7_000)

    answer = _report(session, household)
    rows = {row.name: row for row in answer["expense"].rows}

    assert set(rows) == {"Groceries", "Uncategorised"}
    assert rows["Uncategorised"].total_minor == -7_000
    assert rows["Uncategorised"].key is None
    # Biggest first by magnitude, so the outgoing that matters most is on top.
    assert [row.name for row in answer["expense"].rows] == ["Uncategorised", "Groceries"]


def test_the_average_divides_by_the_window_not_by_the_months_it_appears_in(
    session, household, owner, accounts, categorised
):
    """One purchase in a three-month window is an average of a third of it.

    Dividing by the months the row appears in would report the full amount and
    turn "you spend 30 a month on this" into "you spent 90 the one month you
    bought it".
    """
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 5), amount=-9_000, category=categorised["groceries"],
    )

    answer = _report(session, household)
    row = answer["expense"].rows[0]

    assert row.total_minor == -9_000
    assert row.average_minor == -3_000
    assert answer["expense"].average_minor == -3_000


def test_a_zero_row_lands_on_neither_side(session, household, owner, accounts):
    """It moves no money, and counting it would make a row count disagree."""
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 5), amount=0)

    answer = _report(session, household)

    assert answer["income"].count == 0
    assert answer["expense"].count == 0


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def test_the_account_filter_narrows_the_figures(
    session, household, owner, accounts, categorised
):
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 5), amount=-3_000)
    _add(session, household, owner, accounts["card"], when=date(2026, 1, 6), amount=-4_000)

    both = _report(session, household)
    card_only = _report(session, household, account_ids=[accounts["card"].id])

    assert both["expense"].total_minor == -7_000
    assert card_only["expense"].total_minor == -4_000


def test_uncategorised_can_be_filtered_out_on_its_own(
    session, household, owner, accounts, categorised
):
    """It is a slice a person can untick, but it is not an id.

    Which is why it is a separate argument rather than a null inside the list.
    """
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 5), amount=-3_000, category=categorised["groceries"],
    )
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 6), amount=-7_000)

    without = _report(session, household, include_uncategorised=False)

    assert [row.name for row in without["expense"].rows] == ["Groceries"]
    assert without["expense"].total_minor == -3_000


def test_a_category_filter_still_keeps_uncategorised_when_asked(
    session, household, owner, accounts, categorised
):
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 5), amount=-3_000, category=categorised["groceries"],
    )
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 6), amount=-1_000, category=categorised["salary"],
    )
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 7), amount=-7_000)

    answer = _report(
        session,
        household,
        category_ids=[categorised["groceries"].id],
        include_uncategorised=True,
    )

    assert {row.name for row in answer["expense"].rows} == {"Groceries", "Uncategorised"}
    assert answer["expense"].total_minor == -10_000


# --------------------------------------------------------------------------- #
# The currency list behind the toggle
# --------------------------------------------------------------------------- #


def test_the_currency_list_comes_from_the_accounts_busiest_first(
    session, household, owner, accounts
):
    """Not from `base_currency`, which is a default rather than a fact."""
    _add(session, household, owner, accounts["pounds"], when=date(2026, 1, 5), amount=-1_000)
    _add(session, household, owner, accounts["pounds"], when=date(2026, 1, 6), amount=-1_000)
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 7), amount=-1_000)

    assert report_service.currencies_in_use(session, household.id) == ["GBP", "EUR"]


def test_a_category_filter_can_also_drop_uncategorised(
    session, household, owner, accounts, categorised
):
    """Both halves of the category filter at once.

    Ticking two categories and unticking Uncategorised is an ordinary thing to
    do on screen, and it is the one combination where the two arguments have
    to be applied together rather than either one alone.
    """
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 5), amount=-3_000, category=categorised["groceries"],
    )
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 6), amount=-1_000, category=categorised["salary"],
    )
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 7), amount=-7_000)

    answer = _report(
        session,
        household,
        category_ids=[categorised["groceries"].id],
        include_uncategorised=False,
    )

    assert [row.name for row in answer["expense"].rows] == ["Groceries"]
    assert answer["expense"].total_minor == -3_000


# --------------------------------------------------------------------------- #
# "All dates": a window with no ends
# --------------------------------------------------------------------------- #


def test_all_dates_resolves_to_where_the_flow_actually_is(
    session, household, owner, accounts
):
    """An omitted end becomes a real date, and the answer says which."""
    _add(session, household, owner, accounts["checking"], when=date(2025, 11, 4), amount=-1_000)
    _add(session, household, owner, accounts["checking"], when=date(2026, 2, 17), amount=-2_000)

    answer = report_service.income_expense(session, household.id, currency="EUR")

    assert answer["since"] == date(2025, 11, 4)
    assert answer["until"] == date(2026, 2, 17)
    assert answer["months"] == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert answer["expense"].total_minor == -3_000


def test_all_dates_does_not_start_at_an_opening_balance(session, household, owner):
    """The reason the span is measured over flow rather than over the register.

    The earliest row in most ledgers is an opening balance. Letting it set the
    left edge opens every all-dates report with a leading empty column, on the
    one view where somebody is looking at the whole history.
    """
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session,
            household=household,
            name="Santander",
            type="checking",
            currency="EUR",
            opening_balance=250_000,
            opening_date=date(2025, 6, 1),
        )
    _add(session, household, owner, account, when=date(2026, 1, 9), amount=-4_000)

    answer = report_service.income_expense(session, household.id, currency="EUR")

    assert answer["since"] == date(2026, 1, 9), "the opening balance set the left edge"
    assert answer["months"] == ["2026-01"]


def test_all_dates_is_per_currency(session, household, owner, accounts):
    """A EUR report spans the EUR rows, not whatever the GBP account did."""
    _add(session, household, owner, accounts["checking"], when=date(2026, 3, 1), amount=-1_000)
    _add(session, household, owner, accounts["pounds"], when=date(2024, 1, 1), amount=-5_000)

    euros = report_service.income_expense(session, household.id, currency="EUR")
    assert euros["since"] == date(2026, 3, 1)


def test_a_household_with_no_flow_still_answers(session, household, owner, accounts):
    """No rows at all is a real state, not a division by zero."""
    answer = report_service.income_expense(session, household.id, currency="EUR")

    assert answer["income"].total_minor == 0
    assert answer["expense"].total_minor == 0
    assert len(answer["months"]) == 1, "the month somebody is standing in"


# --------------------------------------------------------------------------- #
# Drilling into a figure
# --------------------------------------------------------------------------- #


def test_every_figure_reconciles_with_the_rows_behind_it(
    session, household, owner, accounts, categorised
):
    """The one property the drill-through exists to have.

    Walked over every cell of a real report rather than asserted on one, so a
    narrowing argument that quietly widened the answer is caught wherever it
    is. A bubble that disagreed with the figure above it would be worse than
    no bubble: it hands somebody checking a number the evidence that it is
    wrong when it is right.
    """
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 1, 5), amount=-3_000, category=categorised["groceries"],
    )
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 2, 6), amount=-7_000,
    )
    _add(
        session, household, owner, accounts["card"],
        when=date(2026, 2, 9), amount=2_500, category=categorised["groceries"],
    )
    _add(
        session, household, owner, accounts["checking"],
        when=date(2026, 3, 1), amount=9_000, category=categorised["salary"],
    )

    window = dict(currency="EUR", since=date(2026, 1, 1), until=date(2026, 3, 31))
    answer = report_service.income_expense(session, household.id, **window)

    for section, direction in ((answer["income"], "in"), (answer["expense"], "out")):
        for row in section.rows:
            which = dict(
                row_category_id=row.key, row_uncategorised=row.key is None
            )
            # Every month cell of this row.
            for period, expected in row.by_month.items():
                drilled = report_service.behind(
                    session, household.id, period=period, direction=direction,
                    **which, **window,
                )
                assert drilled["total_minor"] == expected, f"{row.name} {period}"
                assert sum(e.amount_minor for e in drilled["entries"]) == expected
            # And its Total cell.
            whole = report_service.behind(
                session, household.id, direction=direction, **which, **window
            )
            assert whole["total_minor"] == row.total_minor, row.name

        # The band's own total.
        band = report_service.behind(session, household.id, direction=direction, **window)
        assert band["total_minor"] == section.total_minor


def test_a_net_figure_drills_into_both_sides(
    session, household, owner, accounts, categorised
):
    """No direction is not a missing argument -- a net is made of both."""
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 5), amount=-3_000)
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 9), amount=8_000)

    window = dict(currency="EUR", since=date(2026, 1, 1), until=date(2026, 1, 31))
    answer = report_service.income_expense(session, household.id, **window)
    drilled = report_service.behind(session, household.id, period="2026-01", **window)

    assert answer["net_by_month"]["2026-01"] == 5_000
    assert drilled["total_minor"] == 5_000
    assert drilled["count"] == 2


def test_drilling_never_shows_a_transfer_or_an_opening_balance(
    session, household, owner, accounts
):
    """The rows the report excluded must not reappear underneath it.

    A drill-through written against the register instead of against
    `flow_rows` would show exactly these, and they are the two things the
    figure above deliberately does not contain.
    """
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        account = account_service.create_account(
            session,
            household=household,
            name="Santander",
            type="checking",
            currency="EUR",
            opening_balance=250_000,
            opening_date=date(2026, 1, 2),
        )
    _add(session, household, owner, account, when=date(2026, 1, 5), amount=-1_500)

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        out = Transaction(
            household_id=household.id, account_id=account.id,
            date=date(2026, 1, 8), amount=-20_000,
            transfer_account_id=accounts["card"].id,
        )
        back = Transaction(
            household_id=household.id, account_id=accounts["card"].id,
            date=date(2026, 1, 8), amount=20_000,
            transfer_account_id=account.id,
        )
        session.add_all([out, back])
        session.flush()
        out.transfer_transaction_id = back.id
        back.transfer_transaction_id = out.id

    drilled = report_service.behind(
        session, household.id, currency="EUR",
        since=date(2026, 1, 1), until=date(2026, 1, 31),
    )

    assert drilled["count"] == 1
    assert drilled["total_minor"] == -1_500
    assert [e.amount_minor for e in drilled["entries"]] == [-1_500]


def test_a_capped_bubble_still_adds_up(session, household, owner, accounts):
    """The total is counted over the match, not over the rows handed back.

    A shorter list that silently disagreed with the number above it is the
    defect the register's own ceiling comment warns about, one screen down.
    """
    for day in range(1, 11):
        _add(
            session, household, owner, accounts["checking"],
            when=date(2026, 1, day), amount=-100,
        )

    drilled = report_service.behind(
        session, household.id, currency="EUR",
        since=date(2026, 1, 1), until=date(2026, 1, 31), limit=4,
    )

    assert len(drilled["entries"]) == 4
    assert drilled["count"] == 10
    assert drilled["capped"] is True
    assert drilled["total_minor"] == -1_000, "the total is over all ten"


# --------------------------------------------------------------------------- #
# Work expenses and their repayments (decision G(i), 2026-09-25)
# --------------------------------------------------------------------------- #


def _flag(session, household, owner, txn, state=ReimbursementState.expected, *, paid_by=None):
    """Set the two columns directly, inside a batch.

    The service writer is built separately; what is under test here is what
    the report does with rows in each state, not how they got there.
    """
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn.reimbursement = state
        txn.reimbursed_by_id = paid_by.id if paid_by else None


def test_an_exact_claim_is_neither_spending_nor_income(session, household, owner, accounts):
    """A EUR 240 hotel repaid EUR 240 by work: the report sees only the shop.

    Left in, both sides grow by 240 and net stays right, which is exactly why
    nobody would notice -- so the assertion is on each side, not on net.
    """
    _add(session, household, owner, accounts["checking"], when=date(2026, 1, 5), amount=-5_000)
    hotel = _add(session, household, owner, accounts["card"], when=date(2026, 1, 8), amount=-24_000)
    repaid = _add(
        session, household, owner, accounts["checking"], when=date(2026, 1, 28), amount=24_000
    )
    _flag(session, household, owner, hotel, paid_by=repaid)

    answer = _report(session, household)

    assert answer["expense"].total_minor == -5_000, "the hotel was counted as spend"
    assert answer["income"].total_minor == 0, "the repayment was counted as income"
    assert answer["net_total_minor"] == -5_000
    assert answer["excluded"]["reimbursements"] == 2
    assert answer["excluded"]["transfers"] == 0


def test_an_outstanding_work_expense_is_not_spending(session, household, owner, accounts):
    """Owed back and not yet paid is a receivable, not a cost."""
    _add(session, household, owner, accounts["checking"], when=date(2026, 2, 3), amount=-1_200)
    taxi = _add(session, household, owner, accounts["card"], when=date(2026, 2, 4), amount=-2_340)
    _flag(session, household, owner, taxi)

    answer = _report(session, household)

    assert answer["expense"].total_minor == -1_200
    assert answer["expense"].by_month == {"2026-02": -1_200}
    assert answer["excluded"]["reimbursements"] == 1


def test_a_written_off_expense_is_spending_after_all(session, household, owner, accounts):
    """Work said no, so it was the household's money, in the month it went."""
    _add(session, household, owner, accounts["checking"], when=date(2026, 3, 1), amount=-1_000)
    dinner = _add(session, household, owner, accounts["card"], when=date(2026, 3, 9), amount=-6_000)
    _flag(session, household, owner, dinner, ReimbursementState.written_off)

    answer = _report(session, household)

    assert answer["expense"].total_minor == -7_000
    assert answer["expense"].by_month == {"2026-03": -7_000}
    assert answer["excluded"]["reimbursements"] == 0


def test_an_advance_is_income_until_something_points_at_it(session, household, owner, accounts):
    """Nothing says what an unlinked advance is, so it counts -- until linked."""
    advance = _add(
        session, household, owner, accounts["checking"], when=date(2026, 1, 2), amount=50_000
    )

    before = _report(session, household)
    assert before["income"].total_minor == 50_000
    assert before["excluded"]["reimbursements"] == 0

    trip = _add(session, household, owner, accounts["card"], when=date(2026, 1, 20), amount=-34_000)
    _flag(session, household, owner, trip, paid_by=advance)

    after = _report(session, household)
    assert after["income"].total_minor == 0, "a linked advance is not income"
    assert after["expense"].total_minor == 0
    assert after["excluded"]["reimbursements"] == 2


def test_a_repayment_leaves_every_flow_query_not_only_the_totals(
    session, household, owner, accounts
):
    """`behind` and `flow_span` compose the same predicate, so they agree.

    A bubble under the Income total that showed the repayment would be handing
    the reader evidence against a figure that is right.
    """
    _add(session, household, owner, accounts["checking"], when=date(2026, 2, 10), amount=-900)
    hotel = _add(session, household, owner, accounts["card"], when=date(2026, 1, 3), amount=-24_000)
    repaid = _add(
        session, household, owner, accounts["checking"], when=date(2026, 3, 30), amount=24_000
    )
    _flag(session, household, owner, hotel, paid_by=repaid)

    drilled = report_service.behind(
        session, household.id, currency="EUR", since=date(2026, 1, 1), until=date(2026, 3, 31)
    )
    assert [e.amount_minor for e in drilled["entries"]] == [-900]
    assert drilled["total_minor"] == -900

    assert report_service.flow_span(session, household.id, currency="EUR") == (
        date(2026, 2, 10),
        date(2026, 2, 10),
    ), "the work rows set the all-dates window"


def test_the_reimbursement_count_is_windowed_and_per_currency(
    session, household, owner, accounts
):
    """The count says what *this* report dropped, not the whole ledger."""
    euro = _add(session, household, owner, accounts["card"], when=date(2026, 1, 8), amount=-1_000)
    pound = _add(session, household, owner, accounts["pounds"], when=date(2026, 1, 8), amount=-900)
    later = _add(session, household, owner, accounts["card"], when=date(2026, 5, 8), amount=-700)
    for row in (euro, pound, later):
        _flag(session, household, owner, row)

    assert _report(session, household)["excluded"]["reimbursements"] == 1
    assert _report(session, household, currency="GBP")["excluded"]["reimbursements"] == 1

