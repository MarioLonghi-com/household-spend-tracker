"""The Net Worth report (#397), asserted on the figures.

Every test names a figure worked out by hand from its fixture. The way this
report goes wrong is a 200 with a wrong total in it: two currencies added
without a rate, a card's debt counted as money, a running month treated as
closed, a part of a split that does not add up to the whole.

Exchange rates come from #396 (`services/fx.py`), reached through one seam,
`net_worth.rates_for`. Some tests hand it a small fixed table (`TableRates`) to hold the arithmetic of
§5 on its own; the tests under "Through services/fx.py" store real manual
and ECB rates and hold the household's own path to the same figures.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from app import money
from app.audit.batch import batch
from app.errors import ValidationError
from app.models import AccountType, BatchKind, FxManualRate, FxPeriod, FxSource, Household, User
from app.services import accounts as account_service
from app.services import agent_keys as key_service
from app.services import fx, fx_fetching, fx_sources, insights
from app.services import net_worth as nw
from app.services import transactions as txn_service
from tests.conftest import HEADERS, _setup_owner

#: The spec's own "today": on 9 November, October is the last closed month.
TODAY = date(2026, 11, 9)


class TableRates:
    """A fixed rate table handed to the seam: units per one EUR, by date.

    For the arithmetic of §5 without a fetch. Its `convert` divides the way
    `fx.Rates.convert` does -- `money.to_minor` of the decimal quotient -- and
    the tests further down hold the real `fx` path to the same figures.
    """

    chain = ("manual", "ecb")
    policy = "spot"

    def __init__(self, table: dict[str, list[tuple[date, str]]], stale_days: int = 7):
        self.table = {code: sorted(rows) for code, rows in table.items()}
        self.stale_days = stale_days
        self.asked_status: list[list[str]] = []

    def available(self) -> bool:
        return True

    def rate_on(self, currency: str, on_date: date) -> fx.RateUsed | None:
        found = [row for row in self.table.get(currency, []) if row[0] <= on_date]
        if not found:
            return None
        when, rate = found[-1]
        return fx.RateUsed(
            rate=Decimal(rate),
            rate_date=when,
            source="manual",
            stale=(on_date - when).days > self.stale_days,
        )

    def convert(self, minor: int, currency: str, on_date: date) -> fx.Converted | None:
        used = self.rate_on(currency, on_date)
        if used is None:
            return None
        value = money.to_decimal(minor, currency) / used.rate
        return fx.Converted(amount_minor=money.to_minor(value, "EUR"), rate_used=used)

    def status(self, currencies: list[str]) -> fx.RatesStatus:
        self.asked_status.append(list(currencies))
        return fx.RatesStatus(
            as_of=None, stale=False, running=False, fetched_at=None, fetched_by=None,
            last_fetch_failed=None, chain=list(self.chain), stale_days=self.stale_days,
            remote_in_chain=True, manual_changed_at=None, manual_changed_by=None,
            per_currency=[], not_converted=list(currencies),
        )


def _account(session, household, owner, name, type_, currency, **kw):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        return account_service.create_account(
            session, household=household, name=name, type=type_, currency=currency, **kw
        )


def _row(session, household, owner, account, when, amount):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        return txn_service.create(session, account=account, date=when, amount=amount)


@pytest.fixture()
def ledger(session, household, other_household, owner, member):
    """Six accounts in two currencies, every type, and a second household.

    Base currency EUR. Opening balances on 1 Sep 2026 unless said otherwise.

    - Santander (EUR checking, ES, "Banco Santander"): 5,000.00, -300.00 on 15 Oct
    - Visa (EUR credit card, ES, "Banco Santander"): -200.00 on 20 Oct
    - Loan (EUR other_liability, no country, no bank): -1,000.00
    - Car (EUR other_asset, ES, no bank): 3,000.00
    - Monzo (GBP savings, GB, "Monzo"): 1,000.00
    - Old (GBP checking, GB, "Monzo"), closed: 50.00 in Sep, emptied 30 Sep
    - Somebody else's EUR account, which must never appear.
    """
    sep1 = date(2026, 9, 1)
    santander = _account(
        session, household, owner, "Santander", "checking", "EUR",
        country="ES", institution="Banco Santander", opening_balance=500000, opening_date=sep1,
    )
    visa = _account(
        session, household, owner, "Visa", "credit_card", "EUR",
        country="ES", institution="Banco Santander",
    )
    loan = _account(
        session, household, owner, "Loan", "other_liability", "EUR",
        opening_balance=-100000, opening_date=sep1,
    )
    car = _account(
        session, household, owner, "Car", "other_asset", "EUR",
        country="ES", opening_balance=300000, opening_date=sep1,
    )
    monzo = _account(
        session, household, owner, "Monzo", "savings", "GBP",
        country="GB", institution="Monzo", opening_balance=100000, opening_date=sep1,
    )
    old = _account(
        session, household, owner, "Old", "checking", "GBP",
        country="GB", institution="Monzo", opening_balance=5000, opening_date=sep1,
    )
    _row(session, household, owner, santander, date(2026, 10, 15), -30000)
    _row(session, household, owner, visa, date(2026, 10, 20), -20000)
    _row(session, household, owner, old, date(2026, 9, 30), -5000)
    # In November: after the last closed month, so only "Today" sees it.
    _row(session, household, owner, santander, date(2026, 11, 3), -1000)
    # In the future: counts only once its date has passed.
    _row(session, household, owner, santander, date(2026, 11, 20), -999)
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        old.closed = True

    with batch(
        session, kind=BatchKind.manual, actor_id=member.id, household_id=other_household.id
    ):
        account_service.create_account(
            session, household=other_household, name="Theirs", type="checking",
            currency="EUR", opening_balance=777777, opening_date=sep1,
        )
    session.commit()
    return {
        "santander": santander, "visa": visa, "loan": loan, "car": car,
        "monzo": monzo, "old": old,
    }


def _report(session, household, **kw):
    kw.setdefault("since", date(2026, 9, 1))
    kw.setdefault("until", date(2026, 11, 30))
    kw.setdefault("today", TODAY)
    return nw.report(session, household, **kw)


EUR_ONLY = nw.Filters(currencies=["EUR"])


# --------------------------------------------------------------------------- #
# Balances
# --------------------------------------------------------------------------- #


def test_each_points_balance_is_the_one_insights_and_accounts_give(session, household, ledger):
    """§9: the balance at every point is `balances(as_of=...)` for every account."""
    answer = _report(session, household, closed_only=False, include_closed_accounts=True)
    points = [one.date for one in answer.currencies[0].points]
    assert points == [date(2026, 9, 30), date(2026, 10, 31), TODAY]

    history = {
        account_id: nw._history(moves, points)
        for account_id, moves in insights.monthly_movements(
            session, household.id, until=TODAY
        ).items()
    }
    for at, day in enumerate(points):
        expected = dict(
            (account.id, total) for account, total in insights.balances(session, household.id, as_of=day)
        )
        for account in ledger.values():
            assert (history[account.id][at] or 0) == expected[account.id], (account.name, day)
            per_account = account_service.balances(session, account.id, as_of=day)["balance"]
            assert (history[account.id][at] or 0) == per_account


def test_balances_are_one_query_whatever_the_number_of_points(session, household, ledger):
    """§5.2 and §9: one SQL round trip for balances, for 3 points or 36."""
    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args):
        if "FROM transactions" in statement:
            statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        short = _report(session, household, filters=EUR_ONLY)
        seen_short = len(statements)
        statements.clear()
        long = _report(session, household, filters=EUR_ONLY, since=date(2023, 11, 1))
        seen_long = len(statements)
    finally:
        event.remove(engine, "before_cursor_execute", count)

    assert len(short.points) == 2 and len(long.points) == 36
    assert seen_short == 1 and seen_long == 1


# --------------------------------------------------------------------------- #
# Closed months
# --------------------------------------------------------------------------- #


def test_closed_months_stop_at_the_last_month_that_has_ended(session, household, ledger):
    answer = _report(session, household, filters=EUR_ONLY)

    assert [one.date for one in answer.points] == [date(2026, 9, 30), date(2026, 10, 31)]
    assert answer.as_of == date(2026, 10, 31)
    assert answer.clipped_from == TODAY
    assert not any(one.today for one in answer.points)
    # 5,000 - 300 - 200 - 1,000 + 3,000: neither November row is in it.
    assert answer.headline.net_worth == 650000


def test_this_month_shows_the_last_closed_month_instead(session, household, ledger):
    answer = _report(
        session, household, filters=EUR_ONLY, since=date(2026, 11, 1), until=date(2026, 11, 30)
    )
    assert [one.date for one in answer.points] == [date(2026, 10, 31)]
    assert answer.clipped_from == TODAY
    assert answer.headline.net_worth == 650000
    assert answer.headline.change_prev is None and answer.headline.change_range is None


def test_a_range_that_ended_before_the_running_month_is_not_clipped(session, household, ledger):
    answer = _report(session, household, filters=EUR_ONLY, until=date(2026, 9, 30))
    assert [one.date for one in answer.points] == [date(2026, 9, 30)]
    assert answer.clipped_from is None


def test_with_closed_months_off_the_last_point_is_today(session, household, ledger):
    answer = _report(session, household, filters=EUR_ONLY, closed_only=False)

    assert [one.date for one in answer.points] == [date(2026, 9, 30), date(2026, 10, 31), TODAY]
    assert [one.today for one in answer.points] == [False, False, True]
    assert answer.as_of == TODAY and answer.clipped_from is None
    # The 3 November row counts; the one dated 20 November has not happened.
    assert answer.headline.net_worth == 650000 - 1000
    assert answer.headline.change_prev == -1000
    assert answer.headline.prev_date == date(2026, 10, 31)


def test_all_dates_starts_at_the_first_row_in_scope(session, household, ledger):
    answer = _report(session, household, filters=EUR_ONLY, since=None, until=None)
    assert answer.since == date(2026, 9, 1)
    assert [one.date for one in answer.points] == [date(2026, 9, 30), date(2026, 10, 31)]


def test_a_range_that_ends_before_it_starts_is_refused_with_a_code(session, household, ledger):
    with pytest.raises(ValidationError) as caught:
        _report(session, household, since=date(2026, 10, 1), until=date(2026, 9, 1))
    assert caught.value.code == "report.range_backwards"
    assert caught.value.params == {"since": "2026-10-01", "until": "2026-09-01"}


# --------------------------------------------------------------------------- #
# Liabilities, types and splits
# --------------------------------------------------------------------------- #


def test_other_liability_subtracts_like_a_card_and_other_asset_counts(session, household, ledger):
    """§9: by their own sign. Nothing flips one."""
    answer = _report(session, household, filters=EUR_ONLY, split=nw.Split.type)
    end = answer.points[-1]

    assert end.parts == {
        "checking": 470000, "credit_card": -20000, "other_liability": -100000,
        "other_asset": 300000,
    }
    assert end.liabilities == -20000 - 100000
    assert answer.headline.liabilities == -120000
    assert end.net_worth == 650000


@pytest.mark.parametrize("split", list(nw.Split))
def test_every_splits_parts_add_up_to_the_total_exactly(session, household, ledger, split):
    rates = TableRates({"GBP": [(date(2026, 8, 31), "0.8333")]})
    answer = _report(session, household, split=split, rates=rates, closed_only=False)
    for point in answer.points:
        assert sum(point.parts.values()) == point.net_worth, (split, point.date)
    assert sum(line.value for line in answer.held) == answer.headline.net_worth
    for book in answer.currencies:
        for point in book.points:
            assert sum(point.parts.values()) == point.net_worth


def test_a_split_by_bank_and_country_files_the_unset_under_the_empty_key(
    session, household, ledger
):
    by_bank = _report(session, household, filters=EUR_ONLY, split=nw.Split.institution)
    assert by_bank.points[-1].parts == {"Banco Santander": 450000, "": 200000}
    by_country = _report(session, household, filters=EUR_ONLY, split=nw.Split.country)
    assert by_country.points[-1].parts == {"ES": 750000, "": -100000}


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #


def _names(answer) -> set[str]:
    return {one.name for one in answer.accounts}


def test_choices_in_one_filter_widen_and_filters_narrow(session, household, ledger):
    both_banks = _report(
        session, household, filters=nw.Filters(institutions=["Banco Santander", "Monzo"])
    )
    assert _names(both_banks) == {"Santander", "Visa", "Monzo"}

    narrowed = _report(
        session, household,
        filters=nw.Filters(institutions=["Banco Santander", "Monzo"], countries=["GB"]),
    )
    assert _names(narrowed) == {"Monzo"}
    assert narrowed.display_currency == "GBP"
    assert narrowed.headline.net_worth == 100000


def test_no_bank_set_and_no_country_set_are_choices(session, household, ledger):
    no_bank = _report(session, household, filters=nw.Filters(institutions=[nw.UNSET]))
    assert _names(no_bank) == {"Loan", "Car"}
    assert no_bank.headline.net_worth == 200000

    no_country = _report(session, household, filters=nw.Filters(countries=[nw.UNSET]))
    assert _names(no_country) == {"Loan"}
    assert no_country.headline.net_worth == -100000


def test_the_account_filter_narrows_inside_the_others(session, household, ledger):
    answer = _report(
        session, household,
        filters=nw.Filters(currencies=["EUR"], account_ids=[ledger["visa"].id, ledger["monzo"].id]),
    )
    assert _names(answer) == {"Visa"}
    assert answer.headline.net_worth == -20000


def test_no_account_matching_gives_an_empty_answer_not_a_zero_total(session, household, ledger):
    answer = _report(session, household, filters=nw.Filters(countries=["JP"]))
    assert answer.explanation.accounts_counted == 0
    assert answer.headline is None and answer.points == [] and answer.currencies == []


def test_a_closed_account_leaves_the_table_and_keeps_its_past(session, household, ledger):
    gbp = _report(session, household, filters=nw.Filters(currencies=["GBP"]),
                  since=date(2026, 8, 1))
    # The closed account held 50.00 through September and nothing after.
    assert _names(gbp) == {"Monzo"}
    assert gbp.explanation.accounts_hidden_closed == 1
    assert [one.accounts_without_data for one in gbp.points] == [2, 0, 0]
    assert [one.net_worth for one in gbp.points] == [0, 100000, 100000]

    shown = _report(session, household, filters=nw.Filters(currencies=["GBP"]),
                    include_closed_accounts=True)
    assert _names(shown) == {"Monzo", "Old"}
    assert shown.explanation.accounts_hidden_closed == 0


def test_another_households_accounts_never_count(session, household, ledger):
    answer = _report(session, household, filters=EUR_ONLY)
    assert "Theirs" not in _names(answer)
    assert answer.explanation.accounts_counted == 4


# --------------------------------------------------------------------------- #
# Currencies and rates
# --------------------------------------------------------------------------- #


def test_one_currency_needs_no_rate_and_converts_nothing(session, household, ledger):
    answer = _report(session, household, filters=nw.Filters(currencies=["GBP"]),
                     rates=TableRates({}))
    assert answer.display_currency == "GBP"
    assert answer.converted is False
    assert answer.rate_source is None and answer.rates_status is None
    assert answer.headline.net_worth == 100000
    assert [line.value for line in answer.accounts] == [100000]
    assert answer.explanation.rates == []


def test_two_currencies_and_no_rates_give_each_its_own_total_and_no_sum(
    session, household, ledger
):
    answer = _report(session, household)

    assert answer.display_currency is None and answer.converted is False
    assert answer.headline is None and answer.bridge is None
    assert answer.points == [] and answer.held == []
    books = {book.currency: book for book in answer.currencies}
    assert books["EUR"].headline.net_worth == 650000
    assert books["GBP"].headline.net_worth == 100000
    assert books["GBP"].held[0].native_minor == 100000
    # Nothing anywhere is the sum of the two.
    assert all(line.value is None for line in answer.accounts)
    assert "750000" not in repr(answer)


def test_the_synthetic_example_gives_exactly_the_four_numbers(session, household, owner):
    """§5.6, to the cent."""
    eur = _account(session, household, owner, "Savings EUR", "savings", "EUR",
                   opening_balance=1000000, opening_date=date(2025, 10, 1))
    _account(session, household, owner, "Savings GBP", "savings", "GBP",
             opening_balance=800000, opening_date=date(2025, 10, 1))
    _row(session, household, owner, eur, date(2026, 5, 1), 200000)
    rates = TableRates({"GBP": [(date(2025, 10, 31), "0.8000"), (date(2026, 10, 30), "0.8500")]})

    answer = _report(session, household, since=date(2025, 10, 1), until=date(2026, 10, 31),
                     rates=rates)

    assert answer.display_currency == "EUR" and answer.converted is True
    assert (answer.points[0].date, answer.points[-1].date) == (date(2025, 10, 31), date(2026, 10, 31))
    assert answer.bridge.start == 2000000
    assert answer.bridge.end == 2141176
    assert answer.bridge.fx_effect == -58824
    assert answer.bridge.money_added_or_spent == 200000
    assert answer.headline.change_range == 141176
    # The worked line §4.6 shows: £8,000.00 ÷ 0.8500 = €9,411.76.
    largest = answer.explanation.largest_foreign
    assert (largest.native_minor, largest.rate, largest.value) == (800000, "0.85", 941176)
    gbp = next(line for line in answer.held if line.key == "GBP")
    assert (gbp.native_minor, gbp.rate, gbp.rate_date, gbp.value) == (
        800000, "0.85", date(2026, 10, 30), 941176
    )
    # The rate is a day older than the "as of" date: the last one before it.
    assert answer.explanation.rates[0].before_as_of is True
    assert answer.rate_source.chain == ["manual", "ecb"]
    assert rates.asked_status == [["GBP"]]


def test_a_currency_with_no_rate_is_left_out_listed_and_named(session, household, ledger, owner):
    yen = _account(session, household, owner, "Yen", "savings", "JPY",
                   opening_balance=50000, opening_date=date(2026, 9, 1))
    rates = TableRates({"GBP": [(date(2026, 9, 30), "0.8")]})

    answer = _report(session, household, rates=rates)

    # 650,000 EUR + 1,000.00 GBP / 0.8; the yen are not in it.
    assert answer.headline.net_worth == 650000 + 125000
    assert [(one.currency, one.native_minor, one.accounts) for one in answer.not_converted] == [
        ("JPY", 50000, 1)
    ]
    assert answer.explanation.missing == ["JPY"]
    assert answer.points[-1].missing_rates == ["JPY"]
    line = next(one for one in answer.accounts if one.id == yen.id)
    assert line.value is None and line.native_minor == 50000


def test_a_stale_rate_still_converts_and_is_flagged(session, household, ledger):
    rates = TableRates({"GBP": [(date(2026, 9, 1), "0.8")]})
    answer = _report(session, household, rates=rates)

    assert answer.headline.net_worth == 650000 + 125000
    assert answer.points[-1].stale_rate is True
    assert answer.explanation.stale is True
    assert answer.explanation.rates[0].stale is True


def test_rates_status_names_only_the_converted_currencies_and_is_absent_with_one(
    session, household, ledger
):
    rates = TableRates({"GBP": [(date(2026, 9, 30), "0.8")]})
    converted = _report(session, household, rates=rates)
    single = _report(session, household, rates=rates, filters=EUR_ONLY)

    assert rates.asked_status == [["GBP"]]
    assert converted.rates_status.not_converted == ["GBP"]
    assert single.rates_status is None


def test_a_cross_currency_transfer_moves_the_total_only_by_the_conversion(
    session, household, owner
):
    eur = _account(session, household, owner, "EUR", "checking", "EUR",
                   opening_balance=100000, opening_date=date(2026, 9, 1))
    gbp = _account(session, household, owner, "GBP", "checking", "GBP")
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        txn_service.create_transfer(
            session, source=eur, destination=gbp, date=date(2026, 10, 15),
            amount=10000, to_amount=8600,
        )

    at_its_own_rate = _report(
        session, household, until=date(2026, 10, 31),
        rates=TableRates({"GBP": [(date(2026, 9, 1), "0.86")]}),
    )
    assert [one.net_worth for one in at_its_own_rate.points] == [100000, 100000]

    at_another = _report(
        session, household, until=date(2026, 10, 31),
        rates=TableRates({"GBP": [(date(2026, 9, 1), "0.80")]}),
    )
    # -100.00 EUR, +86.00 GBP worth 107.50: the total moves by 7.50 alone.
    assert at_another.headline.change_prev == 750
    assert at_another.bridge.fx_effect == 0
    assert at_another.bridge.money_added_or_spent == 750


def test_an_account_opened_in_the_range_counts_as_zero_and_is_marked(session, household, owner):
    _account(session, household, owner, "First", "checking", "EUR",
             opening_balance=1000, opening_date=date(2026, 8, 1))
    _account(session, household, owner, "Later", "savings", "EUR",
             opening_balance=2000, opening_date=date(2026, 10, 2))

    answer = _report(session, household, since=date(2026, 8, 1), until=date(2026, 10, 31))
    assert [one.accounts_without_data for one in answer.points] == [1, 1, 0]
    assert [one.net_worth for one in answer.points] == [1000, 1000, 3000]
    assert answer.explanation.accounts_without_data_at_start == 1


# --------------------------------------------------------------------------- #
# Through services/fx.py: the household's own stored rates
# --------------------------------------------------------------------------- #


def _manual_rate(session, household, owner, currency, day, rate):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        session.add(
            FxManualRate(
                household_id=household.id, quote_currency=household.base_currency,
                currency=currency, date=day, period=FxPeriod.day, rate=rate,
                created_by_id=owner.id,
            )
        )
    session.commit()


def _ecb(session, rows: list[tuple[str, str, str]]) -> None:
    fx_fetching.store(
        session, FxSource.ecb,
        [fx_sources.Published("EUR", code, date.fromisoformat(day), rate) for code, day, rate in rows],
    )
    session.commit()


def test_the_synthetic_example_through_the_households_own_rates(session, household, owner):
    """§5.6 again, with the rates stored as a person would enter them."""
    eur = _account(session, household, owner, "Savings EUR", "savings", "EUR",
                   opening_balance=1000000, opening_date=date(2025, 10, 1))
    _account(session, household, owner, "Savings GBP", "savings", "GBP",
             opening_balance=800000, opening_date=date(2025, 10, 1))
    _row(session, household, owner, eur, date(2026, 5, 1), 200000)
    _manual_rate(session, household, owner, "GBP", date(2025, 10, 31), "0.8000")
    _manual_rate(session, household, owner, "GBP", date(2026, 10, 30), "0.8500")

    answer = _report(session, household, since=date(2025, 10, 1), until=date(2026, 10, 31))

    assert answer.converted is True and answer.display_currency == "EUR"
    assert (answer.bridge.start, answer.bridge.end) == (2000000, 2141176)
    assert (answer.bridge.fx_effect, answer.bridge.money_added_or_spent) == (-58824, 200000)
    assert answer.rate_source.chain == ["manual", "ecb"]
    assert answer.rate_source.policy == "spot"
    rate = answer.explanation.rates[0]
    assert (rate.currency, rate.rate, rate.rate_date, rate.source) == (
        "GBP", "0.85", date(2026, 10, 30), "manual"
    )
    assert answer.rates_status.per_currency[0].currency == "GBP"
    assert answer.rates_status.not_converted == []


def test_a_balance_converts_at_the_spot_rate_not_the_monthly_average(
    session, household, owner
):
    """The household's monthly policy is for flows; a balance is one day's."""
    assert household.fx_monthly_policy == "month_avg"
    _account(session, household, owner, "Sterling", "savings", "GBP",
             opening_balance=100000, opening_date=date(2026, 9, 1))
    _account(session, household, owner, "Euro", "checking", "EUR",
             opening_balance=100, opening_date=date(2026, 9, 1))
    _ecb(session, [("GBP", "2026-10-01", "0.5"), ("GBP", "2026-10-30", "0.8")])

    answer = _report(session, household, since=date(2026, 10, 1), until=date(2026, 10, 31))
    # 1,000.00 GBP at 0.8 (30 Oct, the last before the 31st), not at the mean 0.65.
    assert answer.headline.net_worth == 125000 + 100
    assert answer.held[0].rate_date == date(2026, 10, 30)


def test_rates_status_is_the_oldest_rate_on_screen_and_absent_with_one_currency(
    session, household, owner
):
    """§9: `rates_status.as_of` is the weakest link; one currency has no line."""
    for code in ("GBP", "SEK"):
        _account(session, household, owner, code, "savings", code,
                 opening_balance=1000, opening_date=date(2026, 9, 1))
    _account(session, household, owner, "Euro", "checking", "EUR",
             opening_balance=1000, opening_date=date(2026, 9, 1))
    _ecb(session, [("GBP", "2026-11-06", "0.88"), ("SEK", "2026-11-02", "11.1")])

    both = _report(session, household)
    assert [one.currency for one in both.rates_status.per_currency] == ["GBP", "SEK"]
    assert both.rates_status.as_of == date(2026, 11, 2)

    alone = _report(session, household, filters=nw.Filters(currencies=["GBP"]))
    assert alone.rates_status is None and alone.converted is False


def test_a_chain_with_no_rate_yet_shows_each_currency_and_says_why(session, household, ledger):
    """The default chain, never fetched: no total, and the line says there are no rates."""
    answer = _report(session, household)

    assert answer.converted is False and answer.headline is None
    assert {book.currency for book in answer.currencies} == {"EUR", "GBP"}
    assert answer.rates_status.per_currency == []
    assert answer.rates_status.not_converted == ["GBP"]
    assert answer.rates_status.remote_in_chain is True


def test_an_empty_chain_converts_nothing_and_shows_no_rates_line(session, household, ledger, owner):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        household.fx_chain = ""
    session.commit()
    _ecb(session, [("GBP", "2026-10-30", "0.8")])

    answer = _report(session, household)
    assert answer.converted is False and answer.rates_status is None
    assert answer.headline is None


def test_a_stale_stored_rate_converts_and_is_flagged(session, household, owner, ledger):
    _ecb(session, [("GBP", "2026-09-01", "0.8")])
    answer = _report(session, household)

    assert answer.converted is True
    assert answer.headline.net_worth == 650000 + 125000
    assert answer.points[-1].stale_rate is True
    assert answer.explanation.stale is True
    assert answer.rates_status.stale is True


# --------------------------------------------------------------------------- #
# Over HTTP: the person, the agent, and someone else's household
# --------------------------------------------------------------------------- #


@pytest.fixture()
def world(client):
    owner = _setup_owner(client)
    house = client.post("/api/households", json={"name": "Home"}, headers=HEADERS).json()
    with Session(client.app_module.db_engine, expire_on_commit=False) as s:
        user = s.get(User, owner["user"]["id"])
        home = s.get(Household, house["id"])
        # A real household this person is not a member of.
        with batch(s, kind=BatchKind.admin, actor_id=user.id):
            away = Household(name="Elsewhere", base_currency="EUR")
            s.add(away)
        with batch(s, kind=BatchKind.manual, actor_id=user.id, household_id=home.id):
            for name, type_, currency, country, bank, opening in (
                ("Santander", "checking", "EUR", "ES", "Banco Santander", 400000),
                ("Visa", "credit_card", "EUR", "ES", "Banco Santander", -25000),
                ("Monzo", "savings", "GBP", "GB", "Monzo", 90000),
                ("Pocket", "cash", "GBP", None, None, 1000),
            ):
                account_service.create_account(
                    s, household=home, name=name, type=type_, currency=currency,
                    country=country, institution=bank,
                    opening_balance=opening, opening_date=date(2025, 1, 1),
                )
        with batch(s, kind=BatchKind.manual, actor_id=user.id, household_id=away.id):
            account_service.create_account(
                s, household=away, name="Theirs", type="checking", currency="EUR",
                opening_balance=5, opening_date=date(2025, 1, 1),
            )
        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=home.id):
            _key, token = key_service.issue(s, user=user, household=home, label="the analyst")
        s.commit()
    return {"house": house["id"], "other": away.id, "token": token}


def _person(client, house, **params):
    return client.get(f"/api/households/{house}/reports/net-worth", params=params, headers=HEADERS)


def _agent(client, world, house=None, **params):
    return client.get(
        f"/api/agent/v1/households/{house or world['house']}/reports/net-worth",
        params=params,
        headers={"authorization": f"Bearer {world['token']}"},
    )


def test_the_person_and_the_agent_get_the_same_answer(client, world):
    params = {"since": "2025-06-01", "until": "2025-08-31", "split": "institution",
              "institution": ["Banco Santander", ""]}
    person = _person(client, world["house"], **params)
    agent = _agent(client, world, **params)
    assert person.status_code == 200, person.text
    assert agent.json() == person.json()

    body = person.json()
    # Santander, Visa and the bankless Pocket: two currencies, so no total.
    assert body["display_currency"] is None
    assert body["headline"] is None
    books = {one["currency"]: one for one in body["currencies"]}
    assert books["EUR"]["headline"]["net_worth"] == 375000
    assert books["GBP"]["headline"]["net_worth"] == 1000
    assert books["EUR"]["points"][-1]["parts"] == {"Banco Santander": 375000}
    assert books["GBP"]["points"][-1]["parts"] == {"": 1000}
    assert [one["date"] for one in body["currencies"][0]["points"]] == [
        "2025-06-30", "2025-07-31", "2025-08-31"
    ]


def test_one_currency_over_http_is_exact(client, world):
    body = _person(client, world["house"], currency="eur", since="2025-06-01",
                   until="2025-06-30").json()
    assert body["display_currency"] == "EUR"
    assert body["headline"]["net_worth"] == 375000
    assert body["headline"]["liabilities"] == -25000
    assert {one["name"]: one["value"] for one in body["accounts"]} == {
        "Santander": 400000, "Visa": -25000
    }
    assert body["held"] == [
        {"key": "EUR", "native_minor": 375000, "native_currency": "EUR", "rate": None,
         "rate_date": None, "value": 375000, "share": "1.0000"}
    ]


def test_the_route_uses_the_seam_for_rates(client, world, monkeypatch):
    rates = TableRates({"GBP": [(date(2025, 1, 1), "0.9")]})
    monkeypatch.setattr(nw, "rates_for", lambda _session, _household, today=None: rates)
    body = _person(client, world["house"], since="2025-06-01", until="2025-06-30").json()

    assert body["display_currency"] == "EUR" and body["converted"] is True
    # 3,750.00 EUR + 910.00 GBP / 0.9 = 1,011.11
    assert body["headline"]["net_worth"] == 375000 + 101111
    assert body["rate_source"] == {"chain": ["manual", "ecb"], "policy": "spot"}


def test_over_http_a_stored_rate_converts_and_the_agent_agrees(client, world):
    with Session(client.app_module.db_engine, expire_on_commit=False) as s:
        _ecb(s, [("GBP", "2025-06-27", "0.9")])
    params = {"since": "2025-06-01", "until": "2025-06-30"}
    person = _person(client, world["house"], **params).json()
    assert _agent(client, world, **params).json() == person

    assert person["display_currency"] == "EUR" and person["converted"] is True
    # 3,750.00 EUR + 910.00 GBP / 0.9 = 1,011.11
    assert person["headline"]["net_worth"] == 375000 + 101111
    assert person["rate_source"] == {"chain": ["manual", "ecb"], "policy": "spot"}
    status = person["rates_status"]
    assert [one["currency"] for one in status["per_currency"]] == ["GBP"]
    assert status["per_currency"][0]["rate_date"] == "2025-06-27"
    assert status["as_of"] == "2025-06-27"


def test_a_backwards_range_is_a_422_with_its_code(client, world):
    answer = _person(client, world["house"], since="2025-09-01", until="2025-06-30")
    assert answer.status_code == 422
    assert answer.json()["code"] == "report.range_backwards"


def test_another_households_id_is_a_404_for_both(client, world):
    assert _person(client, world["other"]).status_code == 404
    assert _agent(client, world, house=world["other"]).status_code == 404
    assert _person(client, "not-a-real-id").status_code == 404


def test_an_unknown_split_is_refused(client, world):
    assert _person(client, world["house"], split="planet").status_code == 422


def test_the_account_types_are_all_accounted_for():
    """§1: every AccountType is in scope; a new one has to be decided here."""
    assert {one.value for one in AccountType} == {
        "checking", "savings", "cash", "credit_card", "other_asset", "other_liability"
    }
    assert {one for one in AccountType if one.is_liability} == {
        AccountType.credit_card, AccountType.other_liability
    }
