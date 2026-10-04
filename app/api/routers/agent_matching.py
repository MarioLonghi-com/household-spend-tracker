"""Finding the row a piece of evidence is about (#134).

A receipt, a line on a corporate expenses portal, a row in somebody's
spreadsheet: each is a claim about a purchase, and the agent's job is to
find the ledger row it describes. This module answers that question in one
call rather than a pull of the register -- stories 1 and 3 of #134.

Two routes, one matcher (`services/matching.py`):

* ``POST …/transactions/match`` takes up to fifty descriptions of evidence and
  needs no receipt to have been stored first -- a portal line never is one.
* ``GET …/receipts/{id}/candidates`` is the same search on a stored receipt's
  behalf. It moved here from `agent.py` when it learned to take the date
  printed on the receipt and to match a foreign-currency one on merchant.

Same door as `agent.py`: every route hangs off `current_agent`, so the
capability floor (§1.2 -- no delete, no undo, nothing touching auth, admin,
membership or `/db`) holds here for the same structural reason it holds there.
Both routes read and neither writes, so both take `CurrentAgent`, not
`AgentWriter`: a read key can match.

**Endpoints join `agent.ENDPOINTS` at import**, which is the one list the
manifest publishes and the manifest tests walk. `main.py` imports this module
to mount its router, so any process serving the app has both halves.
"""

from __future__ import annotations

from datetime import date as Date
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator
from sqlalchemy import select

from ...errors import NotFound, ValidationError
from ...models import Account, Receipt
from ...schemas import ManifestEndpoint
from ...services import insights, matching
from ...services.importing import MATCH_WINDOW_DAYS
from ...services.transactions import is_transfer_leg
from ..deps import CurrentAgent
from . import agent

router = APIRouter(prefix=f"/agent/v{agent.API_VERSION}", tags=["agent"])


# --------------------------------------------------------------------------- #
# Shapes. Here rather than in `schemas.py`, as `households.py` does, so this
# module owns everything it answers with.
# --------------------------------------------------------------------------- #


class MatchQuery(BaseModel):
    """One piece of evidence: what was paid, when, and to whom."""

    model_config = ConfigDict(extra="forbid")

    #: The caller's own label, echoed back: "receipt 3", a portal line id.
    ref: str | None = Field(default=None, max_length=200)
    #: Exactly one of these two, and both STRICT, as on an import row: a JSON
    #: float is refused, never rounded. The sign is ignored -- `direction` says
    #: which way the money went.
    amount_minor: StrictInt | None = None
    amount: StrictStr | None = Field(default=None, max_length=40)
    #: The evidence's currency. When an account is in another one, its rows
    #: are matched on date and merchant, never on a converted figure.
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    date: Date
    window_days: int = Field(default=MATCH_WINDOW_DAYS, ge=0, le=matching.MAX_WINDOW_DAYS)
    #: Merchant or description words.
    text: str | None = Field(default=None, max_length=300)
    account_id: str | None = None
    direction: Literal["out", "in", "any"] = "out"

    @model_validator(mode="after")
    def exactly_one_amount(self) -> MatchQuery:
        given = [v for v in (self.amount_minor, self.amount) if v is not None]
        if len(given) != 1:
            raise ValueError(
                "give exactly one of amount_minor (integer minor units, e.g. 1250) "
                "or amount (a decimal STRING, e.g. \"12.50\"). A JSON float is not "
                "accepted for money."
            )
        if self.currency is not None and matching.currency_or_none(self.currency) is None:
            raise ValueError("currency is a three-letter ISO code, e.g. \"EUR\"")
        return self


class MatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[MatchQuery] = Field(min_length=1, max_length=matching.MAX_QUERIES)
    #: Candidates per query.
    limit: int = Field(default=matching.DEFAULT_LIMIT, ge=1, le=matching.MAX_LIMIT)


class MatchCandidateOut(BaseModel):
    """One row the evidence could describe, with enough to decide without a second read."""

    transaction_id: str
    date: Date
    account_id: str
    account: str
    #: The ACCOUNT's currency, which is the currency of `amount_minor`.
    currency: str
    amount_minor: int
    amount: str
    payee: str | None = None
    memo: str | None = None
    #: The bank's own description, where the row came off a statement.
    bank_text: str | None = None
    #: "Group: Category", or None.
    category: str | None = None
    is_transfer: bool
    #: None, "expected", "settled" or "written_off".
    reimbursement: str | None = None
    #: "uncleared", "cleared" or "reconciled". A reconciled row is still offered.
    cleared: str
    #: How many receipts the row already carries.
    receipts: int
    days_apart: int
    amount_matched: bool
    matched_words: list[str] = []
    reason: str


class MatchResultOut(BaseModel):
    ref: str | None = None
    #: What was searched for, echoed back: the magnitude in minor units.
    amount_minor: int
    currency: str | None = None
    since: Date
    until: Date
    #: Said when the search could not do what was asked, e.g. no account in
    #: the evidence's currency.
    note: str | None = None
    candidates: list[MatchCandidateOut] = []


class MatchOut(BaseModel):
    #: One per query, in the order sent.
    results: list[MatchResultOut]


class ReceiptCandidatesOut(BaseModel):
    receipt_id: str
    #: What was matched on, echoed back so an agent that got no answer can see
    #: why rather than guessing.
    total_minor: int | None = None
    currency: str | None = None
    matched_on_date: Date | None = None
    #: Where that date came from: "given", "extracted", "captured" or "uploaded".
    date_from: str | None = None
    merchant: str | None = None
    candidates: list[MatchCandidateOut] = []


# --------------------------------------------------------------------------- #
# The manifest
# --------------------------------------------------------------------------- #

_CANDIDATE = (
    "Each candidate: transaction_id, date, account_id, account, currency, amount_minor, "
    "amount, payee, memo, bank_text, category, is_transfer, reimbursement, cleared, "
    "receipts, days_apart, amount_matched, matched_words, reason."
)

ENDPOINTS: list[ManifestEndpoint] = [
    ManifestEndpoint(
        method="POST",
        path=f"{agent._BASE}/households/{{household_id}}/transactions/match",
        says=(
            "Find the rows that receipts or expense-portal lines describe, without storing "
            f"anything. Send {{queries: [...], limit}}: up to {matching.MAX_QUERIES} queries, "
            f"at most {matching.MAX_LIMIT} candidates each (default {matching.DEFAULT_LIMIT}). "
            "A query is {ref, amount_minor | amount, currency, date, window_days, text, "
            "account_id, direction}: amount_minor an integer or amount a decimal STRING (never a "
            "JSON float; a string needs currency or account_id), sign ignored; direction "
            f"out (default) | in | any; window_days default {MATCH_WINDOW_DAYS}, max "
            f"{matching.MAX_WINDOW_DAYS}; text is the merchant. The amount is compared exactly, "
            "only in accounts of the query's currency; an account in another currency is "
            "matched on date and merchant only, never converted. Ranked: exact amount, then "
            "shared merchant words, then nearer date. Reconciled rows are included. Writes "
            f"nothing. Results come back in order, each {{ref, amount_minor, currency, since, "
            f"until, note, candidates[]}}. {_CANDIDATE}"
        ),
        returns="{results[]}",
        scope="read",
    ),
    ManifestEndpoint(
        method="GET",
        path=f"{agent._BASE}/receipts/{{receipt_id}}/candidates",
        says=(
            "Which transactions could this stored receipt belong to? At most five, ranked, "
            "with a reason each. Pass date= (the date printed on it -- a scan has no camera "
            "date), currency= and total_minor=; otherwise extracted.date, extracted.currency "
            "and extracted.total_minor are used as things to search for, then the camera "
            "date, then the upload date (date_from says which). In its own currency the "
            "amount must match exactly and extracted.merchant only breaks ties; in another "
            f"currency rows are matched on date and merchant. {_CANDIDATE}"
        ),
        returns=(
            "{receipt_id, total_minor, currency, matched_on_date, date_from, merchant, "
            "candidates[]}"
        ),
        scope="read",
    ),
]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def _accounts(agent_ctx: CurrentAgent) -> dict[str, Account]:
    return {
        row.id: row
        for row in agent_ctx.session.execute(
            select(Account).where(Account.household_id == agent_ctx.household.id)
        ).scalars()
    }


def _candidates(session, found: list[matching.Match]) -> list[MatchCandidateOut]:
    counts = matching.receipt_counts(session, [m.transaction.id for m in found])
    out = []
    for m in found:
        row = m.transaction
        currency = row.account.currency
        out.append(
            MatchCandidateOut(
                transaction_id=row.id,
                date=row.date,
                account_id=row.account_id,
                account=row.account.name,
                currency=currency,
                amount_minor=row.amount,
                amount=insights.formatted(row.amount, currency),
                payee=row.payee.name if row.payee else None,
                memo=row.memo,
                bank_text=row.import_payee_original,
                category=row.category.full_name if row.category else None,
                is_transfer=is_transfer_leg(row),
                reimbursement=matching.reimbursement_state(row),
                cleared=row.cleared.value,
                receipts=counts.get(row.id, 0),
                days_apart=m.days_apart,
                amount_matched=m.amount_matched,
                matched_words=list(m.words),
                reason=m.reason,
            )
        )
    return out


def _label(query: MatchQuery, index: int) -> str:
    return f'query {index} ("{query.ref}")' if query.ref else f"query {index}"


@router.post("/households/{household_id}/transactions/match", response_model=MatchOut)
def match(
    household_id: str, body: MatchRequest, agent_ctx: CurrentAgent, request: Request
) -> MatchOut:
    """Up to fifty pieces of evidence, each with its ranked candidate rows.

    Everything is checked before anything is searched, so a bad query is a
    refusal naming it rather than forty-nine answers and a hole.
    """
    house = agent._house(agent_ctx, household_id)
    session = agent_ctx.session
    accounts = _accounts(agent_ctx)

    # A stranger's account id is the same 404 an invented one gets.
    if any(q.account_id and q.account_id not in accounts for q in body.queries):
        raise NotFound("no such account")

    searches: list[matching.Evidence] = []
    for index, query in enumerate(body.queries):
        currency = matching.currency_or_none(query.currency)
        if query.amount_minor is not None:
            minor = abs(query.amount_minor)
        else:
            per = currency or (accounts[query.account_id].currency if query.account_id else None)
            if per is None:
                raise ValidationError(
                    f'{_label(query, index)}: amount "{query.amount}" is a decimal string, and '
                    "with neither currency nor account_id there is no way to know how many "
                    "decimal places it has. Send currency, account_id, or amount_minor."
                )
            try:
                minor = abs(matching.decimal_to_minor(query.amount, per))
            except ValidationError as exc:
                raise ValidationError(f"{_label(query, index)}: {exc}") from None
        searches.append(
            matching.Evidence(
                date=query.date,
                amount_minor=minor,
                currency=currency,
                window_days=query.window_days,
                text=query.text,
                account_id=query.account_id,
                direction=query.direction,
            )
        )

    results: list[MatchResultOut] = []
    rows = 0
    for query, evidence in zip(body.queries, searches, strict=True):
        found, note = matching.find(
            session, house.id, evidence, accounts=accounts, limit=body.limit
        )
        rows += len(found)
        window = evidence.window_days
        results.append(
            MatchResultOut(
                ref=query.ref,
                amount_minor=evidence.amount_minor,
                currency=evidence.currency,
                since=Date.fromordinal(evidence.date.toordinal() - window),
                until=Date.fromordinal(evidence.date.toordinal() + window),
                note=note,
                candidates=_candidates(session, found),
            )
        )

    request.state.agent_rows = rows
    return MatchOut(results=results)


@router.get("/receipts/{receipt_id}/candidates", response_model=ReceiptCandidatesOut)
def receipt_candidates(
    receipt_id: str,
    agent_ctx: CurrentAgent,
    request: Request,
    total_minor: int | None = None,
    date: Date | None = None,
    currency: str | None = None,
) -> ReceiptCandidatesOut:
    """Which transactions could this receipt belong to? At most five, ranked.

    Without this an agent filing one receipt pulls the register and does the
    matching in its context window. With it, one call and about eighty tokens.

    `total_minor`, `date` and `currency` may be given explicitly; otherwise
    each is taken from the agent's own `extracted` claim **only as something
    to search for** -- the matching itself is against the ledger's figures,
    and a claim that is wrong simply finds nothing. `extracted.merchant`
    breaks ties between rows of the same amount, and is the only thing a row
    in another currency can be matched on.
    """
    receipt = agent_ctx.load(Receipt, receipt_id)
    if currency is not None and matching.currency_or_none(currency) is None:
        raise ValidationError("currency is a three-letter ISO code, e.g. \"EUR\"")
    total = total_minor if total_minor is not None else _claimed_total(receipt)
    search = matching.receipt_search(
        agent_ctx.session, receipt,
        total_minor=total, given_date=date, given_currency=currency,
    )

    found: list[matching.Match] = []
    if search.evidence is not None:
        found, _note = matching.find(
            agent_ctx.session,
            receipt.household_id,
            search.evidence,
            accounts=_accounts(agent_ctx),
            text_admits=False,
            exclude=search.exclude,
            limit=matching.DEFAULT_LIMIT,
        )
    request.state.agent_rows = len(found)

    return ReceiptCandidatesOut(
        receipt_id=receipt.id,
        total_minor=total,
        currency=search.currency,
        matched_on_date=search.evidence.date if search.evidence else None,
        date_from=search.date_from,
        merchant=search.merchant,
        candidates=_candidates(agent_ctx.session, found),
    )


def _claimed_total(receipt) -> int | None:
    """The total the agent said it read, if it said one and it is an integer.

    Deliberately narrow: `extracted` is free-form and attacker-shaped. A float
    or a string is ignored rather than coerced -- money does not arrive here as
    either.
    """
    claim = receipt.extracted if isinstance(receipt.extracted, dict) else {}
    total = claim.get("total_minor")
    return total if isinstance(total, int) and not isinstance(total, bool) else None


agent.ENDPOINTS.extend(ENDPOINTS)
