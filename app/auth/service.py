"""Signing in.

The flow the brief asked for: a password, then an authenticator code unless this
browser has already passed one in the last thirty days, then a session.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import ProofRefused, Unauthorized
from ..models import RecoveryCode, User, utcnow
from ..services import agent_keys
from . import devices, email_canonical, keycheck, passwords, ratelimit, sessions, totp

#: One sentence for every way a sign-in can fail on the first step. Saying
#: "no such account" would confirm which addresses exist.
_REFUSAL = "that email and password do not match"

#: The code step, for an account whose authenticator a reset cleared (#284).
#: Said only to somebody who has just given the right password, so it tells
#: nobody anything they could not already learn by signing in.
AUTHENTICATOR_CLEARED = (
    "this account's authenticator was reset, so there is no code to give. "
    "Use the reset link you were sent to enrol a new one, or ask an owner for a new link."
)

#: The code step, for a member whose authenticator this server's key cannot
#: open (#287): recovery mode. Said, like the sentence above, only to somebody
#: who has just given the right password -- in the first step's answer, then
#: as the code step's refusal. A code from that authenticator cannot work and
#: password alone never yields a session, so a recovery code is the way on --
#: and after it, setting up a new authenticator.
KEY_REPLACED = (
    "this server's secret key has been replaced, so it cannot check a code from your "
    "authenticator. Use one of your recovery codes instead; you will then set up your "
    "authenticator again."
)

#: The same, to a member already signed in, at a step-up (`stepup.grant`).
#: Not the sentence above: a step-up never takes a recovery code, so "use one
#: of your recovery codes" sent the member to the one thing that would be
#: refused again with the same words. The way on from here is the profile.
KEY_REPLACED_STEP_UP = (
    "this server's secret key has been replaced, so it cannot check a code from your "
    "authenticator, and a recovery code does not stand in for one here. Set up a new "
    "authenticator from your account first; a code from it will work here."
)

#: The same, at a re-enrolment handed six digits as the current factor
#: (`profile._prove_current_factor`). There a recovery code *is* taken, and
#: setting up the new authenticator is what the member is already doing.
KEY_REPLACED_REENROL = (
    "this server's secret key has been replaced, so it cannot check a code from your "
    "current authenticator. Give one of your recovery codes in its place."
)


@dataclass(slots=True)
class SignInResult:
    user: User
    #: Set once the sign-in is complete.
    session_value: str | None = None
    device_value: str | None = None
    #: True when the browser still has to present a code.
    needs_code: bool = False


def find_user(session: Session, email: str) -> User | None:
    try:
        canonical = email_canonical.canonical(email)
    except Exception:
        return None
    return session.execute(
        select(User).where(User.email_canonical == canonical, User.disabled_at.is_(None))
    ).scalar_one_or_none()


def check_password(
    session: Session,
    engine,
    *,
    email: str,
    password: str,
    ip: str | None,
    device_cookie: str | None = None,
) -> User:
    """Step one. Identical answer and identical work whether or not the user exists.

    A browser holding a live trusted-device record for the named account has
    already proved the second factor, so it is not the guesser the lockout is
    for: it is judged against `ratelimit.FREE_ATTEMPTS_TRUSTED` instead of the
    three ordinary arms (#203). Its failures are still recorded. The trust
    lookup runs whether or not the account exists, against a placeholder id when
    it does not, so the time taken says nothing either way.
    """
    try:
        canonical = email_canonical.canonical(email)
    except Exception:
        # Still counted against *something*, so an address that will not fold
        # cannot dodge the per-account arm -- but never longer than the column,
        # whatever the driver would silently accept. `SignIn` bounds the field
        # already; this is the second line for a caller that is not the router.
        canonical = (email or "").strip().lower()[: email_canonical.MAX_LENGTH]

    user = find_user(session, email)
    trusted = devices.holds_live_trust(session, user.id if user else "-", device_cookie)
    held = ratelimit.reserve(engine, email_canonical=canonical, ip=ip, trusted=trusted)

    # No user and a NULL hash -- a password a reset link cleared (#284) -- are
    # both checked against the dummy: the same work and the same sentence.
    if not passwords.verify_password(user.password_hash if user else None, password):
        raise Unauthorized(_REFUSAL)

    assert user is not None
    ratelimit.release(engine, held)
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
    return user


def refuse_a_replaced_key(
    user: User, *, signed_in: bool = False, sentence: str = KEY_REPLACED
) -> None:
    """Refuse a code for a member the server's key cannot open (#287).

    Before the limiter, like a cleared authenticator: no code can be right, so
    there is nothing to guess and nothing to count. `key_replaced` in the
    answer is what sends the sign-in screen to the recovery code. Nothing is
    written -- the sealed secret stays exactly as it is, so the original key
    put back opens it again.

    `signed_in` for a step-up or a re-enrolment, asked from inside a session:
    the same flag as a `ProofRefused`, so the session survives it, where the
    code would otherwise have reached `open_totp_secret` and come back as a
    422 about a server key that said nothing a screen could act on. Each
    passes its own `sentence`, because what the member can do next differs:
    a recovery code at sign-in and at re-enrolment, a new authenticator
    before a step-up.
    """
    if keycheck.locked_by_key(user):
        refusal = ProofRefused if signed_in else Unauthorized
        raise refusal(sentence, fields={"key_replaced": True})


def check_code(session: Session, engine, user: User, code: str, *, ip: str | None) -> None:
    """Step two, when the browser is not already trusted.

    An account with no authenticator is refused here, before the limiter:
    there is nothing to guess, and the person is owed the reason. Its recovery
    codes went with the secret, so this is the end of the road without the
    link -- a password alone never yields a session.

    So is a member the server's key cannot open (`refuse_a_replaced_key`).
    The router asks that before it spends the half-finished sign-in; this is
    the second line, for any other caller.
    """
    if user.totp_secret is None:
        raise Unauthorized(AUTHENTICATOR_CLEARED)
    refuse_a_replaced_key(user)
    held = ratelimit.reserve(engine, email_canonical=user.email_canonical, ip=ip, kind="totp")
    if not totp.verify_and_consume(user, code):
        raise Unauthorized("that code is not right, or has already been used")
    ratelimit.release(engine, held)


def match_recovery_code(session: Session, user: User, code: str) -> RecoveryCode | None:
    """The unused recovery code this is, if any. Spends nothing.

    Every unused code is checked, so the time taken does not say which one
    matched -- or whether any did.
    """
    candidate = (code or "").strip().replace(" ", "").replace("-", "")
    unused = list(
        session.execute(
            select(RecoveryCode).where(
                RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
            )
        ).scalars()
    )
    matched = None
    for row in unused:
        if passwords.verify_password(row.code_hash, candidate) and matched is None:
            matched = row
    return matched


def redeem_recovery_code(session: Session, engine, user: User, code: str, *, ip: str | None) -> int:
    """Spend one recovery code in place of an authenticator code.

    The wizard has always generated ten of these, hashed them, and made the
    owner confirm they had stored them somewhere safe. Nothing could redeem
    them: with a NOT NULL `totp_secret` and no reset path, a lost phone was a
    permanent lockout whose only cure was hand-editing the database. The admin
    screen meanwhile reported "recovery codes left", which read as a promise the
    API could not keep.

    Redeeming one is deliberately loud. It burns the code, and it revokes every
    session and every trusted browser: if the phone is gone, the assumption that
    those browsers are still yours is exactly the assumption in doubt.

    **And every live agent key** (#210), returned as a count for the sign-in to
    say. A key minted from one of those browsers rests on the same assumption,
    and is the quiet one: it keeps working from wherever it was pasted, for up
    to a year, and nothing tells the person who has just lost their phone to
    look. Changing a password still leaves keys alone, by decision
    (`profile.change_password` reports how many are live instead): that is an
    act the person chose from a browser they hold, not a recovery from a lost
    one.

    Call inside a batch: `agent_keys` is audited, and the revocations are
    recorded in the audit log as part of this act's batch. That batch belongs
    to no household, so a household's History does not list it -- the keys
    screen, which keeps dead keys on purpose, is where they show.
    """
    if user.totp_secret is None:
        # Reset (#284): its codes were deleted with the secret, so say why
        # rather than "that code is not right" ten times over.
        raise Unauthorized(AUTHENTICATOR_CLEARED)
    held = ratelimit.reserve(engine, email_canonical=user.email_canonical, ip=ip, kind="recovery")
    matched = match_recovery_code(session, user, code)

    if matched is None:
        raise Unauthorized("that recovery code is not right, or has already been used")
    ratelimit.release(engine, held)

    matched.used_at = utcnow()
    sessions.revoke_all_for(session, user.id)
    devices.revoke_all_for(session, user.id)
    revoked = 0
    for key in agent_keys.for_user(session, user):
        if key.live():
            agent_keys.revoke(session, key, by=user)
            revoked += 1
    session.flush()
    return revoked


def complete_sign_in(
    session: Session,
    user: User,
    *,
    trust_this_browser: bool,
    device_cookie: str | None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> SignInResult:
    ratelimit.clear_for(session, user.email_canonical)
    result = SignInResult(user=user)
    result.session_value = sessions.issue(session, user, ip=ip, user_agent=user_agent)
    if trust_this_browser:
        value = devices.issue(session, user, label=user_agent)
        # Added to whatever this browser already holds, so a second person
        # signing in here does not evict the first.
        result.device_value = devices.build_cookie(devices.parse_cookie(device_cookie), value)
    return result
