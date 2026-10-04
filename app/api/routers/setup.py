"""The first-boot wizard.

Reachable only while the instance is fresh; ``main`` returns 404 for all of it
once an owner exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from ...auth import cookies, totp
from ...auth import setup as setup_service
from ...errors import Conflict
from ...schemas import SetupBegin, SetupComplete, SetupEnrol, SetupStarted, UserOut
from ..deps import SessionDep, client_ip

router = APIRouter(tags=["setup"])


@router.get("/setup/state")
def setup_state(session: SessionDep) -> dict:
    """Whether setup is needed, and -- while it is -- where the token was written.

    The path is sent because the screen used to say `data/setup-token`, which
    has been wrong for every container since the image existed and wrong for
    any install since the default data directory moved out of the checkout. A
    hard-coded path in a instruction is a guess about somebody else's machine.

    It is **not** a secret and the token is: the path says which file to open,
    and opening it still requires being on the box. It is only sent while the
    instance is unclaimed, so a configured deployment answers with nothing
    beyond `setup_required: false`.
    """
    required = not setup_service.is_configured(session)
    if not required:
        return {"setup_required": False}
    return {
        "setup_required": True,
        "token_path": str(setup_service.setup_token_path()),
    }


@router.post("/setup/begin", response_model=SetupStarted)
def begin(body: SetupBegin, session: SessionDep) -> SetupStarted:
    """Prove you deployed this, and name the owner. Nothing is written."""
    if setup_service.is_configured(session):
        raise Conflict("this instance has already been set up")

    blob = setup_service.begin(
        body.token, email=body.email, display_name=body.display_name, password=body.password
    )
    return SetupStarted(
        blob=setup_service.seal(blob),
        otpauth_uri=totp.provisioning_uri(blob.totp_secret, email=blob.email),
        secret=blob.totp_secret,
        recovery_codes=list(blob.recovery_codes),
    )


@router.post("/setup/enrol")
def enrol(body: SetupEnrol, session: SessionDep) -> dict:
    """Prove the authenticator actually pairs, before it is load-bearing."""
    if setup_service.is_configured(session):
        raise Conflict("this instance has already been set up")
    blob = setup_service.unseal(body.blob)
    confirmed = setup_service.confirm_authenticator(blob, body.code)
    return {"blob": setup_service.seal(confirmed)}


@router.post("/setup/complete", response_model=UserOut)
def complete(
    body: SetupComplete, request: Request, response: Response, session: SessionDep
) -> UserOut:
    """One transaction: the owner, their codes, the instance, and a signed-in
    browser. Or nothing at all."""
    blob = setup_service.unseal(body.blob)
    if blob.invitation_id:
        raise Conflict("that is an invitation; finish it from the link you were sent")
    setup_service.require_codes_saved(body.codes_saved)
    user, session_value, device_value = setup_service.complete(
        session,
        blob,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    cookies.set_session(response, session_value)
    cookies.set_device(response, device_value)
    return UserOut.model_validate(user)
