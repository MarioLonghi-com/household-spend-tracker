"""Reports: read-only arithmetic over the ledger, one currency at a time.

Or, for Income vs Expense, every currency in the household's base currency
(#396 phase 2): `in_base=true` instead of a `currency`. Every row is then
converted on its own through `services/fx.py`, the response says which rates
it used and which rows it could not convert, and it carries `rates_status`
for the "Rates as of" line. Nothing converted is stored.

Every route here is a `GET` and nothing in this module opens a batch. A report
is a question, not an act -- it stores nothing, changes nothing and has no undo
because it has nothing to undo. That is worth saying once at the top rather
than per route, because it is what makes this the one router that needs no
`batch(...)` and no `actor_id`.

Scoping is `CurrentHousehold`, so a household you are not in answers **404**
rather than 403, like everything else.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, Query

from ...errors import ValidationError
from ...models import Household
from ...money import format_amount
from ...schemas import (
    CoverageOut,
    ExcludedOut,
    IncomeExpenseOut,
    NotConvertedOut,
    RateSourceOut,
    RatesStatusOut,
    RateUsedOut,
    ReimbursementsOut,
    ReportBehindOut,
    ReportCurrenciesOut,
    ReportEntryOut,
    ReportLineOut,
    ReportReimbursementsOut,
    ReportRowOut,
    ReportSectionOut,
)
from ...services import fx
from ...services import reporting as report_service
from ..deps import CurrentHousehold, SessionDep

router = APIRouter(tags=["reports"])


def _row_out(row: report_service.Row, currency: str) -> ReportRowOut:
    return ReportRowOut(
        key=row.key,
        name=row.name,
        name_code="unset.category" if row.key is None and row.line is None else None,
        group_name=row.group_name,
        line=row.line,
        by_month=row.by_month,
        total_minor=row.total_minor,
        total=format_amount(row.total_minor, currency, with_symbol=False),
        average_minor=row.average_minor,
        average=format_amount(row.average_minor, currency, with_symbol=False),
        count=row.count,
    )


def _line_out(line: report_service.Line, currency: str) -> ReportLineOut:
    return ReportLineOut(
        key=line.key,
        name=line.name,
        by_month=line.by_month,
        total_minor=line.total_minor,
        total=format_amount(line.total_minor, currency, with_symbol=False),
        average_minor=line.average_minor,
        average=format_amount(line.average_minor, currency, with_symbol=False),
        count=line.count,
    )


def _section_out(section: report_service.Section, currency: str) -> ReportSectionOut:
    return ReportSectionOut(
        rows=[_row_out(row, currency) for row in section.rows],
        groups=[_line_out(line, currency) for line in section.groups],
        by_month=section.by_month,
        total_minor=section.total_minor,
        total=format_amount(section.total_minor, currency, with_symbol=False),
        average_minor=section.average_minor,
        average=format_amount(section.average_minor, currency, with_symbol=False),
        count=section.count,
    )


def _which(household: Household, session, currency: str | None, in_base: bool):
    """``(currency, rates)`` for the service: exactly one of the two is set.

    Neither or both is refused with a code: a report that guessed which was
    meant would answer a question nobody asked, in the wrong currency.
    """
    if (currency is None) == (not in_base):
        raise ValidationError(
            "Ask for one currency, or for every currency in the base currency -- one of the two",
            code="report.currency_or_base",
        )
    if in_base:
        return None, fx.rates(session, household)
    return currency.upper(), None


@router.get(
    "/households/{household_id}/reports/currencies", response_model=ReportCurrenciesOut
)
def report_currencies(
    household: CurrentHousehold, session: SessionDep
) -> ReportCurrenciesOut:
    """Which currencies there is a report to be had about.

    Read from the accounts rather than from `Household.base_currency`, which is
    a default for new accounts and not a statement about what the household
    holds. Busiest first, so the report opens on the currency most of the
    ledger is in rather than on whichever one sorts first alphabetically --
    unless the household has a working chain of exchange rates, when
    `base_ready` says to open on "All, in {base}" instead (#396 §8).
    """
    currencies = report_service.currencies_in_use(session, household.id)
    rates = fx.rates(session, household)
    offered, ready = report_service.base_offer(rates, currencies)
    return ReportCurrenciesOut(
        currencies=currencies,
        base_currency=rates.base,
        base_offered=offered,
        base_ready=ready,
    )


def _converted_fields(
    session, household: Household, converting: report_service.Converting | None
) -> dict:
    """What a converted report says about its own conversion. Nothing per currency."""
    if converting is None:
        return {}
    rates = converting.rates
    return {
        "converted": True,
        "rate_source": RateSourceOut(
            chain=[source.value for source in rates.chain], policy=rates.monthly_policy.value
        ),
        "rates_used": [
            RateUsedOut(**{**used, "rate": fx.rate_text(used["rate"])})
            for used in converting.rates_used()
        ],
        "not_converted": [NotConvertedOut(**one) for one in converting.not_converted()],
        "rates_status": RatesStatusOut.model_validate(
            fx.rates_status(session, household, converting.currencies, rates=rates)
        ),
    }


@router.get(
    "/households/{household_id}/reports/income-expense", response_model=IncomeExpenseOut
)
def income_expense(
    household: CurrentHousehold,
    session: SessionDep,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    in_base: bool = False,
    since: Annotated[Date | None, Query()] = None,
    until: Annotated[Date | None, Query()] = None,
    account_id: Annotated[list[str] | None, Query()] = None,
    category_id: Annotated[list[str] | None, Query()] = None,
    include_uncategorised: bool = True,
) -> IncomeExpenseOut:
    """Income against expense across the window, broken down by category.

    **One currency, or all of them converted** -- and the caller says which.
    `currency` names one: the ledger never converts, so that report adds
    nothing across two currencies. `in_base=true` instead asks for every
    currency in the household's base currency, each row converted on its own
    at the household's chosen rates (#396 phase 2): `converted` is then true,
    `rates_used` and `not_converted` say how, and `rates_status` says how old
    the rates are. Neither or both is refused (`report.currency_or_base`).
    The client's currency toggle is the shape this takes on screen.

    `account_id` and `category_id` repeat for several values -- the accounts
    filter groups by country and type on the client, but that is a way of
    *choosing* ids rather than a different filter, so it arrives here as the
    same flat list and this route does not need to know the grouping existed.

    `include_uncategorised` is separate from `category_id` because
    "Uncategorised" is a slice a person can tick like any other but is not an
    id, so a list of ids cannot express it. On the real ledger it is 99.2% of
    outflow value, which is why it is a first-class filter rather than an
    "Other" bucket.

    `since` and `until` may each be omitted, which is what the "All dates"
    preset sends. An omitted end resolves to where this household's flow
    actually starts or stops, and the answer says which dates it settled on --
    the columns of this report are calendar months, so an open end has no last
    column and the screen must not have to guess what it is showing.
    """
    code, rates = _which(household, session, currency, in_base)
    answer = report_service.income_expense(
        session,
        household.id,
        currency=code,
        rates=rates,
        since=since,
        until=until,
        account_ids=account_id or None,
        category_ids=category_id if category_id else None,
        include_uncategorised=include_uncategorised,
    )
    currency_code = answer["currency"]
    return IncomeExpenseOut(
        currency=currency_code,
        since=answer["since"],
        until=answer["until"],
        months=answer["months"],
        income=_section_out(answer["income"], currency_code),
        expense=_section_out(answer["expense"], currency_code),
        net_by_month=answer["net_by_month"],
        net_total_minor=answer["net_total_minor"],
        net_total=format_amount(answer["net_total_minor"], currency_code, with_symbol=False),
        reimbursements=ReportReimbursementsOut(
            **{
                name: _line_out(line, currency_code)
                for name, line in answer["reimbursements"].items()
            }
        ),
        reimbursement_mixed=answer["reimbursement_mixed"],
        has_income_group=answer["has_income_group"],
        excluded=ExcludedOut(**answer["excluded"]),
        coverage=CoverageOut(**answer["coverage"]),
        **_converted_fields(session, household, answer["converting"]),
    )


def _entry_out(entry: report_service.Entry, base: str) -> ReportEntryOut:
    rate = entry.rate
    return ReportEntryOut(
        id=entry.id,
        date=entry.date,
        account_name=entry.account_name,
        payee_name=entry.payee_name,
        category_name=entry.category_name,
        memo=entry.memo,
        amount_minor=entry.amount_minor,
        amount=format_amount(entry.amount_minor, entry.currency, with_symbol=False),
        currency=entry.currency,
        converted_minor=entry.converted_minor,
        converted=(
            format_amount(entry.converted_minor, base, with_symbol=False)
            if entry.converted_minor is not None
            else None
        ),
        rate=fx.rate_text(rate.rate) if rate is not None else None,
        rate_date=rate.rate_date if rate is not None else None,
        rate_source=rate.source if rate is not None else None,
        rate_via=rate.via if rate is not None else None,
        rate_stale=rate.stale if rate is not None else None,
    )


@router.get(
    "/households/{household_id}/reports/income-expense/behind",
    response_model=ReportBehindOut,
)
def behind_a_figure(
    household: CurrentHousehold,
    session: SessionDep,
    currency: Annotated[str | None, Query(min_length=3, max_length=3)] = None,
    in_base: bool = False,
    since: Annotated[Date | None, Query()] = None,
    until: Annotated[Date | None, Query()] = None,
    account_id: Annotated[list[str] | None, Query()] = None,
    category_id: Annotated[list[str] | None, Query()] = None,
    include_uncategorised: bool = True,
    period: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    row_category_id: Annotated[str | None, Query()] = None,
    row_uncategorised: bool = False,
    row_group_id: Annotated[str | None, Query()] = None,
    direction: Annotated[
        str | None,
        Query(
            pattern=(
                "^(in|out|reimbursement_income|reimbursement_spent|reimbursement_pending"
                "|reimbursement_surplus|reimbursement_shortfall|reimbursement_unmatched"
                "|reimbursement_difference)$"
            )
        ),
    ] = None,
) -> ReportBehindOut:
    """The transactions that add up to one figure on the report.

    Every argument the report takes, plus those that say **which figure**:
    `period` for one month column, `row_category_id` / `row_uncategorised` for
    one category row, `row_group_id` for one group's subtotal, and `direction` for one line: `in` and `out` for income
    and expense, the three work-reimbursement lines by name (#372), and the
    lines worked out per repayment -- `reimbursement_surplus`,
    `reimbursement_shortfall`, `reimbursement_difference` and
    `reimbursement_unmatched` -- which answer with each repayment and the
    expenses it covered. Each
    is optional, and leaving one out widens the answer in exactly the way the
    corresponding cell is wider -- no `period` is the Total column, no
    `direction` is the Net row, which is income and expense together and
    never the reimbursement block.

    With `in_base=true` every entry keeps its own amount and currency and
    adds what it was converted to and at which rate; `total_minor` is the sum
    of the converted amounts, in the base currency, and equals the figure.

    The service composes the same `flow_rows` and the same window the report
    does, so what comes back sums to the figure that was clicked. A
    drill-through written against the register instead would show transfer
    legs and opening balances the report had excluded, and hand somebody
    checking a number the evidence that it was wrong when it was right.
    """
    code, rates = _which(household, session, currency, in_base)
    answer = report_service.behind(
        session,
        household.id,
        currency=code,
        rates=rates,
        since=since,
        until=until,
        account_ids=account_id or None,
        category_ids=category_id if category_id else None,
        include_uncategorised=include_uncategorised,
        period=period,
        row_category_id=row_category_id,
        row_uncategorised=row_uncategorised,
        row_group_id=row_group_id,
        direction=direction,
    )
    total_currency = code or rates.base
    return ReportBehindOut(
        entries=[_entry_out(entry, total_currency) for entry in answer["entries"]],
        currency=total_currency,
        converted=rates is not None,
        total_minor=answer["total_minor"],
        total=format_amount(answer["total_minor"], total_currency, with_symbol=False),
        count=answer["count"],
        capped=answer["capped"],
    )


@router.get(
    "/households/{household_id}/reports/reimbursements",
    response_model=ReimbursementsOut,
)
def reimbursements(
    household: CurrentHousehold,
    session: SessionDep,
    currency: Annotated[str, Query(min_length=3, max_length=3)],
    since: Annotated[Date | None, Query()] = None,
    until: Annotated[Date | None, Query()] = None,
) -> ReimbursementsOut:
    """What work owes, what it paid back, and what each payment covered.

    `currency` is required and singular for the reason every report here
    gives. `since` and `until` narrow on the **expense** date, so an advance
    paid before the trip and a repayment months after both stay with the
    expenses they belong to. Either may be omitted; unlike Income vs Expense
    this report has no calendar columns to close, so an open end stays open.
    """
    answer = report_service.reimbursements(
        session, household.id, currency=currency.upper(), since=since, until=until
    )
    return ReimbursementsOut.model_validate(asdict(answer))
