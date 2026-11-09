"""Income vs Expense in the base currency: "All, in {base}" (#396 phase 2).

Every currency on one report, each row converted on its own at the
household's chosen rate and only then added. Every assertion is on a figure
worked out by hand from the rates below.

Two of everything: two currencies with a rate (GBP, USD) beside the base
(EUR) and one without (SEK); two households, the other with a rate of its own
that must never reach this one's figures.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.api.routers import reporting as report_routes
from app.audit.batch import batch
from app.errors import ValidationError
from app.models import Account, AccountType, BatchKind, FxManualRate, FxPeriod
from app.money import to_decimal, to_minor
from app.services import fx
from app.services import reporting as report_service
from tests import test_reporting as base
from tests.test_reporting import _add, _flag

categorised = base.categorised

WINDOW = dict(since=date(2026, 1, 1), until=date(2026, 2, 28))

#: Units per one EUR, by month: what the household entered by hand.
GBP_JAN, GBP_FEB, USD = "0.8", "0.875", "1.25"


def _rate(session, household, owner, currency, month: date, rate, *, quote=None):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        session.add(
            FxManualRate(
                household_id=household.id,
                quote_currency=quote or household.base_currency,
                currency=currency,
                date=month,
                period=FxPeriod.month,
                rate=rate,
                created_by_id=owner.id,
            )
        )
    session.commit()


@pytest.fixture()
def wallets(session, household, owner, accounts):
    """The conftest's EUR, EUR and GBP accounts, a USD one and a SEK one."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        dollars = Account(
            household_id=household.id, name="US Checking", type=AccountType.checking, currency="USD"
        )
        kronor = Account(
            household_id=household.id, name="Swedish Savings", type=AccountType.savings, currency="SEK"
        )
        session.add_all([dollars, kronor])
    return {**accounts, "dollars": dollars, "kronor": kronor}


@pytest.fixture()
def rated(session, household, owner, member, other_household):
    """GBP and USD have a rate for January and February; SEK has none.

    The other household has a GBP rate of its own, wildly different, against
    EUR too -- if it ever leaked into this household's figures, they would show.
    """
    _rate(session, household, owner, "GBP", date(2026, 1, 1), GBP_JAN)
    _rate(session, household, owner, "GBP", date(2026, 2, 1), GBP_FEB)
    _rate(session, household, owner, "USD", date(2026, 1, 1), USD)
    _rate(session, household, owner, "USD", date(2026, 2, 1), USD)
    _rate(session, other_household, member, "GBP", date(2026, 1, 1), "0.5", quote="EUR")
    _rate(session, other_household, member, "GBP", date(2026, 2, 1), "0.5", quote="EUR")


def _in_base(session, household, **kw):
    return report_service.income_expense(
        session, household.id, rates=fx.rates(session, household), **(WINDOW | kw)
    )


def _behind(session, household, **kw):
    return report_service.behind(
        session, household.id, rates=fx.rates(session, household), **(WINDOW | kw)
    )


@pytest.fixture()
def ledger(session, household, owner, wallets, categorised, rated):
    """One shop per currency in January, one more GBP in February, and a salary."""
    groceries = categorised["groceries"]
    _add(session, household, owner, wallets["checking"], when=date(2026, 1, 5), amount=-3_000, category=groceries)
    _add(session, household, owner, wallets["pounds"], when=date(2026, 1, 6), amount=-8_000, category=groceries)
    _add(session, household, owner, wallets["dollars"], when=date(2026, 1, 7), amount=-5_000, category=groceries)
    _add(session, household, owner, wallets["kronor"], when=date(2026, 1, 8), amount=-10_000, category=groceries)
    _add(session, household, owner, wallets["pounds"], when=date(2026, 2, 3), amount=-7_000, category=groceries)
    _add(
        session, household, owner, wallets["checking"], when=date(2026, 1, 28), amount=200_000,
        category=categorised["salary"],
    )
    session.commit()
    return groceries


# --------------------------------------------------------------------------- #
# The figures
# --------------------------------------------------------------------------- #


def test_every_currency_is_converted_and_the_one_without_a_rate_is_left_out(
    session, household, ledger
):
    answer = _in_base(session, household)

    assert answer["currency"] == "EUR"
    # EUR -30.00; GBP -80.00 / 0.8 = -100.00; USD -50.00 / 1.25 = -40.00;
    # GBP -70.00 / 0.875 = -80.00 in February. The SEK -100.00 has no rate.
    (groceries,) = answer["expense"].rows
    assert groceries.by_month == {"2026-01": -17_000, "2026-02": -8_000}
    assert groceries.count == 4, "the SEK row is not one of the rows behind the figure"
    assert answer["expense"].total_minor == -25_000
    assert answer["income"].total_minor == 200_000
    assert answer["net_total_minor"] == 175_000
    assert answer["net_by_month"] == {"2026-01": 183_000, "2026-02": -8_000}

    converting = answer["converting"]
    assert converting.not_converted() == [{"currency": "SEK", "count": 1, "months": ["2026-01"]}]
    assert converting.currencies == {"EUR", "GBP", "USD", "SEK"}
    used = [(one["currency"], one["rate_date"], str(one["rate"])) for one in converting.rates_used()]
    assert used == [
        ("GBP", date(2026, 1, 1), GBP_JAN),
        ("GBP", date(2026, 2, 1), GBP_FEB),
        ("USD", date(2026, 1, 1), USD),
    ], "the other household's GBP rate of 0.5 was used"


def test_the_per_currency_report_is_unchanged_beside_it(session, household, ledger):
    """The SEK report still has its row: nothing converted, nothing lost."""
    sek = report_service.income_expense(session, household.id, currency="SEK", **WINDOW)
    assert sek["expense"].total_minor == -10_000
    assert sek["converting"] is None
    gbp = report_service.income_expense(session, household.id, currency="GBP", **WINDOW)
    assert gbp["expense"].total_minor == -15_000


def test_each_row_is_converted_before_anything_is_added(
    session, household, owner, wallets, categorised, rated
):
    """Two GBP rows of 0.01: each is EUR 0.0125, rounded to 0.01 -- together 0.02.

    Converting their sum instead would give 0.025, rounded to 0.03, a figure
    no row behind it adds up to.
    """
    for day in (5, 6):
        _add(
            session, household, owner, wallets["pounds"], when=date(2026, 1, day), amount=-1,
            category=categorised["groceries"],
        )
    session.commit()

    answer = _in_base(session, household)
    assert answer["expense"].total_minor == -2
    assert to_minor(to_decimal(-2, "GBP") / fx.parse_rate(GBP_JAN), "EUR") == -3


def test_with_no_rates_at_all_nothing_foreign_is_counted(session, household, owner, wallets, categorised):
    _add(session, household, owner, wallets["checking"], when=date(2026, 1, 5), amount=-3_000, category=categorised["groceries"])
    _add(session, household, owner, wallets["pounds"], when=date(2026, 1, 6), amount=-8_000, category=categorised["groceries"])
    session.commit()

    answer = _in_base(session, household)
    assert answer["expense"].total_minor == -3_000, "a missing rate was taken as 1"
    assert answer["converting"].not_converted() == [{"currency": "GBP", "count": 1, "months": ["2026-01"]}]


# --------------------------------------------------------------------------- #
# The rows behind a figure
# --------------------------------------------------------------------------- #


def test_the_rows_behind_a_cell_keep_their_own_amount_and_add_up_to_it(session, household, ledger):
    cell = _behind(
        session, household, period="2026-01", row_category_id=ledger.id, direction="out"
    )
    assert cell["total_minor"] == -17_000
    assert cell["count"] == 3
    rows = {(e.currency, e.amount_minor): e.converted_minor for e in cell["entries"]}
    assert rows == {("EUR", -3_000): -3_000, ("GBP", -8_000): -10_000, ("USD", -5_000): -4_000}
    pounds = next(e for e in cell["entries"] if e.currency == "GBP")
    assert (str(pounds.rate.rate), pounds.rate.rate_date, pounds.rate.source) == (
        GBP_JAN, date(2026, 1, 1), "manual",
    )

    answer = _in_base(session, household)
    net = _behind(session, household)
    assert net["total_minor"] == answer["net_total_minor"] == 175_000
    assert "SEK" not in {e.currency for e in net["entries"]}, "a row not in the figure is behind it"


# --------------------------------------------------------------------------- #
# Work reimbursements
# --------------------------------------------------------------------------- #


def test_a_surplus_is_worked_out_in_its_currency_and_converted_at_the_repayments_rate(
    session, household, owner, wallets, categorised, rated
):
    """GBP 160 spent in January, GBP 200 paid back in February: a GBP 40 surplus.

    Both rows of the claim convert at February's 0.875: 200 / 0.875 = 228.57,
    -160 / 0.875 = -182.86, so the surplus is EUR 45.71 -- the difference, not
    an exchange gain. The block's own lines convert each row at its own date,
    so there the expense is -160 / 0.8 = -200.00; the gap between the two is
    what phase 3 of #396 is for.
    """
    hotel = _add(session, household, owner, wallets["pounds"], when=date(2026, 1, 12), amount=-16_000)
    repaid = _add(
        session, household, owner, wallets["pounds"], when=date(2026, 2, 20), amount=20_000,
        category=categorised["reimbursement"],
    )
    _flag(session, household, owner, hotel, paid_by=repaid)
    session.commit()

    answer = _in_base(session, household)
    (surplus,) = answer["income"].rows
    assert surplus.line == report_service.REIMBURSEMENT_SURPLUS
    assert surplus.by_month == {"2026-02": 4_571}
    assert answer["reimbursements"]["difference"].total_minor == 4_571
    assert answer["reimbursements"]["income"].total_minor == 22_857
    assert answer["reimbursements"]["spent"].total_minor == -20_000

    rows = _behind(session, household, direction="reimbursement_surplus")
    assert rows["total_minor"] == 4_571
    assert sorted((e.amount_minor, e.converted_minor) for e in rows["entries"]) == [
        (-16_000, -18_286), (20_000, 22_857),
    ]
    assert {e.rate.rate_date for e in rows["entries"]} == {date(2026, 2, 1)}
    income = _behind(session, household, direction="in")
    assert income["total_minor"] == answer["income"].total_minor == 4_571


def test_a_claim_across_two_currencies_stays_uncounted_in_the_base_currency_too(
    session, household, owner, wallets, categorised, rated
):
    repaid = _add(
        session, household, owner, wallets["checking"], when=date(2026, 2, 1), amount=30_000,
        category=categorised["reimbursement"],
    )
    pounds = _add(session, household, owner, wallets["pounds"], when=date(2026, 1, 20), amount=-20_000)
    _flag(session, household, owner, pounds, paid_by=repaid)
    session.commit()

    answer = _in_base(session, household)
    assert answer["reimbursement_mixed"] == 1
    assert answer["income"].total_minor == 0
    assert answer["reimbursements"]["difference"].total_minor == 0
    # Both rows are still in the block, each converted at its own date.
    assert answer["reimbursements"]["income"].total_minor == 30_000
    assert answer["reimbursements"]["spent"].total_minor == -25_000


def test_an_unmatched_repayment_with_no_rate_is_left_out_of_the_block(
    session, household, owner, wallets, categorised, rated
):
    _add(
        session, household, owner, wallets["kronor"], when=date(2026, 2, 1), amount=30_000,
        category=categorised["reimbursement"],
    )
    session.commit()
    answer = _in_base(session, household)
    assert answer["reimbursements"]["unmatched"].total_minor == 0
    assert answer["converting"].not_converted() == [
        {"currency": "SEK", "count": 1, "months": ["2026-02"]}
    ], "the row on the block's income line and on the unmatched line was counted twice"


# --------------------------------------------------------------------------- #
# The route
# --------------------------------------------------------------------------- #


def _route(session, household, **kw):
    params = dict(
        currency=None, in_base=False, since=WINDOW["since"], until=WINDOW["until"],
        account_id=None, category_id=None, include_uncategorised=True,
    )
    return report_routes.income_expense(household=household, session=session, **(params | kw))


def test_the_route_says_what_it_converted_and_how_old_the_rates_are(session, household, ledger):
    out = _route(session, household, in_base=True)
    assert (out.currency, out.converted) == ("EUR", True)
    assert out.expense.total_minor == -25_000
    assert out.rate_source.chain == ["manual", "ecb"]
    assert out.rate_source.policy == "month_avg"
    assert [(r.currency, r.rate, r.rate_date) for r in out.rates_used] == [
        ("GBP", "0.8", date(2026, 1, 1)), ("GBP", "0.875", date(2026, 2, 1)),
        ("USD", "1.25", date(2026, 1, 1)),
    ]
    assert [(n.currency, n.count) for n in out.not_converted] == [("SEK", 1)]
    assert out.rates_status.not_converted == ["SEK"]
    assert {c.currency: c.source for c in out.rates_status.per_currency} == {
        "GBP": "manual", "USD": "manual",
    }

    per_currency = _route(session, household, currency="gbp")
    assert (per_currency.currency, per_currency.converted) == ("GBP", False)
    assert per_currency.rates_status is None
    assert per_currency.rates_used == []


@pytest.mark.parametrize("ask", [dict(), dict(currency="EUR", in_base=True)])
def test_neither_or_both_is_refused_with_a_code(session, household, ledger, ask):
    with pytest.raises(ValidationError) as refused:
        _route(session, household, **ask)
    assert refused.value.code == "report.currency_or_base"


def test_the_route_drills_into_each_row_with_its_rate(session, household, ledger):
    out = report_routes.behind_a_figure(
        household=household, session=session, currency=None, in_base=True,
        since=WINDOW["since"], until=WINDOW["until"], account_id=None, category_id=None,
        include_uncategorised=True, period="2026-01", row_category_id=ledger.id,
        row_uncategorised=False, row_group_id=None, direction="out",
    )
    assert (out.currency, out.converted, out.total_minor) == ("EUR", True, -17_000)
    pounds = next(e for e in out.entries if e.currency == "GBP")
    assert (pounds.amount, pounds.converted_minor, pounds.converted) == ("-80.00", -10_000, "-100.00")
    assert (pounds.rate, pounds.rate_date, pounds.rate_source) == ("0.8", date(2026, 1, 1), "manual")


def test_the_report_opens_on_the_base_currency_only_with_a_working_chain(
    session, household, owner, wallets, rated
):
    offer = report_routes.report_currencies(household=household, session=session)
    assert offer.base_currency == "EUR"
    assert (offer.base_offered, offer.base_ready) == (True, False), "SEK has no rate"

    _rate(session, household, owner, "SEK", date(2026, 1, 1), "11.0")
    offer = report_routes.report_currencies(household=household, session=session)
    assert (offer.base_offered, offer.base_ready) == (True, True)

    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        household.fx_chain = ""
    session.commit()
    offer = report_routes.report_currencies(household=household, session=session)
    assert (offer.base_offered, offer.base_ready) == (True, False), "an empty chain converts nothing"


def test_a_household_in_its_base_currency_alone_is_not_offered_it(
    session, household, owner
):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        session.add(
            Account(household_id=household.id, name="Only", type=AccountType.checking, currency="EUR")
        )
    session.commit()
    offer = report_routes.report_currencies(household=household, session=session)
    assert (offer.currencies, offer.base_offered, offer.base_ready) == (["EUR"], False, False)
