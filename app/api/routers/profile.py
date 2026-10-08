"""Your own account: the password, the second factor, and proving it is you.

The two writes go through a batch, like every other write in this app -- a
password change is a deliberate act and belongs in the history, even though the
audit log redacts the hash itself so what is recorded is "this changed", never
to what.

The third route writes no ledger row and opens no batch. It exchanges both
factors for a single-use grant that a privileged act elsewhere spends; see
`app/auth/stepup.py` for why that act needs a higher bar than the two here.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ... import config, db
from ...audit.batch import batch
from ...auth import cookies, keycheck, passkeys, stepup, tokens

# `from ...db import engine` would bind the object at import time, which is the
# trap `routers/auth.py` carries a comment about: the test harness rebuilds the
# engine per test and reloads `app.db`, but not this module, so the handle
# would stay pointed at the previous test's database. Rate limiting and the
# grant spend both write through it, and both would go to the wrong file.
from ...errors import NotFound
from ...models import AgentKey, BatchKind, Household, Passkey
from ...schemas import (
    AgentKeyIssued,
    AgentKeyOut,
    AuthenticatorStatus,
    ChangePassword,
    ConfirmReenrolment,
    IssueAgentKey,
    PasskeyOptionsRequest,
    PasskeyOut,
    PasswordChanged,
    RecoveryCodesIssued,
    RecoveryCodesLeft,
    ReenrolmentDone,
    ReenrolmentOffer,
    RegenerateRecoveryCodes,
    RegisterPasskey,
    RenamePasskey,
    StartReenrolment,
    StepUpGranted,
    StepUpRequest,
)
from ...services import agent_keys as key_service
from ...services import profile as profile_service
from ...services import users as user_service
from ..deps import CurrentUser, SessionDep, client_ip

router = APIRouter(tags=["profile"])


@router.post("/me/password", response_model=PasswordChanged)
def change_password(
    body: ChangePassword, request: Request, session: SessionDep, user: CurrentUser
) -> PasswordChanged:
    """Change your password, and end every other session, trusted browser,
    half-finished sign-in and step-up grant.

    This browser keeps its session: signing someone out of the act they just
    completed reads as the change having failed.
    """
    value = cookies.session_value(request)
    keep = tokens.fingerprint(value) if value else None
    with batch(session, kind=BatchKind.manual, actor_id=user.id):
        done = profile_service.change_password(
            session,
            db.engine,
            user,
            current=body.current_password,
            replacement=body.new_password,
            keep_session_hash=keep,
            ip=client_ip(request),
        )
    return PasswordChanged(
        other_sessions_ended=done.sessions_ended,
        devices_revoked=done.devices_revoked,
        keys_still_live=done.keys_still_live,
    )


@router.get("/me/authenticator", response_model=AuthenticatorStatus)
def authenticator_status(user: CurrentUser) -> AuthenticatorStatus:
    """Whether the caller has an authenticator, and whether this server's key
    opens it (#287).

    What the profile asks before it offers a new one: a member the key cannot
    open is asked for a recovery code, or spends the grant their recovery
    sign-in left in this browser, rather than for a code no authenticator of
    theirs can give. Only ever the caller -- there is no id to ask about
    anybody else. Computed on every ask, like everything in recovery mode.
    """
    return AuthenticatorStatus(
        enrolled=user.totp_secret is not None, locked_by_key=keycheck.locked_by_key(user)
    )


@router.post("/me/authenticator", response_model=ReenrolmentOffer)
def start_reenrolment(
    body: StartReenrolment, request: Request, session: SessionDep, user: CurrentUser
) -> ReenrolmentOffer:
    """Offer a new authenticator secret. Nothing is stored until it is proved.

    No batch: nothing is written. The secret exists only inside the returned
    token until a working code comes back.
    """
    token, secret, uri = profile_service.begin_reenrolment(
        session, db.engine, user, current=body.current_password, ip=client_ip(request)
    )
    return ReenrolmentOffer(token=token, secret=secret, uri=uri)


@router.post("/me/authenticator/confirm", response_model=ReenrolmentDone)
def confirm_reenrolment(
    body: ConfirmReenrolment, request: Request, session: SessionDep, user: CurrentUser
) -> ReenrolmentDone:
    """Prove the current factor and the new authenticator, then make the new
    one the only one. This browser keeps its session; every other one ends.

    The current factor is `current_code`, or in recovery mode the `grant` a
    recovery sign-in in this same session returned (#287)."""
    value = cookies.session_value(request)
    keep = tokens.fingerprint(value) if value else None
    with batch(session, kind=BatchKind.manual, actor_id=user.id):
        done = profile_service.complete_reenrolment(
            session,
            db.engine,
            user,
            token=body.token,
            code=body.code,
            current_code=body.current_code,
            grant=body.grant,
            keep_session_hash=keep,
            ip=client_ip(request),
        )
    return ReenrolmentDone(
        devices_revoked=done.devices_revoked, other_sessions_ended=done.sessions_ended
    )


@router.get("/me/recovery-codes", response_model=RecoveryCodesLeft)
def recovery_codes_left(session: SessionDep, user: CurrentUser) -> RecoveryCodesLeft:
    """How many unused recovery codes you have. Never the codes: only hashes
    are stored.

    Its own route rather than a field on ``/me``: ``UserOut`` is also what
    ``/presence`` returns about other people, and how many ways back in
    somebody has left is nobody else's business.
    """
    return RecoveryCodesLeft(unused=user_service.unused_recovery_codes(session, user.id))


@router.post("/me/recovery-codes", response_model=RecoveryCodesIssued)
def regenerate_recovery_codes(
    body: RegenerateRecoveryCodes, request: Request, session: SessionDep, user: CurrentUser
) -> RecoveryCodesIssued:
    """Replace every recovery code with ten new ones, shown once (#289).

    Both factors, and the second must be the authenticator: a code from a
    leaked sheet cannot mint a new sheet. Sessions, trusted browsers and agent
    keys are left alone. `manual`, like the password and authenticator
    changes beside it: one deliberate act by the person it is about.
    """
    with batch(session, kind=BatchKind.manual, actor_id=user.id):
        codes = profile_service.regenerate_recovery_codes(
            session,
            db.engine,
            user,
            password=body.password,
            code=body.code,
            ip=client_ip(request),
        )
    return RecoveryCodesIssued(codes=list(codes))


@router.post("/me/step-up", response_model=StepUpGranted)
def step_up(
    body: StepUpRequest, request: Request, session: SessionDep, user: CurrentUser
) -> StepUpGranted:
    """Prove both factors now, and get one act's worth of authority.

    No batch: nothing in the ledger changes. The grant is a row in an unaudited
    table, spent by whichever route it was obtained for, and gone either way.

    The authenticator code is consumed, so it cannot also finish a sign-in in
    the same thirty seconds -- the cost `totp.verify_and_consume` documents,
    imposed here for the same reason.
    """
    value, expires_at = stepup.grant(
        session,
        db.engine,
        user,
        password=body.password,
        code=body.code,
        ip=client_ip(request),
    )
    return StepUpGranted(token=value, expires_at=expires_at)


# --------------------------------------------------------------------------- #
# Keys you have given to programs
# --------------------------------------------------------------------------- #


def _out(key: AgentKey) -> AgentKeyOut:
    """A key as its owner sees it. Never the token.

    There is nothing to return even if a route wanted to: only the SHA-256 is
    stored, which is the same property that makes revocation a row read.
    """
    return AgentKeyOut(
        id=key.id,
        label=key.label,
        agent_name=key.agent_name,
        household_id=key.household_id,
        scopes=key.scope.granted,
        may_commit=key.may_commit,
        created_at=key.created_at,
        expires_at=key.expires_at,
        last_used_at=key.last_used_at,
        revoked_at=key.revoked_at,
        live=key.live(),
    )


@router.get("/me/keys", response_model=list[AgentKeyOut])
def my_keys(session: SessionDep, user: CurrentUser) -> list[AgentKeyOut]:
    """Every key you have issued, newest first, dead ones included.

    Revoked and expired keys are shown rather than hidden: *what did I give
    out, and when did it stop working* is the question this list answers, and
    one that silently drops entries answers it wrongly.
    """
    return [_out(key) for key in key_service.for_user(session, user)]


@router.post("/me/keys", response_model=AgentKeyIssued, status_code=201)
def issue_key(
    body: IssueAgentKey, request: Request, session: SessionDep, user: CurrentUser
) -> AgentKeyIssued:
    """Mint a key, and show its token exactly once.

    The session cookie is not enough. A key's effect outlives the session that
    performed it, so issuing one spends a step-up grant -- both factors, proved
    within the last five minutes. See `app/auth/stepup.py` for the argument,
    and for the other acts that pay the same way; this was once the only one,
    and the docstring saying so is how a backup with `secret.key` in it came to
    be guarded one step lower (#204).

    The grant is spent **before** anything is written, and spent whether or not
    what follows succeeds: one grant is worth one attempt at one key.
    """
    stepup.require(db.engine, body.step_up_token, user_id=user.id)

    household = session.get(Household, body.household_id)
    if household is None:
        # 404 before the service is reached, so an id that never existed and
        # one in a household this person is not in read identically. The
        # service repeats the check for its own callers.
        raise NotFound("no such household")

    # A batch, because `agent_keys` is audited and issuing a credential is
    # exactly the kind of act History should carry. `admin`, not a new kind:
    # BatchKind answers what was done, and this is administration.
    with batch(
        session, kind=BatchKind.admin, actor_id=user.id, household_id=household.id
    ):
        key, token = key_service.issue(
            session,
            user=user,
            household=household,
            label=body.label,
            agent_name=body.agent_name,
            scope=body.scope,
            may_commit=body.may_commit,
            days=body.days,
        )
    return AgentKeyIssued(key=_out(key), token=token)


@router.post("/me/keys/{key_id}/revoke", response_model=AgentKeyOut)
def revoke_key(
    key_id: str, session: SessionDep, user: CurrentUser
) -> AgentKeyOut:
    """Stop a key working. The next request it makes is a 401.

    No step-up. Taking a credential away is the safe direction, and a bar high
    enough to slow somebody down in the moment they realise a key has leaked is
    a bar in the wrong place.
    """
    key = session.get(AgentKey, key_id)
    if key is None or key.user_id != user.id:
        # Somebody else's key reads exactly like one that was never real.
        raise NotFound("no such key")

    with batch(
        session, kind=BatchKind.admin, actor_id=user.id, household_id=key.household_id
    ):
        key_service.revoke(session, key, by=user)
    return _out(key)


# --------------------------------------------------------------------------- #
# Passkeys (#120)
# --------------------------------------------------------------------------- #


def _passkey_out(passkey: Passkey) -> PasskeyOut:
    return PasskeyOut(
        id=passkey.id,
        label=passkey.label,
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
        synced=passkey.backed_up,
        rp_id=passkey.rp_id,
        usable_here=passkey.rp_id == config.settings.rp_id,
        aaguid=passkey.aaguid,
    )


@router.get("/me/passkeys", response_model=list[PasskeyOut])
def my_passkeys(session: SessionDep, user: CurrentUser) -> list[PasskeyOut]:
    """Your passkeys, newest first, including any made for another host name
    -- marked `usable_here: false`, so they can be seen and removed rather
    than silently not working."""
    return [_passkey_out(one) for one in passkeys.for_user(session, user)]


@router.post("/me/passkeys/options")
def passkey_options(
    body: PasskeyOptionsRequest, request: Request, session: SessionDep, user: CurrentUser
) -> dict:
    """Begin adding a passkey: the options for `navigator.credentials.create()`.

    Gated like an agent key (`app/auth/stepup.py`): a passkey is a way in that
    outlives this session, so it costs a fresh password + code. Whether this
    instance can offer passkeys at all is checked first, so a grant is not
    spent on a request that could only be refused. The grant is then spent
    whether or not a passkey follows.
    """
    passkeys.refuse_unless_available(request)
    stepup.require(db.engine, body.step_up_token, user_id=user.id)
    if user.webauthn_user_handle is None:
        # `users` is audited; the handle is written once, on a first passkey.
        with batch(session, kind=BatchKind.manual, actor_id=user.id):
            passkeys.give_user_handle(user)
    return passkeys.registration_options(session, user, request)


@router.post("/me/passkeys", response_model=PasskeyOut, status_code=201)
def register_passkey(
    body: RegisterPasskey, request: Request, session: SessionDep, user: CurrentUser
) -> PasskeyOut:
    """Finish adding a passkey: verify the browser's answer and keep it.

    No second grant: the challenge the options issued is the proof, single-use,
    bound to this member, and five minutes long.
    """
    with batch(session, kind=BatchKind.manual, actor_id=user.id):
        passkey = passkeys.register(
            session, db.engine, user, request, credential=body.credential, label=body.label
        )
    return _passkey_out(passkey)


@router.patch("/me/passkeys/{passkey_id}", response_model=PasskeyOut)
def rename_passkey(
    passkey_id: str, body: RenamePasskey, session: SessionDep, user: CurrentUser
) -> PasskeyOut:
    passkey = passkeys.owned(session, user, passkey_id)
    with batch(session, kind=BatchKind.manual, actor_id=user.id):
        passkeys.rename(passkey, body.label)
    return _passkey_out(passkey)


@router.delete("/me/passkeys/{passkey_id}", status_code=204)
def remove_passkey(passkey_id: str, session: SessionDep, user: CurrentUser) -> None:
    """Remove a passkey. It stops working at once.

    No step-up, for the reason revoking an agent key has none: taking a way in
    away is the safe direction. Hard-deleted, like everything else here; the
    audit log keeps the before-image, and undo never brings it back
    (`undo.CREDENTIAL_TABLES`).
    """
    passkey = passkeys.owned(session, user, passkey_id)
    with batch(session, kind=BatchKind.manual, actor_id=user.id):
        session.delete(passkey)
