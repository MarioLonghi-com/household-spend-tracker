"""Signing in and out, and who is here."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from ... import db
from ...audit.batch import batch
from ...auth import cookies, keycheck, service
from ...auth import devices as device_service
from ...auth import sessions as session_service

# `from ...db import engine` would bind the object at import time. The test
# harness rebuilds the engine per test and reloads app.db, so this module kept
# a handle on the previous test's database and wrote every login attempt there
# -- which is why no TestClient test could ever observe rate limiting.
from ...errors import Unauthorized
from ...models import BatchKind, User
from ...schemas import (
    PresenceOut,
    SignIn,
    SignInState,
    SubmitCode,
    SubmitRecoveryCode,
    UserOut,
)
from ...services import profile as profile_service
from ..deps import CurrentUser, SessionDep, client_ip, device_cookie

router = APIRouter(tags=["auth"])

#: The half-finished sign-in. Named and written by `auth/cookies.py`, which
#: owns the names and the flags of all three of this app's cookies -- there is
#: nothing to clean up here if the browser walks away.
PENDING_MAX_AGE = cookies.PENDING_MAX_AGE


@router.post("/session", response_model=SignInState)
def sign_in(
    body: SignIn, request: Request, response: Response, session: SessionDep
) -> SignInState:
    user = service.check_password(
        session,
        db.engine,
        email=body.email,
        password=body.password,
        ip=client_ip(request),
        device_cookie=device_cookie(request),
    )

    trusted = device_service.is_trusted(session, user, device_cookie(request))
    locked = keycheck.locked_by_key(user)
    # A trusted browser skips the code, and an account whose authenticator a
    # reset cleared has no code to skip to (#284). The reset revoked every
    # trusted browser; this is the second line, so trust can never stand in
    # for a factor the account no longer has.
    #
    # Nor for one the server can no longer check (#287). The trust was earned
    # with an authenticator this key cannot open, and letting it through
    # would sign the member in on the password alone and never tell them:
    # they would stay in recovery mode, unable to pass a step-up, until the
    # thirty days ran out. So the half-finished sign-in it is, and this
    # answer tells them.
    if trusted is None or user.totp_secret is None or locked:
        # The browser has not passed a second factor recently. Hold the
        # half-finished sign-in in a sealed cookie and ask for a code.
        # A row, not just a sealed cookie. The sealed value alone was a bearer
        # token: replayable, and portable to any browser.
        cookies.set_pending(
            response, session_service.issue_pending(session, user, seconds=PENDING_MAX_AGE)
        )
        # In recovery mode the answer says so already (#287), and the screen
        # asks for a recovery code without first asking for a code that
        # cannot work. The password has just been proven, so this tells the
        # caller nothing the code step's refusal would not; that refusal
        # stays, as the second line for a client that never read this.
        return SignInState(
            authenticated=False,
            needs_code=True,
            key_replaced=locked,
            detail=service.KEY_REPLACED if locked else None,
        )

    result = service.complete_sign_in(
        session,
        user,
        trust_this_browser=False,
        device_cookie=device_cookie(request),
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    cookies.set_session(response, result.session_value)
    return SignInState(authenticated=True, user=UserOut.model_validate(user))


@router.post("/session/code", response_model=SignInState)
def submit_code(
    body: SubmitCode, request: Request, response: Response, session: SessionDep
) -> SignInState:
    pending = cookies.pending_value(request.cookies)
    # Recovery mode (#287), before the claim: no code from the authenticator
    # can be right, so none is tried and the half-finished sign-in is left for
    # the recovery code the refusal sends them to.
    waiting_id = session_service.peek_pending(session, pending)
    waiting = session.get(User, waiting_id) if waiting_id else None
    if waiting is not None and waiting.disabled_at is None:
        service.refuse_a_replaced_key(waiting)

    # Claiming spends it, whether or not the code below turns out to be right --
    # so a stolen cookie is worth one attempt, not five minutes of them.
    user_id = session_service.claim_pending(db.engine, pending)
    user = session.get(User, user_id) if user_id else None
    if user is None or user.disabled_at is not None:
        raise Unauthorized("start again from the sign-in page")

    service.check_code(session, db.engine, user, body.code, ip=client_ip(request))
    result = service.complete_sign_in(
        session,
        user,
        trust_this_browser=body.trust_this_browser,
        device_cookie=device_cookie(request),
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    cookies.set_session(response, result.session_value)
    if result.device_value:
        cookies.set_device(response, result.device_value)
    cookies.clear_pending(response)
    return SignInState(authenticated=True, user=UserOut.model_validate(user))


@router.post("/session/recovery", response_model=SignInState)
def submit_recovery_code(
    body: SubmitRecoveryCode, request: Request, response: Response, session: SessionDep
) -> SignInState:
    """The way back in when the authenticator is gone.

    Same door as the code: the password has already been checked, so this stands
    in for the second factor and nothing else. Redeeming a code revokes every
    session and every trusted browser, and this browser is deliberately *not*
    trusted afterwards -- somebody who has just lost their phone should be asked
    for a factor again, not handed thirty days. Every live agent key is revoked
    too, and the answer says how many (#210).

    In recovery mode -- the server's key cannot open this member's
    authenticator (#287) -- the answer also carries a `reenrolment_grant`, so
    setting up a new authenticator does not cost a second recovery code.
    """
    user_id = session_service.claim_pending(db.engine, cookies.pending_value(request.cookies))
    user = session.get(User, user_id) if user_id else None
    if user is None or user.disabled_at is not None:
        raise Unauthorized("start again from the sign-in page")

    with batch(session, kind=BatchKind.admin, actor_id=user.id):
        keys_revoked = service.redeem_recovery_code(
            session, db.engine, user, body.code, ip=client_ip(request)
        )

    result = service.complete_sign_in(
        session,
        user,
        trust_this_browser=False,
        device_cookie=None,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    cookies.set_session(response, result.session_value)
    cookies.clear_pending(response)
    grant = (
        profile_service.grant_key_recovery(user, session_value=result.session_value)
        if keycheck.locked_by_key(user)
        else None
    )
    return SignInState(
        authenticated=True,
        user=UserOut.model_validate(user),
        keys_revoked=keys_revoked,
        reenrolment_grant=grant,
    )


@router.delete("/session", status_code=204)
def sign_out(request: Request, response: Response, session: SessionDep, user: CurrentUser) -> None:
    value = cookies.session_value(request.cookies)
    if value:
        session_service.revoke(session, value)
    cookies.clear_session(response)


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/presence", response_model=PresenceOut)
def presence(session: SessionDep, user: CurrentUser) -> PresenceOut:
    """Who else is here right now.

    Read off ``sessions.last_seen_at``, which every request already keeps
    roughly current -- no extra bookkeeping, and nothing to clean up.
    """
    online_ids = session_service.online_users(session)
    if not online_ids:
        return PresenceOut(online=[])

    # Only people you share a household with. This is the one authenticated
    # route with nothing else scoping it, and it renders email addresses --
    # so unscoped it told any member who else uses the instance.
    from ...models import HouseholdMember

    mine = select(HouseholdMember.household_id).where(HouseholdMember.user_id == user.id)
    visible = select(HouseholdMember.user_id).where(HouseholdMember.household_id.in_(mine))
    rows = session.execute(
        select(User).where(
            User.id.in_(online_ids),
            # Yourself always: somebody in no household yet should see "only
            # you", not an empty list that looks like the feature is broken.
            (User.id.in_(visible)) | (User.id == user.id),
        )
    ).scalars().all()
    return PresenceOut(online=[UserOut.model_validate(row) for row in rows])
