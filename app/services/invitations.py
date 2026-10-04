"""One-time links that let someone create their own account.

The point of an invitation rather than "the owner sets a password and tells
them": the owner never holds the new person's credentials. They choose their own
password, enrol their own authenticator, and keep their own recovery codes --
and an owner can invite another owner without any of that passing through them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..audit.batch import batch
from ..auth import tokens
from ..errors import Conflict, NotFound, ValidationError
from ..models import BatchKind, Household, HouseholdMember, Invitation, Role, User, utcnow

VALID_HOURS = 72

#: How long an invitation is kept once it can no longer be used -- accepted,
#: withdrawn, or past `expires_at` -- measured from whichever came first.
#: Long enough to answer "did they ever accept?" from the admin screen's
#: neighbourhood of time; after that the row is an invitee's address kept for
#: nothing. Swept by `auth/housekeeping.py`, which is the only place a
#: retention window counts (#211).
RETENTION = timedelta(days=30)

log = logging.getLogger("spendtracker")


def create(
    session: Session,
    *,
    invited_by: User,
    role: Role | str = Role.member,
    email: str | None = None,
    household_ids: list[str] | None = None,
) -> tuple[Invitation, str]:
    """Return the row and the token to put in the link. The token is shown once."""
    if invited_by.role is not Role.owner:
        raise ValidationError("only an owner can invite people")

    for household_id in household_ids or []:
        if session.get(Household, household_id) is None:
            raise NotFound("no such household")

    value, digest = tokens.issue()
    invitation = Invitation(
        token_hash=digest,
        email=(email or "").strip() or None,
        role=Role(role),
        invited_by_id=invited_by.id,
        household_ids=list(household_ids or []),
        expires_at=utcnow() + timedelta(hours=VALID_HOURS),
    )
    session.add(invitation)
    session.flush()
    return invitation, value


def lookup(session: Session, token: str) -> Invitation:
    """The invitation behind a link, if it is still good.

    One sentence for every way it can be no good: a spent link and a made-up one
    should not be distinguishable by someone holding neither.
    """
    invitation = session.execute(
        select(Invitation).where(Invitation.token_hash == tokens.fingerprint(token or ""))
    ).scalar_one_or_none()
    if invitation is None:
        raise NotFound("that invitation link is not valid")
    # One answer for every way a link can be no good. Telling a spent link
    # from an invented one hands out information to somebody holding neither.
    if invitation.accepted_at is not None or invitation.revoked_at is not None:
        raise NotFound("that invitation link is not valid")
    if invitation.expires_at <= utcnow():
        raise NotFound("that invitation link is not valid")
    return invitation


def accept(session: Session, invitation: Invitation, user: User) -> None:
    """Mark it spent and put the new person where they were invited to."""
    invitation.accepted_at = utcnow()
    invitation.accepted_user_id = user.id
    for household_id in invitation.household_ids or []:
        already = session.execute(
            select(HouseholdMember).where(
                HouseholdMember.household_id == household_id,
                HouseholdMember.user_id == user.id,
            )
        ).scalar_one_or_none()
        if already is None:
            session.add(
                HouseholdMember(
                    household_id=household_id,
                    user_id=user.id,
                    added_by_id=invitation.invited_by_id,
                    added_at=utcnow(),
                )
            )
    session.flush()


def revoke(session: Session, invitation: Invitation, *, by: User) -> Invitation:
    if by.role is not Role.owner:
        raise ValidationError("only an owner can withdraw an invitation")
    if invitation.accepted_at is not None:
        raise Conflict("that invitation has already been used; disable the account instead")
    invitation.revoked_at = utcnow()
    session.flush()
    return invitation


def spend_after_refusal(session: Session, invitation_id: str) -> None:
    """Withdraw a link whose last step was refused because the address is taken.

    Its own batch, committed by the caller even though the request then
    answers 409: the refusal is the point at which the link has told its
    holder something, so it must not survive the rollback. The actor is the
    inviter -- `batches.actor_id` needs a real user and there is no system
    account, the same answer `housekeeping` gives for keys -- and the batch's
    source says it was this, not them.
    """
    invitation = session.get(Invitation, invitation_id)
    if invitation is None or invitation.accepted_at is not None or invitation.revoked_at is not None:
        return
    with batch(
        session,
        kind=BatchKind.admin,
        actor_id=invitation.invited_by_id,
        source={"invitation": "refused: the address already has an account"},
    ):
        invitation.revoked_at = utcnow()
        session.flush()
    log.warning(
        "invitation %s was spent: its holder tried an address that already has an account",
        invitation.id,
    )


def sweep(session: Session, *, now: datetime | None = None) -> list[Invitation]:
    """Invitations that stopped being usable more than `RETENTION` ago.

    Returned rather than deleted: `invitations` is audited, so the caller
    deletes them through the ORM inside a batch -- see
    `housekeeping._sweep_invitations`.
    """
    cutoff = (now or utcnow()) - RETENTION
    return list(
        session.execute(
            select(Invitation).where(
                or_(
                    Invitation.accepted_at <= cutoff,
                    Invitation.revoked_at <= cutoff,
                    Invitation.expires_at <= cutoff,
                )
            )
        ).scalars()
    )


def outstanding(session: Session) -> list[Invitation]:
    return list(
        session.execute(
            select(Invitation)
            .where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None))
            .order_by(Invitation.created_at.desc())
        ).scalars()
    )


def link_for(token: str, *, base: str) -> str:
    return f"{base.rstrip('/')}/invite/{token}"


def lookup_by_id(session: Session, invitation_id: str) -> Invitation:
    """Re-check an invitation mid-wizard, so a link revoked while someone was
    filling the form cannot still be spent."""
    invitation = session.get(Invitation, invitation_id)
    if invitation is None:
        raise NotFound("that invitation link is not valid")
    # One answer for every way a link can be no good. Telling a spent link
    # from an invented one hands out information to somebody holding neither.
    if invitation.accepted_at is not None or invitation.revoked_at is not None:
        raise NotFound("that invitation link is not valid")
    if invitation.expires_at <= utcnow():
        raise NotFound("that invitation link is not valid")
    return invitation
