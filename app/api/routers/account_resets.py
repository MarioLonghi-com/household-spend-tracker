"""Following an account reset link (#284).

Unauthenticated, like accepting an invitation, and for the same reason: the
account behind the link has been shut, so there is nothing to sign in with.
The link is the credential -- 256 random bits, single-use, expiring -- and an
unknown, spent or lapsed one is a 404 with one sentence for all three.

Issuing a link is not here. The owner's screen (#286, `routers/admin.py`) and
the server command (#285) call `services.account_resets` themselves.

**Nothing here signs anybody in.** The link proves the person was handed it;
a session needs a password and a second factor, and the link supplies at most
one of them. When it is done they sign in like anyone else.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from fastapi import APIRouter

from ...audit.batch import batch
from ...auth import crypto, totp
from ...errors import ValidationError
from ...models import BatchKind, User
from ...schemas import ResetAuthenticator, ResetComplete, ResetDone, ResetState
from ...services import account_resets as reset_service
from ..deps import SessionDep

router = APIRouter(tags=["account resets"])

#: A new authenticator in flight, sealed into the blob the client hands back.
#: Its own purpose, so it opens as nothing else -- not a setup blob, not a
#: re-enrolment -- and nothing else opens as it.
PURPOSE = b"spendtracker/account-reset-enrol/v1"
#: As long as the wizard gives: enough to install an app and scan.
ENROL_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class _Offer:
    reset_id: str
    secret: str


@router.get("/reset/{token}", response_model=ResetState)
def read_reset(token: str, session: SessionDep) -> ResetState:
    reset = reset_service.lookup(session, token)
    user = session.get(User, reset.user_id)
    by = session.get(User, reset.created_by_id) if reset.created_by_id else None
    return ResetState(
        email=user.email,
        display_name=user.display_name,
        password=reset.password,
        authenticator=reset.authenticator,
        reset_by=by.display_name if by is not None else None,
        created_at=reset.created_at,
    )


@router.post("/reset/{token}/authenticator", response_model=ResetAuthenticator)
def offer_authenticator(token: str, session: SessionDep) -> ResetAuthenticator:
    """A new secret to scan. Nothing is written until a code from it comes back."""
    reset = reset_service.lookup(session, token)
    if not reset.authenticator:
        raise ValidationError("this link does not change the authenticator")
    user = session.get(User, reset.user_id)
    secret = totp.new_secret()
    blob = crypto.seal_blob(json.dumps(asdict(_Offer(reset_id=reset.id, secret=secret))), purpose=PURPOSE)
    return ResetAuthenticator(
        blob=blob, otpauth_uri=totp.provisioning_uri(secret, email=user.email), secret=secret
    )


@router.post("/reset/{token}", response_model=ResetDone)
def complete_reset(token: str, body: ResetComplete, session: SessionDep) -> ResetDone:
    """Set what the link was for, spend it, and hand back the recovery codes."""
    reset = reset_service.lookup(session, token)

    secret: str | None = None
    if body.blob:
        try:
            offer = _Offer(
                **json.loads(crypto.open_blob(body.blob, max_age_seconds=ENROL_SECONDS, purpose=PURPOSE))
            )
        except Exception as exc:  # noqa: BLE001 -- any failure here means "scan again"
            raise ValidationError("that authenticator offer has expired; scan a new one") from exc
        # Bound to this link: an offer from another one is not this account's.
        if offer.reset_id != reset.id:
            raise ValidationError("that authenticator offer has expired; scan a new one")
        secret = offer.secret

    with batch(
        session,
        kind=BatchKind.admin,
        actor_id=reset.user_id,
        source={"account_reset": "followed the link"},
    ):
        codes = reset_service.redeem(
            session, reset, new_password=body.password, totp_secret=secret, totp_code=body.code
        )
    return ResetDone(recovery_codes=codes)
