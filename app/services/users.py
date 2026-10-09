"""Creating and managing the people who can sign in.

The owner never sets, sees or learns a member's password. They create an
invitation; the member walks the same enrolment the owner did, and picks their
own password and their own authenticator.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import Conflict, NotFound, ValidationError
from ..models import Household, HouseholdMember, Role, User, utcnow
from . import households as household_service

#: Long enough to be unguessable, short enough to read over the phone if need be.
INVITE_VALID_HOURS = 72


def list_users(session: Session) -> list[User]:
    return list(session.execute(select(User).order_by(User.created_at)).scalars())


def get_user(session: Session, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFound("no such person", code="user.not_found")
    return user


def set_disabled(session: Session, *, user: User, disabled: bool, by: User | None) -> User:
    """Disabling someone stops them signing in; it does not delete their work.

    Their name stays on every batch they ever ran, which is the point of
    recording an actor at all.

    ``by`` is the owner doing it, or None when it comes from the server, as in
    `account_resets.issue`: `scripts.reset_account --enable` is how a disabled
    account comes back when no owner who can sign in is left to do it.
    """
    if by is not None and by.role is not Role.owner:
        raise ValidationError("only the owner can do that", code="auth.owner_only")
    if by is not None and user.id == by.id and disabled:
        raise Conflict(
            "you cannot disable yourself; there would be nobody left to undo it",
            code="user.cannot_disable_self",
        )

    user.disabled_at = utcnow() if disabled else None
    if disabled:
        from ..auth import devices, sessions

        sessions.revoke_all_for(session, user.id)
        devices.revoke_all_for(session, user.id)
    session.flush()
    return user


def set_role(session: Session, *, user: User, role: Role, by: User) -> User:
    if by.role is not Role.owner:
        raise ValidationError("only the owner can do that", code="auth.owner_only")
    if user.id == by.id and role is not Role.owner:
        owners = [u for u in list_users(session) if u.role is Role.owner and u.id != user.id]
        if not owners:
            raise Conflict("there would be no owner left", code="user.no_owner_left")
    user.role = Role(role)
    session.flush()
    return user


def create_household_for(
    session: Session,
    *,
    name: str,
    creator: User,
    base_currency: str = "EUR",
    date_format: str = "YYYY-MM-DD",
    members: list[str] | None = None,
) -> Household:
    """Create a household and put people in it, in one act.

    The creator is always a member -- a household nobody can reach is data with
    no way back to it.
    """
    household = household_service.create_household(
        session,
        name=name,
        creator=creator,
        base_currency=base_currency,
        date_format=date_format,
    )
    for user_id in members or []:
        if user_id == creator.id:
            continue
        if session.get(User, user_id) is None:
            raise NotFound("no such person", code="user.not_found")
        session.add(
            HouseholdMember(
                household_id=household.id,
                user_id=user_id,
                added_by_id=creator.id,
                added_at=utcnow(),
            )
        )
    session.flush()
    return household


def all_households(session: Session) -> list[Household]:
    """Every household on the instance. Owner-only, for the admin screen."""
    return list(session.execute(select(Household).order_by(Household.created_at)).scalars())


def new_invite_token() -> tuple[str, str]:
    """``(value_to_send, sha256_to_store)`` -- the same shape as a session."""
    from ..auth import tokens

    return tokens.issue()


def invite_expiry():
    return utcnow() + timedelta(hours=INVITE_VALID_HOURS)


def unused_recovery_codes(session: Session, user_id: str) -> int:
    from ..models import RecoveryCode

    return len(
        [
            code
            for code in session.execute(
                select(RecoveryCode).where(
                    RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None)
                )
            ).scalars()
        ]
    )
