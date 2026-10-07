"""Linking two existing rows as one transfer, and finding the ones to link.

Issues #70 and #71. `POST /transfers` (in `transactions.py`) makes a transfer
from nothing; these routes turn two rows that already exist -- one from each
bank's statement -- into its two legs.
"""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from ...audit.batch import batch
from ...errors import NotFound
from ...models import Account, BatchKind, Transaction
from ...schemas import (
    AwaitingLegOut,
    LinkedPairOut,
    TransferFindingsOut,
    TransferLegOut,
    TransferLinkPair,
    TransferLinkRequest,
    TransferLinkResult,
    TransferPairOut,
    TransferRejectRequest,
    TransferRejectResult,
)
from ...services import transfers as transfer_service
from ..deps import CurrentHousehold, CurrentUser, SessionDep, load_for

router = APIRouter(tags=["transfers"])


def _leg(txn: Transaction, accounts: dict[str, Account]) -> TransferLegOut:
    account = accounts[txn.account_id]
    return TransferLegOut(
        id=txn.id,
        account_id=account.id,
        account_name=account.name,
        date=txn.date,
        amount=txn.amount,
        currency=account.currency,
        description=txn.import_payee_original or (txn.payee.name if txn.payee else None),
    )


@router.get("/households/{household_id}/transfers/findings", response_model=TransferFindingsOut)
def findings(household: CurrentHousehold, session: SessionDep) -> TransferFindingsOut:
    """Likely transfers among rows nobody has linked yet. Reads only.

    The one-time sweep over a ledger imported before matching existed, and the
    place a suggested pair is confirmed afterwards. Also the links already
    made that nothing but account history vouches for (#131).
    """
    found = transfer_service.find(session, household.id)
    unproven = transfer_service.unproven(session, household.id)
    accounts = {
        row.id: row
        for row in session.execute(
            select(Account).where(Account.household_id == household.id)
        ).scalars()
    }

    def pair(p: transfer_service.Pair) -> TransferPairOut:
        return TransferPairOut(
            out_leg=_leg(p.out_leg, accounts),
            in_leg=_leg(p.in_leg, accounts),
            strength=p.strength,
            why=p.why,
            words=list(p.shared),
        )

    return TransferFindingsOut(
        strong=[pair(p) for p in found.strong],
        suggested=[pair(p) for p in found.suggested],
        awaiting=[AwaitingLegOut(leg=_leg(t, accounts), why=why) for t, why in found.awaiting],
        unproven=[
            LinkedPairOut(
                out_leg=_leg(one.out_leg, accounts),
                in_leg=_leg(one.in_leg, accounts),
                link_source=one.source.value if one.source else None,
                why=one.why,
            )
            for one in unproven
        ],
    )


def _pair_in(session, household_id: str, one: TransferLinkPair) -> tuple[Transaction, Transaction]:
    legs = []
    for txn_id in (one.first_id, one.second_id):
        txn = session.get(Transaction, txn_id)
        if txn is None or txn.household_id != household_id:
            raise NotFound("no such transaction")
        legs.append(txn)
    return legs[0], legs[1]


@router.post("/households/{household_id}/transfers/link", response_model=TransferLinkResult)
def link(
    body: TransferLinkRequest,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
) -> TransferLinkResult:
    """Link each pair as the two legs of one transfer, all as one act.

    One pair from the register's "Link as transfer", or every strong pair the
    sweep found -- and then what those made strong, until nothing new is
    (#88). Either way one batch, so one undo puts them all back.
    """
    with batch(session, kind=BatchKind.bulk_update, actor_id=user.id, household_id=household.id):
        pairs = [_pair_in(session, household.id, one) for one in body.pairs]
        if body.by == "evidence":
            # Can be more than were sent: what those links made strong (#88).
            linked = transfer_service.link_on_evidence(session, household.id, pairs)
        else:
            for first, second in pairs:
                transfer_service.link(session, first, second)
            linked = len(pairs)
    return TransferLinkResult(linked=linked)


@router.post("/households/{household_id}/transfers/reject", response_model=TransferRejectResult)
def reject(
    body: TransferRejectRequest,
    household: CurrentHousehold,
    session: SessionDep,
    user: CurrentUser,
) -> TransferRejectResult:
    """"Not a transfer": never offer these pairs again (#131). One batch, so
    one undo in History offers them again."""
    rejected = 0
    with batch(session, kind=BatchKind.bulk_update, actor_id=user.id, household_id=household.id):
        for one in body.pairs:
            first, second = _pair_in(session, household.id, one)
            if transfer_service.reject(session, first, second) is not None:
                rejected += 1
    return TransferRejectResult(rejected=rejected)


@router.post("/transactions/{transaction_id}/confirm-link", status_code=204)
def confirm_link(transaction_id: str, session: SessionDep, user: CurrentUser) -> None:
    """A person says a link is right: it leaves "Linked by history only" and
    counts as history from now on (#131)."""
    txn = load_for(session, user, Transaction, transaction_id)
    with batch(
        session, kind=BatchKind.manual, actor_id=user.id, household_id=txn.household_id
    ):
        transfer_service.confirm(session, txn)


@router.post("/transactions/{transaction_id}/unlink", status_code=204)
def unlink(transaction_id: str, session: SessionDep, user: CurrentUser) -> None:
    """Turn a transfer's two legs back into two ordinary rows."""
    txn = load_for(session, user, Transaction, transaction_id)
    with batch(
        session, kind=BatchKind.manual, actor_id=user.id, household_id=txn.household_id
    ):
        transfer_service.unlink(session, txn)
