"""What a person can change about their own account.

Two acts, and the reason they live together is that both re-establish who you
are: the password and the second factor. Everything else about a user is either
set at setup or belongs to an administrator.

Both require the current password, even though the caller is already signed in.
A session cookie proves this browser was signed in at some point; it does not
prove the person at the keyboard is the account holder, and these are exactly
the two changes that would let someone who found an unlocked laptop keep the
account after the owner got it back.

Replacing the recovery codes lives here too, and asks for more than either:
the password and a live authenticator code, because the codes are the way back
in when everything else is lost.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..auth import crypto, devices, keycheck, passwords, ratelimit, sessions, stepup, tokens, totp
from ..auth import service as auth_service
from ..errors import ProofRefused, ValidationError
from ..models import RecoveryCode, User, utcnow
from . import agent_keys

#: A re-enrolment in flight, sealed into a token the client hands back. Its own
#: purpose string, so it cannot be opened as a setup blob or a pending sign-in.
REENROL = b"spendtracker/totp-reenrol/v1"
#: How long a re-enrolment stays open. Long enough to install an authenticator
#: app and scan a code; short enough that a token left in a browser's memory on
#: a shared machine is not a standing offer to replace someone's second factor.
REENROL_SECONDS = 15 * 60


#: A recovery sign-in's leave to replace an authenticator the server's key
#: cannot open (#287), sealed into the sign-in's answer. Its own purpose, so
#: it opens as nothing else and nothing else opens as it.
KEY_RECOVERY = b"spendtracker/key-recovery-grant/v1"
#: As long as the session it is bound to, capped at a day. Not fifteen
#: minutes: the member who says "not now" after the recovery sign-in, or
#: reloads the page, comes back to the profile later, and a grant that had
#: died by then would cost them the second recovery code it exists to save.
#:
#: The age is not what guards it. Three things are. It is honoured only beside
#: the session its recovery sign-in issued, which signing out ends, and so
#: does a password change or a recovery code used anywhere else. It is
#: honoured only while the key still cannot open the member, which the
#: re-enrolment it pays for ends. And only a recovery sign-in issues one, so a
#: session from before the key changed -- one that has proved nothing since --
#: can never hold one. The day is a ceiling on a sealed blob left in a
#: browser's storage, not the rule.
KEY_RECOVERY_SECONDS = 24 * 60 * 60
_GRANT_REFUSED = (
    "that permission to set up a new authenticator has expired, or is not this "
    "session's. Use an unused recovery code in its place."
)
_GRANT_NOT_NEEDED = (
    "your current authenticator can be checked again, so prove it with a code from it "
    "(or an unused recovery code) rather than the sign-in's permission."
)


#: The rate-limit counter these re-checks spend. Deliberately the step-up's own
#: rather than a new one: all three are "prove it is you, again, from inside a
#: signed-in session", and a stolen session cookie that could guess five times
#: here, five more on the step-up and five more on the authenticator would have
#: three budgets where the account has one password.
KIND = stepup.KIND


def _check_current_password(
    session: Session, engine: Engine, user: User, current: str, *, ip: str | None
) -> None:
    """The current password, behind the same limiter the step-up uses.

    Without it a stolen session cookie was an unlimited online guessing oracle
    for the password: fifty wrong answers in a row, fifty 401s, no 429. The
    attempt is reserved on its own session (`ratelimit.reserve`) before it is
    judged, so a failure survives the rollback the refusal causes and a burst
    of parallel guesses cannot all read the same count.
    """
    held = ratelimit.reserve(engine, email_canonical=user.email_canonical, ip=ip, kind=KIND)
    if not passwords.verify_password(user.password_hash, current):
        raise ProofRefused("that isn't your current password")
    ratelimit.release(engine, held)


@dataclass(frozen=True, slots=True)
class PasswordChange:
    """What changing the password took away, and what it deliberately did not."""

    #: Other browsers signed out. This one is kept.
    sessions_ended: int
    #: Browsers that had been let off the code, now asked for it again.
    devices_revoked: int
    #: Agent keys still working. Not revoked -- a key is a separate credential
    #: the person issued on purpose, from a browser they still hold -- but said,
    #: so nobody believes "signed out everywhere" covered them. Redeeming a
    #: recovery code *does* revoke them (#210, `service.redeem_recovery_code`):
    #: that is recovering from a lost device, where every browser a key could
    #: have been minted from is in doubt.
    keys_still_live: int


@dataclass(frozen=True, slots=True)
class Reenrolment:
    """A new authenticator that has been offered and not yet proved."""

    user_id: str
    secret: str


def change_password(
    session: Session,
    engine: Engine,
    user: User,
    *,
    current: str,
    replacement: str,
    keep_session_hash: str | None = None,
    ip: str | None = None,
) -> PasswordChange:
    """Swap the password, and take back everything the old one bought.

    Ending other sessions is the point: if the password is being changed
    *because* someone else knows it, leaving their session alive changes
    nothing. This browser is kept, because signing the person out of the act
    they just performed reads as a failure.

    Sessions are not the only thing the old password bought. A **trusted
    device** lets a browser in on the password alone, so a browser trusted by
    whoever knew the old one came straight back in with the new one and no
    code (`devices.py` always said a password change revoked them; this did
    not). A **pending sign-in** is the old password one code away from a
    session, and a **step-up grant** is the old password and a code, one key
    away from a credential that outlives all of this. All three go.

    **Passkeys stay** (#121), as agent keys do. A passkey was never bought
    with the password: it is its own pair of factors, held on the member's
    device, and changing a password someone else learned says nothing about
    that device. A member who suspects one of those removes it in Sign-in
    methods; an account reset (`shut_every_door`) is what removes them all.
    """
    _check_current_password(session, engine, user, current, ip=ip)

    problems = passwords.complaints(replacement, email=user.email)
    if problems:
        raise ValidationError(problems[0])
    if passwords.verify_password(user.password_hash, replacement):
        raise ValidationError("that is already your password")

    user.password_hash = passwords.hash_password(replacement)
    ended = sessions.revoke_all_for(session, user.id, except_hash=keep_session_hash)
    revoked = devices.revoke_all_for(session, user.id)
    sessions.drop_pending_for(session, user.id)
    stepup.drop_for(session, user.id)
    live = sum(1 for key in agent_keys.for_user(session, user) if key.live())
    return PasswordChange(sessions_ended=ended, devices_revoked=revoked, keys_still_live=live)


def begin_reenrolment(
    session: Session, engine: Engine, user: User, *, current: str, ip: str | None = None
) -> tuple[str, str, str]:
    """Offer a new authenticator secret. Nothing is stored yet.

    Returns ``(token, secret, uri)``. The secret lives only inside the sealed
    token until a working code comes back, for the same reason the setup wizard
    works this way: writing it first and verifying afterwards is how someone
    ends up locked out by a phone whose clock is wrong.
    """
    _check_current_password(session, engine, user, current, ip=ip)

    secret = totp.new_secret()
    token = crypto.seal_blob(
        json.dumps(asdict(Reenrolment(user_id=user.id, secret=secret))), purpose=REENROL
    )
    return token, secret, totp.provisioning_uri(secret, email=user.email)


@dataclass(frozen=True, slots=True)
class KeyRecoveryGrant:
    """What a recovery sign-in in recovery mode hands the browser it signed in."""

    user_id: str
    #: The fingerprint of the session that sign-in issued. The grant is worth
    #: nothing beside any other session, the same account's included.
    session: str


def grant_key_recovery(user: User, *, session_value: str) -> str:
    """Leave to replace the authenticator without proving the old one (#287).

    The two-codes problem: re-enrolling asks for proof of the current factor
    (`_prove_current_factor`), and for a member the server's key cannot open
    that could only ever be a *second* recovery code -- one to sign in, one to
    re-enrol, from a stock of ten that is meant to last years. The sign-in
    has just taken a password and a recovery code, which is exactly the proof
    the second code would repeat, so it hands this over instead.

    Sealed, not stored: bound to the member and the session the sign-in
    issued, good for as long as that session within `KEY_RECOVERY_SECONDS`,
    and only while the key still cannot open their secret
    (`_honour_key_recovery_grant`). That last condition is what makes it
    single-use: the re-enrolment it pays for seals a secret this key opens,
    and the grant is dead from then on. The browser keeps it for the
    profile's "Set up a new authenticator", so putting it off costs nothing.
    """
    held = KeyRecoveryGrant(user_id=user.id, session=tokens.fingerprint(session_value))
    return crypto.seal_blob(json.dumps(asdict(held)), purpose=KEY_RECOVERY)


def _honour_key_recovery_grant(user: User, grant: str, *, session_hash: str | None) -> None:
    """Stand the grant in for a code from the current authenticator, or refuse.

    No limiter: a grant is sealed with the server's key, so there is nothing
    to guess. Every refusal is a `ProofRefused`, so the session survives it.
    """
    try:
        payload = json.loads(
            crypto.open_blob(grant, max_age_seconds=KEY_RECOVERY_SECONDS, purpose=KEY_RECOVERY)
        )
        held = KeyRecoveryGrant(**payload)
    except Exception as exc:  # noqa: BLE001 -- expired, tampered or not a grant: one answer
        raise ProofRefused(_GRANT_REFUSED) from exc
    if held.user_id != user.id or session_hash is None or held.session != session_hash:
        raise ProofRefused(_GRANT_REFUSED)
    # Computed now, not when the grant was sealed: the original key may be
    # back, or this grant may already have paid for a re-enrolment.
    if not keycheck.locked_by_key(user):
        raise ProofRefused(_GRANT_NOT_NEEDED)


@dataclass(frozen=True, slots=True)
class ReenrolmentResult:
    """What replacing the authenticator took away."""

    devices_revoked: int
    sessions_ended: int


def _prove_current_factor(
    session: Session, engine: Engine, user: User, code: str, *, ip: str | None
) -> None:
    """A code from the authenticator being replaced, or an unused recovery code.

    Consumed either way. The password alone is not enough to replace the
    second factor -- with only a session and the password, somebody could
    swap in their own authenticator, then pass the step-up with it and mint a
    year-long agent key while the owner's phone quietly stopped working. The
    whole point of a second factor is that the first one does not buy it.

    A recovery code is accepted in place of the authenticator for the reason
    they exist: the phone is gone. That is the case this screen offers ("one
    you no longer have"), and without it a lost phone would leave no way to
    enrol a new one. Six digits are tried as an authenticator code, anything
    else as a recovery code; a wrong one of either spends the step-up's
    rate-limit budget, which the password re-checks share.

    Six digits from a member the server's key cannot open (#287) are refused
    before the limiter with `key_replaced` and a sentence asking for a
    recovery code in their place: no such code can be checked, and a
    recovery code here still can.
    """
    candidate = (code or "").strip().replace(" ", "")
    if candidate.isdigit() and len(candidate) == 6:
        auth_service.refuse_a_replaced_key(
            user, signed_in=True, sentence=auth_service.KEY_REPLACED_REENROL
        )
    held = ratelimit.reserve(engine, email_canonical=user.email_canonical, ip=ip, kind=KIND)
    if candidate.isdigit() and len(candidate) == 6:
        proved = totp.verify_and_consume(user, candidate)
    else:
        matched = auth_service.match_recovery_code(session, user, candidate)
        if matched is not None:
            matched.used_at = utcnow()
        proved = matched is not None
    if not proved:
        raise ProofRefused(
            "that isn't a working code from your current authenticator, or an unused recovery code"
        )
    ratelimit.release(engine, held)


def complete_reenrolment(
    session: Session,
    engine: Engine,
    user: User,
    *,
    token: str,
    code: str,
    current_code: str | None = None,
    grant: str | None = None,
    keep_session_hash: str | None = None,
    ip: str | None = None,
) -> ReenrolmentResult:
    """Prove the current factor and the new authenticator, then make the new one
    the only one.

    The current factor is a code from it or an unused recovery code -- or, for
    a member the server's key cannot open, the `grant` their recovery sign-in
    returned (`grant_key_recovery`), checked against this session
    (`keep_session_hash`).

    Every trusted device is revoked. A device was trusted on the strength of the
    old factor, and "I am replacing my second factor" and "the browsers that no
    longer need it are fine" cannot both be true -- if the old phone is gone or
    compromised, a thirty-day pass on some other machine is the hole.

    Every other session ends too, and so do half-finished sign-ins and unspent
    step-up grants, for the reason a password change ends them: a factor is
    being replaced because somebody else may hold it, and whatever they already
    bought with it has to go with it.
    """
    try:
        payload = json.loads(
            crypto.open_blob(token, max_age_seconds=REENROL_SECONDS, purpose=REENROL)
        )
        pending = Reenrolment(**payload)
    except Exception as exc:  # noqa: BLE001 -- any failure here means "start again"
        raise ValidationError("that re-enrolment has expired; start again") from exc

    if pending.user_id != user.id:
        raise ValidationError("that re-enrolment belongs to somebody else")

    # The new code first: it costs nothing to check, and checking it second
    # would burn the current authenticator's code on a typo in the new one.
    step = totp.code_matches(pending.secret, code)
    if step is None:
        raise ValidationError("that code is not right. Check the time on your phone and try again.")

    if grant is not None:
        _honour_key_recovery_grant(user, grant, session_hash=keep_session_hash)
    else:
        _prove_current_factor(session, engine, user, current_code or "", ip=ip)

    user.totp_secret = crypto.seal_totp_secret(pending.secret, user_id=user.id)
    # Burn the proving code, exactly as enrolment does: a code that has been
    # used is dead, and the counter is what enforces it. Never backwards -- the
    # current authenticator's code may just have moved it past `step`.
    user.totp_last_counter = max(step, user.totp_last_counter or step)
    revoked = devices.revoke_all_for(session, user.id)
    ended = sessions.revoke_all_for(session, user.id, except_hash=keep_session_hash)
    sessions.drop_pending_for(session, user.id)
    stepup.drop_for(session, user.id)
    return ReenrolmentResult(devices_revoked=revoked, sessions_ended=ended)


#: One message for either factor being wrong, so the refusal does not say which.
_REGENERATE_REFUSAL = "that password and authenticator code do not prove it is you"


def regenerate_recovery_codes(
    session: Session, engine: Engine, user: User, *, password: str, code: str, ip: str | None
) -> tuple[str, ...]:
    """Replace every recovery code the member has with ten new ones (#289).

    Proof is the current password **and** a code from the current
    authenticator. A recovery code is not accepted in the authenticator's
    place, unlike `_prove_current_factor`: that path exists because the phone
    is gone, and this one exists because the *sheet* may be in somebody else's
    hands -- a code from a leaked sheet must not be able to mint a fresh sheet
    and lock the owner out of their own way back in. Only six digits are tried,
    and only as an authenticator code.

    Both factors are judged under one reservation of the step-up's rate-limit
    budget, password first, for the reasons `stepup.grant` gives: a wrong one
    of either is a spent attempt, and a code is not burned for a caller who
    does not have the password.

    Every existing code goes, used or not, and the new ones are stored only as
    hashes. Sessions, trusted browsers and agent keys are left alone: this is a
    deliberate act from a browser the person holds, like changing a password,
    and replaces nothing the other credentials stood on.

    Call inside a batch. Returns the new codes, to be shown once.
    """
    from ..auth.setup import RECOVERY_CODE_COUNT

    if user.totp_secret is None:
        # Nothing to prove the second factor with. Refused before the budget is
        # touched: it is a fact about the account, not a guess.
        raise ValidationError("set up an authenticator before making new recovery codes")

    held = ratelimit.reserve(engine, email_canonical=user.email_canonical, ip=ip, kind=KIND)
    if not passwords.verify_password(user.password_hash, password):
        raise ProofRefused(_REGENERATE_REFUSAL)
    candidate = (code or "").strip().replace(" ", "")
    if not (candidate.isdigit() and len(candidate) == 6):
        raise ProofRefused(_REGENERATE_REFUSAL)
    if not totp.verify_and_consume(user, candidate):
        raise ProofRefused(_REGENERATE_REFUSAL)
    ratelimit.release(engine, held)

    for old in session.scalars(select(RecoveryCode).where(RecoveryCode.user_id == user.id)):
        session.delete(old)
    codes = tuple(secrets.token_hex(5) for _ in range(RECOVERY_CODE_COUNT))
    for one in codes:
        session.add(RecoveryCode(user_id=user.id, code_hash=passwords.hash_password(one)))
    session.flush()
    return codes


@dataclass(frozen=True, slots=True)
class DoorsShut:
    """What `shut_every_door` ended, for whoever has to say so."""

    sessions_ended: int
    devices_revoked: int
    keys_revoked: int
    passkeys_removed: int = 0


def shut_every_door(session: Session, user: User) -> DoorsShut:
    """End everything a credential of this account had already bought.

    Every session, trusted browser, half-finished sign-in, step-up grant and
    live agent key -- and every passkey (#121), which is a way in on its own:
    a reset account that a passkey still opened would not be reset. What replacing a factor that may be in somebody else's
    hands has to do, and the same list wherever it is done -- an operator's
    reset here, a reset link (`services/account_resets.py`) -- so the two
    cannot drift into ending different things.

    Call inside a batch: `agent_keys` and `passkeys` are audited.
    """
    from ..auth import passkeys

    ended = sessions.revoke_all_for(session, user.id)
    revoked = devices.revoke_all_for(session, user.id)
    sessions.drop_pending_for(session, user.id)
    stepup.drop_for(session, user.id)
    keys = 0
    for key in agent_keys.for_user(session, user):
        if key.live():
            agent_keys.revoke(session, key, by=user)
            keys += 1
    removed = passkeys.remove_all_for(session, user)
    return DoorsShut(
        sessions_ended=ended, devices_revoked=revoked, keys_revoked=keys, passkeys_removed=removed
    )


def delete_recovery_codes(session: Session, user: User) -> None:
    """Every recovery code this account has, used or not. Call inside a batch."""
    for old in session.scalars(select(RecoveryCode).where(RecoveryCode.user_id == user.id)):
        session.delete(old)


def replace_recovery_codes(session: Session, user: User) -> tuple[str, ...]:
    """Delete every recovery code this account has and issue ten new ones.

    Returned in plaintext to be shown once; only their hashes are stored.
    Call inside a batch: `recovery_codes` is audited.
    """
    from ..auth.setup import RECOVERY_CODE_COUNT

    delete_recovery_codes(session, user)
    codes = tuple(secrets.token_hex(5) for _ in range(RECOVERY_CODE_COUNT))
    for code in codes:
        session.add(RecoveryCode(user_id=user.id, code_hash=passwords.hash_password(code)))
    return codes


@dataclass(frozen=True, slots=True)
class OperatorReset:
    """What `scripts.reset_authenticator` did, for it to print once."""

    recovery_codes: tuple[str, ...]
    sessions_ended: int
    devices_revoked: int
    keys_revoked: int
    passkeys_removed: int = 0


def reset_authenticator_from_the_server(
    session: Session, user: User, *, secret: str, step: int
) -> OperatorReset:
    """Give a member a new authenticator and new recovery codes, by the operator.

    The path for a member the screens cannot help: no authenticator, no
    unused recovery code -- or every authenticator refused because the ledger
    is beside the wrong `secret.key`. Whoever can run a command against the
    data directory already holds the ledger and its key, so this grants
    nothing they did not have; it does in one audited act what would otherwise
    be hand-editing the database.

    The caller has already had a code from `secret` and passes its time
    step, for the reason the wizard and re-enrolment work that way: storing
    a secret nobody has proved is how somebody ends up locked out by a phone
    whose clock is wrong.

    As loud as redeeming a recovery code, and for the same reason: the
    factor is being replaced because it may be in somebody else's hands, so
    every session, trusted browser, half-finished sign-in, step-up grant and
    live agent key that rested on it goes too. The old recovery codes are
    deleted rather than kept: they were issued alongside the factor being
    retired, and ten new ones are shown once instead.

    Call inside a batch.
    """
    user.totp_secret = crypto.seal_totp_secret(secret, user_id=user.id)
    user.totp_last_counter = step
    codes = replace_recovery_codes(session, user)
    shut = shut_every_door(session, user)
    session.flush()
    return OperatorReset(
        recovery_codes=codes,
        sessions_ended=shut.sessions_ended,
        devices_revoked=shut.devices_revoked,
        keys_revoked=shut.keys_revoked,
        passkeys_removed=shut.passkeys_removed,
    )
