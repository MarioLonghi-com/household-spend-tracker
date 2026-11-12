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

from fastapi import APIRouter, Request

from ... import db
from ...audit.batch import batch
from ...auth import crypto, passkeys, totp
from ...errors import ValidationError
from ...models import BatchKind, User
from ...schemas import PasskeyOffer, ResetAuthenticator, ResetComplete, ResetDone, ResetState
from ...services import account_resets as reset_service
from ..deps import SessionDep

router = APIRouter(tags=["account resets"])

#: A new authenticator in flight, sealed into the blob the client hands back.
#: Its own purpose, so it opens as nothing else -- not a setup blob, not a
#: re-enrolment -- and nothing else opens as it.
PURPOSE = b"spendtracker/account-reset-enrol/v1"
#: As long as the wizard gives: enough to install an app and scan.
ENROL_SECONDS = 15 * 60


#: A passkey in flight (#456): the provisional user handle and the hash of
#: the challenge it must answer, bound to this link. Its own purpose too.
PASSKEY_PURPOSE = b"spendtracker/account-reset-passkey/v1"


@dataclass(frozen=True, slots=True)
class _Offer:
    reset_id: str
    secret: str


@dataclass(frozen=True, slots=True)
class _PasskeyOffer:
    reset_id: str
    handle: str
    challenge: str


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
        raise ValidationError("this link does not change the authenticator", code="reset.not_authenticator")
    user = session.get(User, reset.user_id)
    secret = totp.new_secret()
    blob = crypto.seal_blob(json.dumps(asdict(_Offer(reset_id=reset.id, secret=secret))), purpose=PURPOSE)
    return ResetAuthenticator(
        blob=blob, otpauth_uri=totp.provisioning_uri(secret, email=user.email), secret=secret
    )


@router.post("/reset/{token}/passkey", response_model=PasskeyOffer)
def offer_passkey(token: str, request: Request, session: SessionDep) -> PasskeyOffer:
    """Creation options for a passkey in place of the authenticator (#456).

    Nothing is written but the challenge. The member's existing user handle is
    kept when they have one, so a passkey made here answers to the same
    account as any made before the reset.
    """
    reset = reset_service.lookup(session, token)
    if not reset.authenticator:
        raise ValidationError("this link does not change the authenticator", code="reset.not_authenticator")
    user = session.get(User, reset.user_id)
    handle = passkeys.handle_for(user)
    options, challenge = passkeys.enrolment_options(
        session, request, handle=handle, email=user.email, display_name=user.display_name
    )
    blob = crypto.seal_blob(
        json.dumps(asdict(_PasskeyOffer(reset_id=reset.id, handle=handle, challenge=challenge))),
        purpose=PASSKEY_PURPOSE,
    )
    return PasskeyOffer(blob=blob, options=options)


@router.post("/reset/{token}", response_model=ResetDone)
def complete_reset(
    token: str, body: ResetComplete, request: Request, session: SessionDep
) -> ResetDone:
    """Set what the link was for, spend it, and hand back the recovery codes.

    The second step is a new authenticator, or with `second_step: "passkey"`
    a passkey answering `POST /reset/{token}/passkey` (#456)."""
    reset = reset_service.lookup(session, token)

    enrolled: passkeys.Enrolled | None = None
    handle: str | None = None
    if body.second_step == "passkey":
        try:
            offer = _PasskeyOffer(
                **json.loads(
                    crypto.open_blob(
                        body.blob or "", max_age_seconds=ENROL_SECONDS, purpose=PASSKEY_PURPOSE
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 -- any failure here means "start again"
            raise ValidationError(
                "that passkey could not be registered here. Start again.",
                code="passkey.not_registered",
            ) from exc
        if offer.reset_id != reset.id or body.credential is None:
            raise ValidationError(
                "that passkey could not be registered here. Start again.",
                code="passkey.not_registered",
            )
        enrolled = passkeys.verify_enrolment(
            db.engine, request, credential=body.credential, challenge_hash=offer.challenge
        )
        handle = offer.handle

    secret: str | None = None
    if body.blob and enrolled is None:
        try:
            offer = _Offer(
                **json.loads(crypto.open_blob(body.blob, max_age_seconds=ENROL_SECONDS, purpose=PURPOSE))
            )
        except Exception as exc:  # noqa: BLE001 -- any failure here means "scan again"
            raise ValidationError(
            "that authenticator offer has expired; scan a new one", code="reset.offer_expired"
        ) from exc
        # Bound to this link: an offer from another one is not this account's.
        if offer.reset_id != reset.id:
            raise ValidationError(
            "that authenticator offer has expired; scan a new one", code="reset.offer_expired"
        )
        secret = offer.secret

    with batch(
        session,
        kind=BatchKind.admin,
        actor_id=reset.user_id,
        source={"account_reset": "followed the link"},
    ):
        codes = reset_service.redeem(
            session,
            reset,
            new_password=body.password,
            totp_secret=secret,
            totp_code=body.code,
            passkey=enrolled,
            passkey_handle=handle,
        )
    return ResetDone(recovery_codes=codes)
