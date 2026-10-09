"""Reports: read-only arithmetic over the ledger, one currency at a time.

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

from ...money import format_amount
from ...schemas import (
    CoverageOut,
    ExcludedOut,
    IncomeExpenseOut,
    ReimbursementsOut,
    ReportBehindOut,
    ReportCurrenciesOut,
    ReportEntryOut,
    ReportRowOut,
    ReportSectionOut,
)
from ...services import reporting as report_service
from ..deps import CurrentHousehold, SessionDep

router = APIRouter(tags=["reports"])


def _row_out(row: report_service.Row, currency: str) -> ReportRowOut:
    return ReportRowOut(
        key=row.key,
        name=row.name,
        group_name=row.group_name,
        by_month=row.by_month,
        total_minor=row.total_minor,
        total=format_amount(row.total_minor, currency, with_symbol=False),
        average_minor=row.average_minor,
        average=format_amount(row.average_minor, currency, with_symbol=False),
        count=row.count,
    )


def _section_out(section: report_service.Section, currency: str) -> ReportSectionOut:
    return ReportSectionOut(
        rows=[_row_out(row, currency) for row in section.rows],
        by_month=section.by_month,
        total_minor=section.total_minor,
        total=format_amount(section.total_minor, currency, with_symbol=False),
        average_minor=section.average_minor,
        average=format_amount(section.average_minor, currency, with_symbol=False),
        count=section.count,
    )


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
    ledger is in rather than on whichever one sorts first alphabetically.
    """
    return ReportCurrenciesOut(
        currencies=report_service.currencies_in_use(session, household.id)
    )


@router.get(
    "/households/{household_id}/reports/income-expense", response_model=IncomeExpenseOut
)
def income_expense(
    household: CurrentHousehold,
    session: SessionDep,
    currency: Annotated[str, Query(min_length=3, max_length=3)],
    since: Annotated[Date | None, Query()] = None,
    until: Annotated[Date | None, Query()] = None,
    account_id: Annotated[list[str] | None, Query()] = None,
    category_id: Annotated[list[str] | None, Query()] = None,
    include_uncategorised: bool = True,
) -> IncomeExpenseOut:
    """Income against expense across the window, broken down by category.

    `currency` is **required and singular**, which is the whole design. The
    ledger never converts, so a report spanning two currencies would be adding
    figures that have no sum; making the caller name one means the question is
    answerable before it is asked. The client's currency toggle is the shape
    this takes on screen.

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
    answer = report_service.income_expense(
        session,
        household.id,
        currency=currency.upper(),
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
        excluded=ExcludedOut(**answer["excluded"]),
        coverage=CoverageOut(**answer["coverage"]),
    )


@router.get(
    "/households/{household_id}/reports/income-expense/behind",
    response_model=ReportBehindOut,
)
def behind_a_figure(
    household: CurrentHousehold,
    session: SessionDep,
    currency: Annotated[str, Query(min_length=3, max_length=3)],
    since: Annotated[Date | None, Query()] = None,
    until: Annotated[Date | None, Query()] = None,
    account_id: Annotated[list[str] | None, Query()] = None,
    category_id: Annotated[list[str] | None, Query()] = None,
    include_uncategorised: bool = True,
    period: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
    row_category_id: Annotated[str | None, Query()] = None,
    row_uncategorised: bool = False,
    direction: Annotated[str | None, Query(pattern="^(in|out)$")] = None,
) -> ReportBehindOut:
    """The transactions that add up to one figure on the report.

    Every argument the report takes, plus three that say **which figure**:
    `period` for one month column, `row_category_id` / `row_uncategorised` for
    one category row, and `direction` for one band. Each is optional, and
    leaving one out widens the answer in exactly the way the corresponding
    cell is wider -- no `period` is the Total column, no `direction` is the Net
    row, where both sides are the honest answer because a net figure is made
    of both.

    The service composes the same `flow_rows` and the same window the report
    does, so what comes back sums to the figure that was clicked. A
    drill-through written against the register instead would show transfer
    legs and opening balances the report had excluded, and hand somebody
    checking a number the evidence that it was wrong when it was right.
    """
    answer = report_service.behind(
        session,
        household.id,
        currency=currency.upper(),
        since=since,
        until=until,
        account_ids=account_id or None,
        category_ids=category_id if category_id else None,
        include_uncategorised=include_uncategorised,
        period=period,
        row_category_id=row_category_id,
        row_uncategorised=row_uncategorised,
        direction=direction,
    )
    code = currency.upper()
    return ReportBehindOut(
        entries=[
            ReportEntryOut(
                id=entry.id,
                date=entry.date,
                account_name=entry.account_name,
                payee_name=entry.payee_name,
                category_name=entry.category_name,
                memo=entry.memo,
                amount_minor=entry.amount_minor,
                amount=format_amount(entry.amount_minor, code, with_symbol=False),
            )
            for entry in answer["entries"]
        ],
        total_minor=answer["total_minor"],
        total=format_amount(answer["total_minor"], code, with_symbol=False),
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
