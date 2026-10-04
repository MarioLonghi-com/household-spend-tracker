"""Links an agent may make between rows: work expenses and transfers (#134).

Flagging a row as a work expense (read off a corporate portal) and linking
the two legs of a transfer the matcher missed -- stories 3 and 5 of #134.
Unlinking and undo stay a person's, as everywhere on the agent surface.

Same door as `agent.py`: every route hangs off `current_agent`, so the
capability floor (§1.2 -- no delete, no undo, nothing touching auth, admin,
membership or `/db`) holds here for the same structural reason it holds there.

**Endpoints join `agent.ENDPOINTS` at import**, which is the one list the
manifest publishes and the manifest tests walk. `main.py` imports this module
to mount its router, so any process serving the app has both halves.

**Every write here is the categorise route's shape**: one batch, so one undo
in History puts the whole request back; a row the rules refuse is named with
the service's own sentence and the rest still apply; an id that is not this
household's is listed in `not_found`, never told apart from an invented one.

**What an agent may not do here, and why** (§1.2, "no unlink"):

- *Take a payment off a work expense.* `settled_by: null` is refused, and so
  is clearing the flag on a row that has been paid back, because clearing the
  flag clears the link with it (`set_reimbursement`). Both undo a link a
  person or a program made; a person does that, or undoes the batch.
- *Unlink or confirm a transfer.* Neither route exists. A pair an agent links
  is recorded as `LinkSource.agent`, which the matcher does not count as
  history and the Transfers screen lists for a person to keep or unlink.
- *Link a pair a person marked "not a transfer", or reject a linked pair.*
  The first would overrule a person's decision; the second would record a
  rejection of a link that is still there, which only an unlink can mean.
"""

from __future__ import annotations

from collections import Counter
from datetime import date as Date

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ...errors import Conflict, NotFound, ValidationError
from ...models import (
    Account,
    BatchKind,
    LinkSource,
    ReimbursementState,
    Transaction,
    TransferRejection,
)
from ...schemas import ManifestEndpoint
from ...services import insights
from ...services import transactions as txn_service
from ...services import transfers as transfer_service
from ..deps import AgentWriter, CurrentAgent
from . import agent as agent_api

router = APIRouter(prefix=f"/agent/v{agent_api.API_VERSION}", tags=["agent"])

_BASE = f"/api/agent/v{agent_api.API_VERSION}"

#: The most rows one reimbursement request may touch: the categorise route's
#: ceiling, for the same reason -- one undo should stay one readable act.
MAX_ASSIGNMENTS = 1000
#: The most pairs one link or reject request may carry.
MAX_PAIRS = 200


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #


class ReimbursementAssignment(BaseModel):
    """One row's work-expense flag, as an agent read it off a portal."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1, max_length=64)
    #: `expected` -- somebody else is to pay it back; `written_off` -- they
    #: will not; null -- not a work expense. Required: there is no default
    #: an agent could mean by leaving it out.
    state: ReimbursementState | None
    #: The payment that repaid it: money in, this household, not a transfer.
    #: Optional. Linking an unflagged row flags it `expected`. Sending null
    #: (taking a payment off) is refused: that is a person's.
    settled_by: str | None = Field(default=None, max_length=64)


class ReimbursementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignments: list[ReimbursementAssignment] = Field(min_length=1, max_length=MAX_ASSIGNMENTS)


class RowRefusal(BaseModel):
    transaction_id: str
    #: The rule's own sentence, the one a person would be shown.
    reason: str


class ReimbursementResult(BaseModel):
    batch_id: str
    changed: int
    #: Already as asked, so nothing was written for them.
    unchanged: int
    #: Ids that are not rows in this household.
    not_found: list[str] = []
    #: Rows the rules refused, each with why. The rest still applied.
    refused: list[RowRefusal] = []


class AgentTransferLeg(BaseModel):
    id: str
    date: Date
    account_id: str
    account: str
    currency: str
    amount_minor: int
    amount: str
    #: What the bank said, else the payee.
    description: str | None


class AgentTransferPair(BaseModel):
    out_leg: AgentTransferLeg
    in_leg: AgentTransferLeg
    #: `strong` -- the evidence names the other side or the accounts have a
    #: vouched history; `suggested` -- amounts and dates only, or a reason
    #: to doubt it is in `why`.
    strength: str
    why: str


class AgentAwaitingLeg(BaseModel):
    leg: AgentTransferLeg
    why: str


class AgentLinkedPair(BaseModel):
    out_leg: AgentTransferLeg
    in_leg: AgentTransferLeg
    link_source: str | None
    why: str


class TransferFindings(BaseModel):
    #: How many of each there are in all, before `limit`.
    pair_count: int
    awaiting_count: int
    unproven_count: int
    #: True when any list below was cut at `limit`.
    truncated: bool
    pairs: list[AgentTransferPair]
    awaiting: list[AgentAwaitingLeg]
    unproven: list[AgentLinkedPair]


class PairIds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    out_id: str = Field(min_length=1, max_length=64)
    in_id: str = Field(min_length=1, max_length=64)


class PairsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairs: list[PairIds] = Field(min_length=1, max_length=MAX_PAIRS)


class PairRefusal(BaseModel):
    out_id: str
    in_id: str
    reason: str


class LinkedOut(BaseModel):
    #: The leg money left, and the one it arrived in -- whichever order sent.
    out_id: str
    in_id: str
    #: Destination units per one source unit, as a decimal string, on a
    #: cross-currency pair; null when both legs share a currency.
    fx_rate: str | None


class TransferLinkResult(BaseModel):
    batch_id: str
    linked: list[LinkedOut] = []
    not_found: list[str] = []
    refused: list[PairRefusal] = []


class TransferRejectResult(BaseModel):
    batch_id: str
    rejected: int
    #: Already marked not a transfer.
    unchanged: int
    not_found: list[str] = []
    refused: list[PairRefusal] = []


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

ENDPOINTS: list[ManifestEndpoint] = [
    ManifestEndpoint(
        method="PATCH",
        path=f"{_BASE}/households/{{household_id}}/transactions/reimbursement",
        says=(
            "Flag rows as work expenses someone else pays back, e.g. from a corporate "
            "expenses portal. Send assignments: [{transaction_id, state}] with state "
            "'expected', 'written_off' or null (not a work expense); up to 1000, one "
            "batch, one undo. Only money OUT that is not a transfer leg can be flagged. "
            "Optional settled_by: the money-IN row that repaid it. A row the rules refuse "
            "is listed in refused[] with the reason and the others still apply. Taking a "
            "payment off an expense is a person's: settled_by null, a different settled_by "
            "on a paid row, or state null on one is refused."
        ),
        returns="{batch_id, changed, unchanged, not_found[], refused[]}",
        scope="write",
    ),
    ManifestEndpoint(
        method="GET",
        path=f"{_BASE}/households/{{household_id}}/transfers/findings",
        says=(
            "What the transfer matcher sees now: pairs[] of unlinked rows that look like "
            "one transfer (strength strong|suggested, why), awaiting[] rows naming another "
            "account whose other side is not imported yet, and unproven[] links no name "
            "or person vouches for. Same-currency only: the matcher never pairs two "
            "currencies, so find those yourself. limit caps each list (default 100)."
        ),
        returns=(
            "{pair_count, awaiting_count, unproven_count, truncated, pairs[], awaiting[], "
            "unproven[]}"
        ),
        scope="read",
    ),
    ManifestEndpoint(
        method="POST",
        path=f"{_BASE}/households/{{household_id}}/transfers/link",
        says=(
            "Link rows as the two legs of one transfer between the household's own "
            "accounts: pairs: [{out_id, in_id}], up to 200, one batch. Same currency needs "
            "exactly opposite amounts; across currencies the rate is worked out and "
            "returned. Your links are recorded as a program's: they appear in unproven[] "
            "until a person keeps them, and never count as history for the matcher. "
            "Refused pairs are named with why; you cannot unlink."
        ),
        returns="{batch_id, linked[], not_found[], refused[]}",
        scope="write",
    ),
    ManifestEndpoint(
        method="POST",
        path=f"{_BASE}/households/{{household_id}}/transfers/reject",
        says=(
            "Say pairs are NOT a transfer, so the matcher never offers them again: "
            "pairs: [{out_id, in_id}], up to 200, one batch. For suggestions that are "
            "really a refund, a salary or two unrelated payments. Not for a linked pair: "
            "unlinking is a person's."
        ),
        returns="{batch_id, rejected, unchanged, not_found[], refused[]}",
        scope="write",
    ),
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _rows(agent, household_id: str, ids) -> dict[str, Transaction]:
    """This household's rows among ``ids``; anything else is simply absent."""
    wanted = list(dict.fromkeys(i for i in ids if i))
    if not wanted:
        return {}
    return {
        row.id: row
        for row in agent.session.execute(
            select(Transaction).where(
                Transaction.household_id == household_id, Transaction.id.in_(wanted)
            )
        ).scalars()
    }


def _accounts(agent, household_id: str) -> dict[str, Account]:
    return {
        row.id: row
        for row in agent.session.execute(
            select(Account).where(Account.household_id == household_id)
        ).scalars()
    }


def _leg(txn: Transaction, accounts: dict[str, Account]) -> AgentTransferLeg:
    account = accounts[txn.account_id]
    return AgentTransferLeg(
        id=txn.id,
        date=txn.date,
        account_id=account.id,
        account=account.name,
        currency=account.currency,
        amount_minor=txn.amount,
        amount=insights.formatted(txn.amount, account.currency),
        description=txn.import_payee_original or (txn.payee.name if txn.payee else None),
    )


# --------------------------------------------------------------------------- #
# Story 3: work expenses
# --------------------------------------------------------------------------- #

#: Said when an agent would take a payment off a work expense. One sentence
#: for both ways of asking, since both are the same act.
UNLINK_IS_A_PERSONS = (
    "this was paid back by another row, and taking the payment off it is a person's "
    "to do -- in the register, or by undoing the batch that linked it"
)


@router.patch(
    "/households/{household_id}/transactions/reimbursement",
    response_model=ReimbursementResult,
)
def set_reimbursements(
    household_id: str, body: ReimbursementRequest, agent: AgentWriter, request: Request
) -> ReimbursementResult:
    """Flag rows as work expenses, as one act. Story 3 of #134.

    An agent reads a corporate expenses portal, finds each claimed row (with
    the match route), and flags it here. Through `set_reimbursement`, the one
    writer of the two columns, row by row -- so the rules, the refusal
    sentences and History's wording ("work expense: expected", "via <key>")
    are the ones the register's own panel produces.

    **`settled_by` is accepted because the service supports it**, and linking
    is additive: it records which payment repaid a claim, and the batch's undo
    takes it back. **Clearing a link is not accepted** -- not as
    `settled_by: null`, not as a different `settled_by` on a row already paid
    back, and not as `state: null` on one (the service would clear the link
    with the flag). That is an unlink,
    and §1.2 keeps unlinking a person's: a program should not be able to erase
    the record that a person reconciled a payment against their claims.
    """
    house = agent_api._house(agent, household_id)
    session = agent.session

    ids = [one.transaction_id for one in body.assignments]
    repeated = sorted(i for i, n in Counter(ids).items() if n > 1)
    if repeated:
        raise ValidationError(
            f"each transaction may appear once; these appear more than once: {', '.join(repeated)}"
        )

    rows = _rows(agent, house.id, [*ids, *(a.settled_by for a in body.assignments)])
    changed = unchanged = 0
    not_found: list[str] = []
    refused: list[RowRefusal] = []

    with agent.batch(kind=BatchKind.bulk_update) as acting:
        for one in body.assignments:
            txn = rows.get(one.transaction_id)
            if txn is None:
                not_found.append(one.transaction_id)
                continue
            outcome = _one_reimbursement(session, txn, one, rows)
            if outcome is None:
                unchanged += 1
            elif outcome is True:
                changed += 1
            else:
                refused.append(RowRefusal(transaction_id=txn.id, reason=outcome))
        session.flush()
        batch_id = acting.id

    request.state.agent_rows = changed
    request.state.agent_batch_id = batch_id
    return ReimbursementResult(
        batch_id=batch_id,
        changed=changed,
        unchanged=unchanged,
        not_found=not_found,
        refused=refused,
    )


def _one_reimbursement(session, txn, one: ReimbursementAssignment, rows) -> bool | str | None:
    """Apply one assignment: True if written, None if already so, else why not."""
    # Sent as null, implied by clearing the flag (which clears the link with
    # it), or by pointing the row at a different payment (which takes it off
    # the first). Refused only where there is a link to take off: `null` on a
    # row nobody linked asks for nothing, so it is not an unlink.
    clearing_link = "settled_by" in one.model_fields_set and one.settled_by is None
    moving_link = one.settled_by is not None and one.settled_by != txn.reimbursed_by_id
    if txn.reimbursed_by_id is not None and (clearing_link or moving_link or one.state is None):
        return UNLINK_IS_A_PERSONS

    settled_by: object = txn_service.UNSET
    if one.settled_by is not None:
        payment = rows.get(one.settled_by)
        if payment is None:
            return f"settled_by {one.settled_by} is not a transaction in this household"
        settled_by = payment

    same_state = txn.reimbursement == one.state
    same_link = settled_by is txn_service.UNSET or txn.reimbursed_by_id == one.settled_by
    if same_state and same_link:
        return None
    try:
        # Pending unless it writes a link (#236): a link is the one change
        # that alters what `is_payment` answers for a later row of this same
        # request, and that query cannot see what has not been flushed.
        txn_service.set_reimbursement(
            session,
            txn,
            state=one.state,
            settled_by=settled_by,
            flush=settled_by is not txn_service.UNSET,
        )
    except NotFound:
        return f"settled_by {one.settled_by} is not a transaction in this household"
    except (ValidationError, Conflict) as refusal:
        return str(refusal)
    return True


# --------------------------------------------------------------------------- #
# Story 5: transfers the matcher missed
# --------------------------------------------------------------------------- #


@router.get("/households/{household_id}/transfers/findings", response_model=TransferFindings)
def transfer_findings(
    household_id: str,
    agent: CurrentAgent,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> TransferFindings:
    """The matcher's current view, as the Transfers screen shows it. Reads only.

    The same `find` and `unproven` the screen calls, so an agent and a person
    are looking at one answer. Strong and suggested pairs are one list with a
    `strength`: to an agent both are "pairs worth deciding about", and the
    matcher's reason is what it should weigh. Each list is capped at `limit`,
    with its full count, so a ledger with a thousand suggestions costs the
    caller a sentence rather than its context window.
    """
    house = agent_api._house(agent, household_id)
    session = agent.session
    found = transfer_service.find(session, house.id)
    unproven = transfer_service.unproven(session, house.id)
    accounts = _accounts(agent, house.id)

    pairs = [*found.strong, *found.suggested]
    truncated = max(len(pairs), len(found.awaiting), len(unproven)) > limit
    answer = TransferFindings(
        pair_count=len(pairs),
        awaiting_count=len(found.awaiting),
        unproven_count=len(unproven),
        truncated=truncated,
        pairs=[
            AgentTransferPair(
                out_leg=_leg(p.out_leg, accounts),
                in_leg=_leg(p.in_leg, accounts),
                strength=p.strength,
                why=p.why,
            )
            for p in pairs[:limit]
        ],
        awaiting=[
            AgentAwaitingLeg(leg=_leg(txn, accounts), why=why)
            for txn, why in found.awaiting[:limit]
        ],
        unproven=[
            AgentLinkedPair(
                out_leg=_leg(one.out_leg, accounts),
                in_leg=_leg(one.in_leg, accounts),
                link_source=one.source.value if one.source else None,
                why=one.why,
            )
            for one in unproven[:limit]
        ],
    )
    request.state.agent_rows = len(answer.pairs) + len(answer.awaiting) + len(answer.unproven)
    return answer


#: Said when an agent would link a pair a person marked "not a transfer".
REJECTED_BY_A_PERSON = (
    "these two were marked not a transfer. Linking them now overrules that, which is "
    "a person's to do -- on the Transfers screen or in the register"
)
#: Said when an agent would reject a pair that is linked.
LINKED_ALREADY = (
    "these two are linked as one transfer. Saying they are not one means unlinking "
    "them, which is a person's to do"
)


def _pair(rows, one: PairIds, not_found: list[str]):
    """Both rows of a pair, or None having named the missing ones."""
    missing = [i for i in (one.out_id, one.in_id) if i not in rows]
    for i in missing:
        if i not in not_found:
            not_found.append(i)
    if missing:
        return None
    return rows[one.out_id], rows[one.in_id]


@router.post("/households/{household_id}/transfers/link", response_model=TransferLinkResult)
def link_transfers(
    household_id: str, body: PairsRequest, agent: AgentWriter, request: Request
) -> TransferLinkResult:
    """Link pairs the matcher missed, as one act. Story 5 of #134.

    `transfers.link` with its rules unchanged -- same household, two accounts,
    neither already a transfer, no work expense or payment, opposite
    directions, and equal amounts within one currency; across currencies the
    rate is worked out from the two amounts and stored on both legs, as for a
    person. Each pair is checked before anything is written, so a refusal
    leaves that pair as it was and the others still link.

    **Recorded as `LinkSource.agent`.** A program's link is not history: the
    matcher does not treat these two accounts as "linked before" on its
    strength, and the pair is listed under "Linked by history only" until a
    person keeps it (`person`) or unlinks it. Otherwise one wrong link by an
    agent would make the next wrong link strong, exactly as #131 found for
    history-only links.
    """
    house = agent_api._house(agent, household_id)
    session = agent.session
    rows = _rows(agent, house.id, [i for one in body.pairs for i in (one.out_id, one.in_id)])
    rejected = {
        (out_id, in_id)
        for out_id, in_id in session.execute(
            select(TransferRejection.out_transaction_id, TransferRejection.in_transaction_id).where(
                TransferRejection.household_id == house.id
            )
        ).all()
    }

    linked: list[LinkedOut] = []
    not_found: list[str] = []
    refused: list[PairRefusal] = []
    with agent.batch(kind=BatchKind.bulk_update) as acting:
        for one in body.pairs:
            legs = _pair(rows, one, not_found)
            if legs is None:
                continue
            first, second = legs
            if (first.id, second.id) in rejected or (second.id, first.id) in rejected:
                refused.append(PairRefusal(out_id=one.out_id, in_id=one.in_id, reason=REJECTED_BY_A_PERSON))
                continue
            try:
                out_leg, in_leg = transfer_service.link(
                    session, first, second, source=LinkSource.agent
                )
            except (ValidationError, Conflict) as refusal:
                refused.append(PairRefusal(out_id=one.out_id, in_id=one.in_id, reason=str(refusal)))
                continue
            linked.append(
                LinkedOut(out_id=out_leg.id, in_id=in_leg.id, fx_rate=out_leg.transfer_fx_rate)
            )
        session.flush()
        batch_id = acting.id

    request.state.agent_rows = 2 * len(linked)
    request.state.agent_batch_id = batch_id
    return TransferLinkResult(
        batch_id=batch_id, linked=linked, not_found=not_found, refused=refused
    )


@router.post("/households/{household_id}/transfers/reject", response_model=TransferRejectResult)
def reject_transfers(
    household_id: str, body: PairsRequest, agent: AgentWriter, request: Request
) -> TransferRejectResult:
    """"Not a transfer" for pairs the matcher offers, as one act.

    `transfers.reject`, so the matcher never offers the pair again -- until a
    person undoes the batch in History, which offers them again. A pair
    already rejected is counted, not refused. A linked pair is refused: a
    rejection beside a link that still stands records nothing true, and
    taking the link apart is an unlink.
    """
    house = agent_api._house(agent, household_id)
    session = agent.session
    rows = _rows(agent, house.id, [i for one in body.pairs for i in (one.out_id, one.in_id)])

    rejected = unchanged = 0
    not_found: list[str] = []
    refused: list[PairRefusal] = []
    with agent.batch(kind=BatchKind.bulk_update) as acting:
        for one in body.pairs:
            legs = _pair(rows, one, not_found)
            if legs is None:
                continue
            first, second = legs
            if first.transfer_transaction_id == second.id or second.transfer_transaction_id == first.id:
                refused.append(PairRefusal(out_id=one.out_id, in_id=one.in_id, reason=LINKED_ALREADY))
                continue
            try:
                made = transfer_service.reject(session, first, second)
            except (ValidationError, Conflict) as refusal:
                refused.append(PairRefusal(out_id=one.out_id, in_id=one.in_id, reason=str(refusal)))
                continue
            if made is None:
                unchanged += 1
            else:
                rejected += 1
        session.flush()
        batch_id = acting.id

    request.state.agent_rows = rejected
    request.state.agent_batch_id = batch_id
    return TransferRejectResult(
        batch_id=batch_id,
        rejected=rejected,
        unchanged=unchanged,
        not_found=not_found,
        refused=refused,
    )


agent_api.ENDPOINTS.extend(ENDPOINTS)
