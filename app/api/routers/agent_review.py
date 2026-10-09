"""Reading the ledger to review it and analyse it (#134).

A filtered read of rows, the categorisation review, the reports, and the
exchange rates the household actually got -- stories 2 and 4 of #134. Every
figure stays per currency: nothing here converts or sums across two.

Same door as `agent.py`: every route hangs off `current_agent`, so the
capability floor (§1.2 -- no delete, no undo, nothing touching auth, admin,
membership or `/db`) holds here for the same structural reason it holds there.

**Endpoints join `agent.ENDPOINTS` at import**, which is the one list the
manifest publishes and the manifest tests walk. `main.py` imports this module
to mount its router, so any process serving the app has both halves.

The response models live here rather than in `app/schemas.py`, the way
`households.py` keeps its own: nothing else answers with them.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from ...errors import ValidationError
from ...models import ClearedState, LinkSource, RegisterSource, ReimbursementState, ReimbursementView
from ...schemas import IncomeExpenseOut, ManifestEndpoint, ReimbursementsOut
from ...services import insights, review
from ...services import transactions as txn_service
from ..deps import CurrentAgent
from . import agent
from . import reporting as human_reports
from .agent import _house

router = APIRouter(prefix=f"/agent/v{agent.API_VERSION}", tags=["agent"])

_BASE = f"/api/agent/v{agent.API_VERSION}"
_HOUSE = f"{_BASE}/households/{{household_id}}"


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


class RegisterRowOut(BaseModel):
    """One row, compact. For reading, not for adding up -- `/summary` does that."""

    id: str
    date: Date
    account_id: str
    currency: str
    amount_minor: int
    amount: str
    payee_id: str | None = None
    payee_name: str | None = None
    #: What the bank's statement called it, before any payee rule. Null on a
    #: row somebody typed.
    bank_text: str | None = None
    memo: str | None = None
    category_id: str | None = None
    #: "Group: Category", the name the manifest and the categorise route use.
    category_name: str | None = None
    #: A transfer leg has no category by design, and the categorise route
    #: leaves it alone.
    is_transfer: bool
    transfer_account_id: str | None = None
    split_id: str | None = None
    #: `expected` or `written_off` for a work expense; null otherwise.
    reimbursement: ReimbursementState | None = None
    #: The payment that repaid it. Repaid is this being set.
    reimbursed_by_id: str | None = None
    receipt_count: int
    cleared: ClearedState


class AgentRegisterOut(BaseModel):
    """A page of the register. Keep reading while `has_more`, from `next_offset`."""

    total: int
    offset: int
    limit: int
    has_more: bool
    #: Null on the last page.
    next_offset: int | None = None
    rows: list[RegisterRowOut]


class CategoryShareOut(BaseModel):
    category_id: str
    full_name: str
    count: int
    #: Of the payee's categorised rows, rounded down. `count` is exact.
    share_percent: int


class PayeeProfileOut(BaseModel):
    payee_id: str
    payee_name: str
    row_count: int
    categorised_count: int
    uncategorised_count: int
    categories: list[CategoryShareOut]
    usual_category_id: str | None = None
    usual_category_name: str | None = None
    usual_share_percent: int | None = None


class ReviewRowOut(BaseModel):
    transaction_id: str
    date: Date
    account_id: str
    currency: str
    amount_minor: int
    amount: str
    payee_id: str | None = None
    payee_name: str | None = None
    bank_text: str | None = None
    #: What it carries now; null when uncategorised.
    category_id: str | None = None
    category_name: str | None = None
    #: What its payee's rows usually carry; null when the payee has no clear one.
    usual_category_id: str | None = None
    usual_category_name: str | None = None
    usual_share_percent: int | None = None


class CategorisationReviewOut(BaseModel):
    """Facts about categorisation. Which rows are wrong is the reader's call."""

    since: Date | None = None
    until: Date | None = None
    account_id: str | None = None
    min_rows: int
    dominant_percent: int
    limit: int
    rows_considered: int
    excluded_transfer_legs: int
    excluded_system_payee_rows: int
    payees: list[PayeeProfileOut]
    payees_total: int
    outliers: list[ReviewRowOut]
    outliers_total: int
    uncategorised_with_usual: list[ReviewRowOut]
    uncategorised_with_usual_total: int
    uncategorised_without_usual: list[ReviewRowOut]
    uncategorised_without_usual_total: int


class ObservedRateOut(BaseModel):
    date: Date
    out_transaction_id: str
    in_transaction_id: str
    from_account_id: str
    from_currency: str
    from_amount_minor: int
    from_amount: str
    to_account_id: str
    to_currency: str
    to_amount_minor: int
    to_amount: str
    #: Units of `to_currency` per one `from_currency`, as a decimal STRING.
    rate: str
    link_source: LinkSource | None = None


class ObservedRatesOut(BaseModel):
    """What the household actually got. Evidence for a conversion, not one."""

    since: Date | None = None
    until: Date | None = None
    pair: str | None = None
    total: int
    observations: list[ObservedRateOut]


# --------------------------------------------------------------------------- #
# The register, for reading
# --------------------------------------------------------------------------- #


@router.get("/households/{household_id}/register", response_model=AgentRegisterOut)
def register(
    household_id: str,
    agent: CurrentAgent,
    request: Request,
    account_id: Annotated[list[str] | None, Query()] = None,
    since: Date | None = None,
    until: Date | None = None,
    search: str | None = None,
    cleared: ClearedState | None = None,
    uncategorised: bool = False,
    amount: Annotated[str | None, Query(max_length=32)] = None,
    source: RegisterSource | None = None,
    reimbursement: ReimbursementView | None = None,
    category_id: str | None = None,
    payee_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> AgentRegisterOut:
    """The register's own filter, through the register's own code.

    `transactions.filtered` narrows the rows, with the same argument names the
    person's register takes, so the same parameters select the same ids on
    both. The order is fixed -- newest first, ties broken to the id -- so
    `offset` paging neither repeats nor skips while nothing is written.
    """
    house = _house(agent, household_id)
    session = agent.session
    typed_amount = (amount or "").strip()
    stmt = txn_service.filtered(
        house.id,
        account_ids=account_id,
        since=since,
        until=until,
        search=search,
        cleared=cleared,
        uncategorised=uncategorised,
        amount=(
            txn_service.amount_lookup(
                txn_service.account_currencies(session, house.id), typed_amount
            )
            if typed_amount
            else None
        ),
        source=source,
        reimbursement=reimbursement,
        category_id=category_id,
        payee_id=payee_id,
    )
    page = review.register_read(session, house.id, stmt, limit=limit, offset=offset)
    request.state.agent_rows = len(page.rows)
    has_more = offset + len(page.rows) < page.total
    return AgentRegisterOut(
        total=page.total,
        offset=offset,
        limit=limit,
        has_more=has_more,
        next_offset=offset + len(page.rows) if has_more else None,
        rows=[
            RegisterRowOut(
                id=row.id,
                date=row.date,
                account_id=row.account_id,
                currency=row.currency,
                amount_minor=row.amount_minor,
                amount=insights.formatted(row.amount_minor, row.currency),
                payee_id=row.payee_id,
                payee_name=row.payee_name,
                bank_text=row.bank_text,
                memo=row.memo,
                category_id=row.category_id,
                category_name=row.category_name,
                is_transfer=row.is_transfer,
                transfer_account_id=row.transfer_account_id,
                split_id=row.split_id,
                reimbursement=row.reimbursement,
                reimbursed_by_id=row.reimbursed_by_id,
                receipt_count=row.receipt_count,
                cleared=row.cleared,
            )
            for row in page.rows
        ],
    )


# --------------------------------------------------------------------------- #
# Categorisation review
# --------------------------------------------------------------------------- #


def _review_row(row: review.ReviewRow) -> ReviewRowOut:
    return ReviewRowOut(
        transaction_id=row.transaction_id,
        date=row.date,
        account_id=row.account_id,
        currency=row.currency,
        amount_minor=row.amount_minor,
        amount=insights.formatted(row.amount_minor, row.currency),
        payee_id=row.payee_id,
        payee_name=row.payee_name,
        bank_text=row.bank_text,
        category_id=row.category_id,
        category_name=row.category_name,
        usual_category_id=row.usual_category_id,
        usual_category_name=row.usual_category_name,
        usual_share_percent=row.usual_share_percent,
    )


@router.get(
    "/households/{household_id}/categorisation/review", response_model=CategorisationReviewOut
)
def categorisation_review(
    household_id: str,
    agent: CurrentAgent,
    request: Request,
    account_id: str | None = None,
    since: Date | None = None,
    until: Date | None = None,
    min_rows: int = Query(default=3, ge=1, le=1000),
    dominant_percent: int = Query(default=80, ge=51, le=100),
    limit: int = Query(default=100, ge=1, le=1000),
) -> CategorisationReviewOut:
    """Per payee, the categories its rows carry; and the rows that disagree.

    `dominant_percent` is at least 51 so a payee can have at most one usual
    category -- two categories cannot both hold a majority.
    """
    house = _house(agent, household_id)
    answer = review.categorisation_review(
        agent.session,
        house.id,
        account_id=account_id,
        since=since,
        until=until,
        min_rows=min_rows,
        dominant_percent=dominant_percent,
        limit=limit,
    )
    request.state.agent_rows = answer.rows_considered
    return CategorisationReviewOut(
        since=since,
        until=until,
        account_id=account_id,
        min_rows=min_rows,
        dominant_percent=dominant_percent,
        limit=limit,
        rows_considered=answer.rows_considered,
        excluded_transfer_legs=answer.excluded_transfer_legs,
        excluded_system_payee_rows=answer.excluded_system_payee_rows,
        payees=[
            PayeeProfileOut(
                payee_id=p.payee_id,
                payee_name=p.payee_name,
                row_count=p.row_count,
                categorised_count=p.categorised_count,
                uncategorised_count=p.uncategorised_count,
                categories=[
                    CategoryShareOut(
                        category_id=c.category_id,
                        full_name=c.full_name,
                        count=c.count,
                        share_percent=c.share_percent,
                    )
                    for c in p.categories
                ],
                usual_category_id=p.usual_category_id,
                usual_category_name=p.usual_category_name,
                usual_share_percent=p.usual_share_percent,
            )
            for p in answer.payees
        ],
        payees_total=answer.payees_total,
        outliers=[_review_row(r) for r in answer.outliers],
        outliers_total=answer.outliers_total,
        uncategorised_with_usual=[_review_row(r) for r in answer.uncategorised_with_usual],
        uncategorised_with_usual_total=answer.uncategorised_with_usual_total,
        uncategorised_without_usual=[
            _review_row(r) for r in answer.uncategorised_without_usual
        ],
        uncategorised_without_usual_total=answer.uncategorised_without_usual_total,
    )


# --------------------------------------------------------------------------- #
# Reports: the person's own, one currency at a time
# --------------------------------------------------------------------------- #


@router.get(
    "/households/{household_id}/reports/income-expense", response_model=IncomeExpenseOut
)
def income_expense(
    household_id: str,
    agent: CurrentAgent,
    request: Request,
    currency: Annotated[str, Query(min_length=3, max_length=3)],
    since: Date | None = None,
    until: Date | None = None,
    account_id: Annotated[list[str] | None, Query()] = None,
    category_id: Annotated[list[str] | None, Query()] = None,
    include_uncategorised: bool = True,
) -> IncomeExpenseOut:
    """The Income vs Expense report, answered by the person's route itself.

    Called rather than re-assembled, so the two cannot disagree about a figure
    or about what `excluded` counts. `currency` is required and singular there
    for the reason every report gives, and so it is here.
    """
    house = _house(agent, household_id)
    answer = human_reports.income_expense(
        household=house,
        session=agent.session,
        currency=currency,
        since=since,
        until=until,
        account_id=account_id,
        category_id=category_id,
        include_uncategorised=include_uncategorised,
    )
    request.state.agent_rows = answer.income.count + answer.expense.count
    return answer


@router.get(
    "/households/{household_id}/reports/reimbursements", response_model=ReimbursementsOut
)
def reimbursements(
    household_id: str,
    agent: CurrentAgent,
    request: Request,
    currency: Annotated[str, Query(min_length=3, max_length=3)],
    since: Date | None = None,
    until: Date | None = None,
) -> ReimbursementsOut:
    """The Reimbursements report, answered by the person's route itself."""
    house = _house(agent, household_id)
    answer = human_reports.reimbursements(
        household=house, session=agent.session, currency=currency, since=since, until=until
    )
    request.state.agent_rows = (
        answer.outstanding_count + answer.recovered_count + answer.written_off_count
    )
    return answer


# --------------------------------------------------------------------------- #
# Observed exchange rates
# --------------------------------------------------------------------------- #


def _pair(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    cleaned = value.strip().upper().replace("/", "").replace("-", "")
    if len(cleaned) != 6 or not cleaned.isalpha() or cleaned[:3] == cleaned[3:]:
        raise ValidationError(f"{value!r} is not a currency pair; send it as EUR/GBP")
    return cleaned[:3], cleaned[3:]


@router.get("/households/{household_id}/fx/observed", response_model=ObservedRatesOut)
def fx_observed(
    household_id: str,
    agent: CurrentAgent,
    request: Request,
    since: Date | None = None,
    until: Date | None = None,
    pair: Annotated[str | None, Query(max_length=16)] = None,
    limit: int = Query(default=500, ge=1, le=1000),
) -> ObservedRatesOut:
    """The rates implied by the household's own linked cross-currency transfers."""
    house = _house(agent, household_id)
    currencies = _pair(pair)
    found = review.observed_rates(
        agent.session, house.id, since=since, until=until, currencies=currencies
    )
    request.state.agent_rows = len(found)
    return ObservedRatesOut(
        since=since,
        until=until,
        pair=f"{currencies[0]}/{currencies[1]}" if currencies else None,
        total=len(found),
        observations=[
            ObservedRateOut(
                date=o.date,
                out_transaction_id=o.out_transaction_id,
                in_transaction_id=o.in_transaction_id,
                from_account_id=o.from_account_id,
                from_currency=o.from_currency,
                from_amount_minor=o.from_amount_minor,
                from_amount=insights.formatted(o.from_amount_minor, o.from_currency),
                to_account_id=o.to_account_id,
                to_currency=o.to_currency,
                to_amount_minor=o.to_amount_minor,
                to_amount=insights.formatted(o.to_amount_minor, o.to_currency),
                rate=o.rate,
                link_source=o.link_source,
            )
            for o in found[:limit]
        ],
    )


# --------------------------------------------------------------------------- #
# What the manifest says about them
# --------------------------------------------------------------------------- #

ENDPOINTS: list[ManifestEndpoint] = [
    ManifestEndpoint(
        method="GET",
        path=f"{_HOUSE}/register",
        says=(
            "Read rows to review them, newest first, paged: limit (default 200, max "
            "1000) and offset; keep going from next_offset while has_more. Filters, the "
            "person's register's own: account_id (repeatable), since, until, search, "
            "cleared, uncategorised, amount (unsigned decimal string, every currency), "
            "source (transfer|split|imported|manual), reimbursement (work|owed|paid|"
            "off), category_id, payee_id. Each row carries its own currency. "
            "This is for reading, not arithmetic: for totals ask /summary, which adds "
            "exactly and per currency."
        ),
        returns="{total, offset, limit, has_more, next_offset, rows[]}",
        scope="read",
    ),
    ManifestEndpoint(
        method="GET",
        path=f"{_HOUSE}/categorisation/review",
        says=(
            "Facts for spotting wrong categories, not verdicts. payees[]: each payee with "
            "at least min_rows rows (default 3) and the categories its rows carry, with "
            "counts. A payee has a usual category when one holds at least dominant_percent "
            "(default 80) of its categorised rows. outliers[]: rows carrying a different "
            "category from their payee's usual one. uncategorised_with_usual[]: rows with "
            "no category whose payee has a usual one -- the likely answer, for you to "
            "confirm. uncategorised_without_usual[]: the rest. Transfer legs and "
            "app-made payees (opening balances) are left out and counted. Filters: "
            "account_id, since, until -- the usual category is read from the same window. "
            "Each list is capped at limit and its *_total says how many there are. To "
            "change a category, send the ids to PATCH .../transactions."
        ),
        returns=(
            "{since, until, account_id, min_rows, dominant_percent, limit, rows_considered, "
            "excluded_transfer_legs, excluded_system_payee_rows, payees[], payees_total, "
            "outliers[], outliers_total, uncategorised_with_usual[], "
            "uncategorised_with_usual_total, uncategorised_without_usual[], "
            "uncategorised_without_usual_total}"
        ),
        scope="read",
    ),
    ManifestEndpoint(
        method="GET",
        path=f"{_HOUSE}/reports/income-expense",
        says=(
            "The Income vs Expense report for ONE currency (currency is required): income "
            "and expense by category and month, net, in that currency only. Ask once per "
            "currency the manifest's accounts hold and never add the answers together. "
            "Transfers, opening balances and work expenses with their repayments are left "
            "out and counted in `excluded`. Filters: since, until (an omitted end is where "
            "the flow starts or stops, and the answer says which), account_id and "
            "category_id (both repeatable), include_uncategorised."
        ),
        returns=(
            "{currency, since, until, months[], income, expense, net_by_month, "
            "net_total_minor, net_total, excluded, coverage}"
        ),
        scope="read",
    ),
    ManifestEndpoint(
        method="GET",
        path=f"{_HOUSE}/reports/reimbursements",
        says=(
            "The Reimbursements report for ONE currency (currency is required): what work "
            "still owes, what it repaid and what was written off, as positive minor units, "
            "with the outstanding rows and each repayment's claim. since/until narrow on "
            "the expense date. available_currencies lists the others to ask about."
        ),
        returns=(
            "{currency, since, until, available_currencies[], outstanding, "
            "outstanding_count, oldest_outstanding, recovered, recovered_count, "
            "written_off, written_off_count, unmatched, outstanding_rows[], claims[], "
            "months[]}"
        ),
        scope="read",
    ),
    ManifestEndpoint(
        method="GET",
        path=f"{_HOUSE}/fx/observed",
        says=(
            "The exchange rates this household actually got: one entry per linked "
            "transfer between accounts in two currencies, with both amounts and the rate "
            "(units of to_currency per one from_currency) as a decimal STRING -- parse it "
            "as a decimal, never a float. Filters: since, until, pair (EUR/GBP, matched in "
            "either direction). This converts nothing: it is evidence for a conversion you "
            "make and show. An empty list means there are no such transfers."
        ),
        returns="{since, until, pair, total, observations[]}",
        scope="read",
    ),
]


agent.ENDPOINTS.extend(ENDPOINTS)
