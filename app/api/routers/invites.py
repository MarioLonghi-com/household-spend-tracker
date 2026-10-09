"""Invitations: creating a link, and walking the person who clicks it through
the same enrolment the first owner did.

The acceptance routes are deliberately unauthenticated -- the whole point is
that the person does not have an account yet. The link is the credential, it is
single-use, and it expires.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ... import db
from ...audit.batch import batch
from ...auth import cookies, stepup, totp
from ...auth import setup as setup_service
from ...errors import Conflict
from ...models import BatchKind, Household, Role, User
from ...schemas import (
    InviteBegin,
    InviteCreate,
    InviteCreated,
    InviteOut,
    InviteState,
    SetupComplete,
    SetupEnrol,
    SetupStarted,
    UserOut,
)
from ...services import invitations as invite_service
from ..deps import OwnerOnly, SessionDep, client_ip, public_origin

router = APIRouter(tags=["invitations"])


@router.get("/admin/invitations", response_model=list[InviteOut])
def list_invitations(session: SessionDep, owner: OwnerOnly) -> list[InviteOut]:
    return [InviteOut.model_validate(one) for one in invite_service.outstanding(session)]


@router.post("/admin/invitations", response_model=InviteCreated, status_code=201)
def create_invitation(
    body: InviteCreate, request: Request, session: SessionDep, owner: OwnerOnly
) -> InviteCreated:
    """Create a one-time link.

    An owner can invite another **owner** this way, which is the point: the role
    travels on the invitation, so administering the instance never requires
    holding somebody else's password.

    An owner-role link spends a step-up grant first (#205). With only a lifted
    session cookie, the holder could otherwise mint themselves a second owner
    account -- their own password, their own authenticator -- that survives
    every reset the real owner can make to their own credentials. The grant is
    spent before anything is written, and spent whether or not the
    invitation is then created.
    """
    if Role(body.role) is Role.owner:
        stepup.require(db.engine, body.step_up_token, user_id=owner.id)
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invitation, token = invite_service.create(
            session,
            invited_by=owner,
            role=body.role,
            email=body.email,
            household_ids=body.household_ids,
        )
    return InviteCreated(
        invitation=InviteOut.model_validate(invitation),
        link=invite_service.link_for(token, base=public_origin(request)),
    )


@router.delete("/admin/invitations/{invitation_id}", status_code=204)
def revoke_invitation(invitation_id: str, session: SessionDep, owner: OwnerOnly) -> None:
    from ...errors import NotFound
    from ...models import Invitation

    invitation = session.get(Invitation, invitation_id)
    if invitation is None:
        raise NotFound("no such invitation", code="invite.not_found")
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        invite_service.revoke(session, invitation, by=owner)


# --------------------------------------------------------------------------- #
# Accepting -- no account yet, so no authentication
# --------------------------------------------------------------------------- #


@router.get("/invite/{token}", response_model=InviteState)
def read_invitation(token: str, session: SessionDep) -> InviteState:
    invitation = invite_service.lookup(session, token)
    inviter = session.get(User, invitation.invited_by_id)
    names = []
    for household_id in invitation.household_ids or []:
        household = session.get(Household, household_id)
        if household is not None:
            names.append(household.name)
    return InviteState(
        role=invitation.role,
        email=invitation.email,
        invited_by=inviter.display_name if inviter else "somebody",
        households=names,
    )


@router.post("/invite/begin", response_model=SetupStarted)
def begin(body: InviteBegin, session: SessionDep) -> SetupStarted:
    """Name yourself and choose a password. Nothing is written."""
    invitation = invite_service.lookup(session, body.token)
    blob = setup_service.begin_invited(
        invitation_id=invitation.id,
        role=Role(invitation.role).value,
        email=body.email,
        display_name=body.display_name,
        password=body.password,
    )
    return SetupStarted(
        blob=setup_service.seal(blob),
        otpauth_uri=totp.provisioning_uri(blob.totp_secret, email=blob.email),
        secret=blob.totp_secret,
        recovery_codes=list(blob.recovery_codes),
    )


@router.post("/invite/enrol")
def enrol(body: SetupEnrol, session: SessionDep) -> dict:
    """The same mandatory authenticator check the first owner passed."""
    blob = setup_service.unseal(body.blob)
    if not blob.invitation_id:
        raise Conflict("that is not an invitation", code="invite.not_an_invitation")
    invite_service.lookup_by_id(session, blob.invitation_id)
    confirmed = setup_service.confirm_authenticator(blob, body.code)
    return {"blob": setup_service.seal(confirmed)}


@router.post("/invite/complete", response_model=UserOut)
def complete(
    body: SetupComplete, request: Request, response: Response, session: SessionDep
) -> UserOut:
    """Create the account the link was for.

    **An address that already has an account spends the link** (#211). The
    refusal has to say something -- one address, one account, is the design,
    and the person finishing the wizard is owed a reason -- so whoever holds a
    live link could otherwise walk the wizard again and again and learn which
    addresses are registered here, one per walk. Spending the link on the first
    such answer makes it one address per invitation, and the invitation
    disappearing from the owner's list, with a line in the log saying why, is
    the hint to the person who sent it. The cost falls on somebody who already
    has an account and was invited anyway; they ask for a new link, or sign in.

    The first owner's wizard cannot meet this: there are no accounts yet.
    """
    blob = setup_service.unseal(body.blob)
    if not blob.invitation_id:
        raise Conflict("that is not an invitation", code="invite.not_an_invitation")
    setup_service.require_codes_saved(body.codes_saved)

    try:
        user, session_value, device_value = setup_service.complete(
            session,
            blob,
            ip=client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except setup_service.AddressTaken:
        session.rollback()
        invite_service.spend_after_refusal(session, blob.invitation_id)
        session.commit()
        raise Conflict(
            "that email address cannot be used for a new account here, and this link is now "
            "spent. Ask whoever invited you for a new one.",
            code="invite.address_unusable",
        ) from None
    cookies.set_session(response, request, session_value)
    cookies.set_device(response, request, device_value)
    return UserOut.model_validate(user)
