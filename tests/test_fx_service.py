"""The conversion service: how a rate is found and how an amount is converted (#396 §6, §10).

Fixed rates, two households, two currencies and more, and every assertion is
on a figure. The rates are the ECB's for late October 2026, as recorded in
`tests/fixtures/fx/` -- public data, nobody's.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.audit.batch import batch
from app.models import (
    Account,
    AccountType,
    BatchKind,
    FxManualRate,
    FxPeriod,
    FxPolicy,
    FxRate,
    FxSource,
    Household,
    HouseholdMember,
    utcnow,
)
from app.money import to_minor
from app.services import fx, fx_fetching, fx_sources
from app.services import transactions as txn_service

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "tests/fixtures/fx/ecb-gbp-sek-usd-2026-10-29-to-11-04.csv"

#: The day the tests run "on": a Wednesday, the last rate in the sample.
TODAY = date(2026, 11, 4)


def _publish(session, rows: list[tuple[str, str, str]]) -> None:
    """(currency, date, rate) as the ECB publishes them, against EUR."""
    fx_fetching.store(
        session,
        FxSource.ecb,
        [fx_sources.Published("EUR", c, date.fromisoformat(d), r) for c, d, r in rows],
    )
    session.commit()


@pytest.fixture()
def ecb(session):
    """The recorded sample, stored as a fetch would store it."""
    published = fx_sources.parse_ecb_csv(SAMPLE.read_bytes())
    fx_fetching.store(session, FxSource.ecb, published)
    session.commit()
    return published


def _manual(session, household, owner, currency, day, rate, period=FxPeriod.day, quote=None):
    with batch(session, kind=BatchKind.manual, actor_id=owner.id, household_id=household.id):
        session.add(
            FxManualRate(
                household_id=household.id,
                quote_currency=quote or household.base_currency,
                currency=currency,
                date=day,
                period=period,
                rate=rate,
                created_by_id=owner.id,
            )
        )
    session.commit()


def _pounds_household(session, owner) -> Household:
    """A second household, based in GBP."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        house = Household(name="Flat", base_currency="GBP")
        session.add(house)
        session.flush()
        session.add(
            HouseholdMember(household_id=house.id, user_id=owner.id, added_by_id=owner.id, added_at=utcnow())
        )
    session.commit()
    return house


# --------------------------------------------------------------------------- #
# Reading the source
# --------------------------------------------------------------------------- #


def test_the_recorded_answer_reads_as_fifteen_rates_against_the_euro():
    published = fx_sources.parse_ecb_csv(SAMPLE.read_bytes())
    assert len(published) == 15
    assert {p.quote for p in published} == {"EUR"}
    assert fx_sources.Published("EUR", "GBP", date(2026, 10, 30), "0.8338") in published
    assert fx_sources.Published("EUR", "SEK", date(2026, 11, 4), "10.9718") in published


def test_an_empty_answer_is_no_rates_and_a_foreign_one_is_refused():
    assert fx_sources.parse_ecb_csv(b"") == []
    with pytest.raises(fx_sources.SourceError):
        fx_sources.parse_ecb_csv(b"<html>maintenance</html>\n")


def test_the_request_names_only_codes_and_dates():
    url = fx_sources.ecb_url(["SEK", "GBP"], date(2026, 10, 30), date(2026, 11, 4))
    assert url == (
        "https://data-api.ecb.europa.eu/service/data/EXR/D.GBP+SEK.EUR.SP00.A"
        "?startPeriod=2026-10-30&endPeriod=2026-11-04&format=csvdata&detail=dataonly"
    )


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #


def test_spot_on_a_weekend_takes_fridays_rate_and_says_its_date(session, household, ecb):
    rates = fx.rates(session, household, today=TODAY)
    used = rates.rate_on("GBP", date(2026, 11, 1), policy=FxPolicy.spot)  # a Sunday
    assert used.rate == Decimal("0.8338")
    assert used.rate_date == date(2026, 10, 30)
    assert used.source == "ecb"
    assert used.stale is False
    assert used.via is None, "the ECB quotes against EUR, the base"


def test_month_average_is_the_mean_of_the_months_dailies(session, household, ecb):
    rates = fx.rates(session, household, today=TODAY)
    used = rates.rate_on("GBP", date(2026, 10, 15), policy=FxPolicy.month_avg)
    assert used.rate == (Decimal("0.83578") + Decimal("0.8338")) / 2
    assert used.rate_date == date(2026, 10, 30), "the last daily the mean was taken from"


def test_month_end_is_the_last_rate_on_or_before_the_months_last_day(session, household, ecb):
    rates = fx.rates(session, household, today=TODAY)
    used = rates.rate_on("SEK", date(2026, 10, 3), policy=FxPolicy.month_end)
    assert used.rate == Decimal("10.978")
    assert used.rate_date == date(2026, 10, 30)


def test_the_households_own_setting_is_the_default_policy(session, household, owner, ecb):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        household.fx_monthly_policy = "month_end"
    session.commit()
    used = fx.rates(session, household, today=TODAY).rate_on("GBP", date(2026, 10, 1))
    assert used.rate == Decimal("0.8338")


# --------------------------------------------------------------------------- #
# Cross rates and conversion
# --------------------------------------------------------------------------- #


def test_a_pound_household_crosses_through_the_euro_to_the_last_digit(session, owner, ecb):
    house = _pounds_household(session, owner)
    rates = fx.rates(session, house, today=TODAY)

    used = rates.rate_on("SEK", date(2026, 10, 30), policy=FxPolicy.spot)
    assert used.rate == Decimal("10.978") / Decimal("0.8338")
    assert used.via == "EUR"

    # SEK 1,000.00 on 30 October, by hand: 1000 * 0.8338 / 10.978 = 75.9519...
    converted = rates.convert(100_000, "SEK", date(2026, 10, 30), policy=FxPolicy.spot)
    assert converted.amount_minor == 7_595
    assert converted.rate_used is used

    # The euro itself: one over GBP per EUR.
    euro = rates.convert(10_000, "EUR", date(2026, 10, 30), policy=FxPolicy.spot)
    assert euro.amount_minor == 8_338
    assert euro.rate_used.via is None


def test_the_base_converts_to_itself_at_one(session, household, ecb):
    converted = fx.convert(session, household, 12_345, "EUR", date(2026, 10, 30))
    assert converted.amount_minor == 12_345
    assert converted.rate_used.source == fx.BASE


@pytest.mark.parametrize(
    ("base", "currency", "rate", "amount", "expected"),
    [
        # JPY has no minor unit: EUR 1.00 at 2 EUR per JPY is 0.5 JPY, a tie.
        ("JPY", "EUR", "2", 100, 1),
        ("JPY", "EUR", "2", -100, -1),
        # EUR: GBP 0.01 at 2 GBP per EUR is 0.005 EUR, a tie at the cent.
        ("EUR", "GBP", "2", 1, 1),
        ("EUR", "GBP", "4", 2, 1),
        # KWD has three: EUR 0.01 at 20 EUR per KWD is 0.0005 KWD.
        ("KWD", "EUR", "20", 1, 1),
        ("KWD", "EUR", "20", -1, -1),
    ],
)
def test_rounding_is_money_to_minor_half_up_at_the_targets_exponent(
    session, owner, base, currency, rate, amount, expected
):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        house = Household(name=f"In {base}", base_currency=base)
        session.add(house)
    session.commit()
    _manual(session, house, owner, currency, date(2026, 10, 30), rate)

    converted = fx.convert(session, house, amount, currency, date(2026, 10, 30), policy=FxPolicy.spot)

    assert converted.amount_minor == expected
    # Half-even would have given 0 on every tie above; this is the entered-amount rule.
    assert converted.amount_minor == to_minor(
        Decimal(amount) / Decimal(10) ** (2) / Decimal(rate), base
    )


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #


def test_a_manual_rate_beats_the_ecb(session, household, owner, ecb):
    _manual(session, household, owner, "GBP", date(2026, 11, 2), "0.9")
    used = fx.rates(session, household, today=TODAY).rate_on("GBP", date(2026, 11, 3), policy=FxPolicy.spot)
    assert (used.source, used.rate) == ("manual", Decimal("0.9"))


def test_a_stale_manual_rate_loses_to_a_fresh_ecb_rate(session, household, owner, ecb):
    _manual(session, household, owner, "GBP", date(2026, 9, 1), "0.9")
    used = fx.rates(session, household, today=TODAY).rate_on("GBP", date(2026, 11, 3), policy=FxPolicy.spot)
    assert (used.source, used.rate, used.stale) == ("ecb", Decimal("0.83358"), False)


def test_with_nothing_fresh_the_newest_is_used_and_marked_stale(session, household, owner, ecb):
    used = fx.rates(session, household, today=date(2026, 12, 1)).rate_on(
        "GBP", date(2026, 12, 1), policy=FxPolicy.spot
    )
    assert used.rate_date == date(2026, 11, 4)
    assert used.stale is True, "27 days old against a limit of 7"


def test_a_monthly_manual_rate_covers_every_day_of_its_month(session, household, owner):
    _manual(session, household, owner, "BRL", date(2026, 10, 1), "6.1", period=FxPeriod.month)
    rates = fx.rates(session, household, today=TODAY)
    late = rates.rate_on("BRL", date(2026, 10, 30), policy=FxPolicy.spot)
    assert (late.rate, late.rate_date, late.stale) == (Decimal("6.1"), date(2026, 10, 1), False)
    assert rates.rate_on("BRL", date(2026, 10, 30), policy=FxPolicy.month_avg).rate == Decimal("6.1")


def test_no_rate_at_all_is_none_never_zero_or_one(session, household, ecb):
    rates = fx.rates(session, household, today=TODAY)
    assert rates.rate_on("ARS", date(2026, 10, 30)) is None
    assert rates.convert(100_000, "ARS", date(2026, 10, 30)) is None
    # Before the first rate is the same: nothing to take.
    assert rates.convert(100_000, "GBP", date(2026, 1, 1), policy=FxPolicy.spot) is None


def test_an_empty_chain_converts_nothing(session, household, owner, ecb):
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        household.fx_chain = ""
    session.commit()
    assert fx.convert(session, household, 100, "GBP", date(2026, 10, 30)) is None


def test_one_households_manual_rates_never_reach_another(session, household, other_household, owner, member):
    _manual(session, household, owner, "SEK", date(2026, 10, 30), "11")
    assert fx.rates(session, other_household, today=TODAY).rate_on("SEK", date(2026, 10, 30)) is None


def test_a_manual_rate_entered_now_is_in_the_next_request(session, household, owner, ecb):
    before = fx.rate_on(session, household, "GBP", date(2026, 11, 3), policy=FxPolicy.spot)
    _manual(session, household, owner, "GBP", date(2026, 11, 3), "0.91")
    after = fx.rate_on(session, household, "GBP", date(2026, 11, 3), policy=FxPolicy.spot)
    assert (before.rate, after.rate) == (Decimal("0.83358"), Decimal("0.91"))


# --------------------------------------------------------------------------- #
# Own rates: normalised before they are shown
# --------------------------------------------------------------------------- #


def test_an_own_rate_is_inverted_into_base_terms_and_one_without_the_base_is_left_out(
    session, owner, member
):
    house = _pounds_household(session, owner)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=house.id):
        pounds = Account(household_id=house.id, name="Pounds", type=AccountType.checking, currency="GBP")
        euros = Account(household_id=house.id, name="Euros", type=AccountType.checking, currency="EUR")
        kronor = Account(household_id=house.id, name="Kronor", type=AccountType.checking, currency="SEK")
        session.add_all([pounds, euros, kronor])
        session.flush()
        # GBP 100.00 out, EUR 115.00 in: 1.15 EUR per GBP, the base's own terms.
        txn_service.create_transfer(
            session, source=pounds, destination=euros, date=date(2026, 10, 30), amount=10_000, to_amount=11_500
        )
        # EUR 200.00 out, GBP 170.00 in: 0.85 GBP per EUR, inverted to 1/0.85.
        txn_service.create_transfer(
            session, source=euros, destination=pounds, date=date(2026, 10, 29), amount=20_000, to_amount=17_000
        )
        # SEK to EUR: neither leg in the base, so it says nothing here.
        txn_service.create_transfer(
            session, source=kronor, destination=euros, date=date(2026, 10, 28), amount=100_000, to_amount=9_000
        )
    session.commit()

    rates = fx.rates(session, house, today=TODAY)
    assert rates.own_rate("EUR", date(2026, 10, 30)) == Decimal("1.15")
    assert rates.own_rate("EUR", date(2026, 10, 29)) == Decimal(1) / Decimal("0.85")
    assert rates.own_rate("SEK", date(2026, 10, 28)) is None
    assert rates.own_rate("EUR", date(2026, 10, 28)) is None

    # Off by default, and allowed first in the chain when a household asks.
    assert rates.rate_on("EUR", date(2026, 10, 30), policy=FxPolicy.spot) is None
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=house.id):
        house.fx_chain = "own,manual,ecb"
    session.commit()
    used = fx.rates(session, house, today=TODAY).rate_on("EUR", date(2026, 10, 30), policy=FxPolicy.spot)
    assert (used.source, used.rate) == ("own", Decimal("1.15"))


# --------------------------------------------------------------------------- #
# Storing fetched rates
# --------------------------------------------------------------------------- #


def test_two_fetches_of_the_same_day_leave_one_row(session):
    rows = [fx_sources.Published("EUR", "GBP", date(2026, 10, 30), "0.8338")]
    assert fx_fetching.store(session, FxSource.ecb, rows) == (1, 0)
    session.commit()
    assert fx_fetching.store(session, FxSource.ecb, rows) == (0, 1)
    session.commit()
    assert session.execute(select(func.count()).select_from(FxRate)).scalar_one() == 1


def test_the_unique_key_holds_because_no_column_in_it_is_null(session):
    for _ in range(2):
        session.add(
            FxRate(source=FxSource.ecb, quote_currency="EUR", currency="GBP", date=date(2026, 10, 30), rate="0.8338")
        )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    assert all(not column.nullable for column in FxRate.__table__.c if column.name != "id")


def test_a_range_refetch_replaces_a_changed_rate_and_missing_does_not(session):
    fx_fetching.store(session, FxSource.ecb, [fx_sources.Published("EUR", "GBP", date(2026, 10, 30), "0.9")])
    corrected = [fx_sources.Published("EUR", "GBP", date(2026, 10, 30), "0.8338")]
    assert fx_fetching.store(session, FxSource.ecb, corrected) == (0, 1)
    assert session.execute(select(FxRate.rate)).scalar_one() == "0.9"
    assert fx_fetching.store(session, FxSource.ecb, corrected, overwrite=True) == (1, 0)
    assert session.execute(select(FxRate.rate)).scalar_one() == "0.8338"


# --------------------------------------------------------------------------- #
# rates_status
# --------------------------------------------------------------------------- #


def test_rates_status_takes_the_oldest_rate_date_and_lists_what_has_none(session, household, owner, ecb):
    _manual(session, household, owner, "BRL", date(2026, 10, 20), "6.1")
    status = fx.rates_status(
        session, household, ["GBP", "BRL", "ARS", "EUR"], rates=fx.rates(session, household, today=TODAY)
    )
    by = {c.currency: c for c in status.per_currency}
    assert set(by) == {"GBP", "BRL"}, "the base is dropped, ARS has no rate"
    assert by["GBP"].rate_date == date(2026, 11, 4)
    assert (by["BRL"].rate_date, by["BRL"].stale, by["BRL"].age_days) == (date(2026, 10, 20), True, 15)
    assert status.as_of == date(2026, 10, 20), "the weakest link sets the date"
    assert status.stale is True
    assert status.not_converted == ["ARS"]
    assert status.chain == ["manual", "ecb"]
    assert status.remote_in_chain is True
    assert status.manual_changed_by == "Jane"


def test_a_report_in_the_base_alone_has_nothing_to_say(session, household, ecb):
    status = fx.rates_status(session, household, ["EUR"])
    assert (status.per_currency, status.not_converted, status.as_of) == ([], [], None)


# --------------------------------------------------------------------------- #
# The no-convert rule moves here
# --------------------------------------------------------------------------- #

#: Where a rate is *computed* from two amounts, which is not converting one.
#: `transfers.link` and `transactions.create_transfer` store the rate a linked
#: pair implies; `review.implied_rate` recomputes it for `fx/observed`.
RATE_FROM_AMOUNTS = {"app/services/transfers.py", "app/services/review.py"}

_DIVIDES_BY_A_RATE = re.compile(r"/\s*(?:[\w.]*\.)?(?:rate|fx_rate|transfer_fx_rate|used\.rate)\b")


def test_no_module_but_fx_divides_an_amount_by_a_rate():
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "app/services/fx.py" or relative in RATE_FROM_AMOUNTS:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _DIVIDES_BY_A_RATE.search(line.split("#")[0]):
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, "convert through services/fx.py:\n" + "\n".join(offenders)


def test_the_grep_bites_on_a_planted_conversion():
    assert _DIVIDES_BY_A_RATE.search("total = amount / used.rate")
    assert _DIVIDES_BY_A_RATE.search("value = Decimal(minor) / rate")
    assert not _DIVIDES_BY_A_RATE.search("ratio = amount / count")


def test_importing_the_adapters_performs_no_io(monkeypatch):
    """Nothing fetches without a person's request -- not even an import."""
    import importlib
    import socket

    def refuse(*args, **kwargs):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("an import opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    importlib.reload(fx_sources)
    importlib.reload(fx_fetching)
