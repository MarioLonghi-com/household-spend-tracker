"""One-time links that give an account a new password, a new authenticator, or
both (#284).

Modelled on invitations, and for the same reason: whoever issues the link never
holds the credentials it leads to. The person following it chooses their own
password, enrols their own authenticator and keeps their own recovery codes.

**The account is shut when the link is issued, not when it is followed.** An
owner reaches for this because an account may be in the wrong hands, or
because its holder has lost the way in; in both cases everything the old
credentials bought has to end now, not whenever somebody gets round to the
link. So `issue` ends every session, trusted browser and live agent key, and
clears the password, the authenticator, or both. Until the link is
followed the account cannot be signed into at all -- and withdrawing the link
leaves it that way, on purpose: the next step is a new link, not a quiet
return to credentials that were reset for a reason.

Every function here that writes expects the caller to have opened a batch, as
`invitations.create` does: `account_resets`, `users`, `recovery_codes` and
`agent_keys` are all audited.

**A link is spent by the row count of its DELETE, never by having read it.**
`lookup` is a plain SELECT, and pysqlite begins a transaction only at the first
write, so two requests can both find the same row. Whichever one's DELETE then
matches nothing has been overtaken -- by a redemption, a withdrawal or a
replacement -- and is refused, and everything it wrote goes with the rollback.
See `_delete` and `AccountReset.__mapper_args__`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from ..auth import crypto, passwords, tokens, totp
from ..errors import Conflict, NotFound, ValidationError
from ..models import AccountReset, Role, User, utcnow
from . import profile

#: As long as an invitation. The person has to be told, find a moment and
#: their phone; a link that dies overnight mostly produces a second link.
VALID_HOURS = 72

#: How long a lapsed link is kept past `expires_at`, so the screen that issued
#: it can say "that link expired" rather than showing nothing for an account
#: that still cannot be signed into. A used or withdrawn link is deleted at
#: once; only lapsed ones wait. Swept by `auth/housekeeping.py`, which is the
#: only place a retention window counts.
RETENTION = timedelta(days=30)

#: One sentence for every way a link can be no good, exactly as invitations
#: do it: telling a spent link from an invented one hands out information to
#: somebody holding neither.
_NOT_VALID = "that reset link is not valid"

#: To an owner who withdrew or replaced a link that was followed, or replaced,
#: a moment before. Not `_NOT_VALID`: an owner is entitled to know the link was
#: used, and is the person who most needs to -- they may have been withdrawing
#: it because they thought it had leaked.
_OVERTAKEN = (
    "that reset link was used or replaced a moment ago. "
    "Look at the account again before doing anything else to it"
)


def _delete(session: Session, reset: AccountReset, *, refusal: Exception) -> None:
    """Delete a link and flush, or raise ``refusal`` if it was already gone.

    The flush is where the row count is checked (`AccountReset.__mapper_args__`),
    so a request that read the row before somebody else deleted it learns so
    here, rather than carrying on as though it had spent it. Whatever else is
    in the same flush is rolled back with it by the batch the caller opened.
    """
    session.delete(reset)
    try:
        session.flush()
    except StaleDataError:
        raise refusal from None


def issue(
    session: Session,
    user: User,
    *,
    password: bool,
    authenticator: bool,
    by: User | None,
) -> tuple[AccountReset, str]:
    """Shut the account and return the link's row and its token, shown once.

    ``by`` is the owner issuing it, or None when it comes from the server --
    whoever can run a command beside the ledger, who is not anybody's account.

    Whatever is already reset stays reset, and the new link covers it: a
    cleared password is still cleared and a cleared authenticator still
    cleared, so a link that set only the other would lead to an account with
    no way in. Read from the account itself -- both are NULL until a link sets
    them -- not from the pending link, which a withdrawal or the sweep may
    already have taken away.

    A pending link for the same account is **replaced**. If it is followed
    between being read here and being deleted, this is refused with a
    Conflict and changes nothing.
    """
    if not (password or authenticator):
        raise ValidationError("choose what to reset: the password, the authenticator, or both")
    if by is not None and by.role is not Role.owner:
        raise ValidationError("only an owner can reset an account")

    previous = session.execute(
        select(AccountReset).where(AccountReset.user_id == user.id)
    ).scalar_one_or_none()
    if previous is not None:
        # Before the insert: the unit of work orders inserts ahead of deletes,
        # and `user_id` is unique. Refused if the link was followed meanwhile:
        # the owner replacing it has to see the account as it now is.
        _delete(session, previous, refusal=Conflict(_OVERTAKEN))
    password = password or user.password_hash is None
    authenticator = authenticator or user.totp_secret is None

    profile.shut_every_door(session, user)
    if password:
        # NULL: nobody knows it. `check_password` verifies against the dummy
        # hash, so it fails exactly as an unknown email does, with the same
        # work and the same sentence -- and unlike a hash of a secret nobody
        # was told, the account still says so once this link is gone.
        user.password_hash = None
    if authenticator:
        user.totp_secret = None
        user.totp_last_counter = None
        profile.delete_recovery_codes(session, user)

    value, digest = tokens.issue()
    reset = AccountReset(
        user_id=user.id,
        token_hash=digest,
        password=password,
        authenticator=authenticator,
        created_by_id=by.id if by is not None else None,
        expires_at=utcnow() + timedelta(hours=VALID_HOURS),
    )
    session.add(reset)
    session.flush()
    return reset, value


def issue_by_owner(
    session: Session,
    user: User,
    *,
    by: User,
    password: bool,
    authenticator: bool,
) -> tuple[AccountReset, str]:
    """`issue`, from the owner's screen (#286).

    Any account, another owner's included, with no step-up: decided in #283
    (decision 3), with visibility as the safeguard -- the link page names who
    reset the account, and every owner sees it in `sign_in_changes.recent`.

    Two refusals, both before the account is touched:

    - **Your own account.** It would end the session you are using and leave
      you holding the only link into it. The profile screen changes your
      password and your authenticator with proof that you hold them.
    - **A disabled account.** `lookup` refuses a link for one, so the owner
      would be handed a link that cannot be followed. Re-enable it first.
    """
    if user.id == by.id:
        raise Conflict(
            "you cannot reset your own account here: change your password or your "
            "authenticator from your profile"
        )
    if user.disabled_at is not None:
        raise Conflict(
            "that account is disabled, so a reset link for it could not be followed. "
            "Re-enable it first."
        )
    return issue(session, user, password=password, authenticator=authenticator, by=by)


def get(session: Session, reset_id: str) -> AccountReset:
    """A pending link by its id, for the owner's screen to withdraw."""
    reset = session.get(AccountReset, reset_id)
    if reset is None:
        raise NotFound("no such reset link")
    return reset


def lookup(session: Session, token: str) -> AccountReset:
    """The reset behind a link, if it is still good."""
    reset = session.execute(
        select(AccountReset).where(AccountReset.token_hash == tokens.fingerprint(token or ""))
    ).scalar_one_or_none()
    if reset is None or reset.expires_at <= utcnow():
        raise NotFound(_NOT_VALID)
    user = session.get(User, reset.user_id)
    if user is None or user.disabled_at is not None:
        raise NotFound(_NOT_VALID)
    return reset


def redeem(
    session: Session,
    reset: AccountReset,
    *,
    new_password: str | None,
    totp_secret: str | None,
    totp_code: str | None,
) -> list[str]:
    """Set what the link was for, spend it, and return the new recovery codes.

    The password is held to the rule every other password is. A new
    authenticator is stored only once a code from it has matched -- the same
    proof the wizard and re-enrolment demand, so nobody leaves with a secret
    their phone never paired with -- and that code's step is burned.

    Ten new recovery codes when the authenticator was reset: the old ones
    were deleted with it. None when only the password was: the codes the
    person already holds still stand beside the authenticator they still have.

    Nothing here signs anybody in. A session needs a password *and* a second
    factor, and the person following a link has proved at most one of them
    through it; they sign in as anyone does once it is done.
    """
    user = session.get(User, reset.user_id)
    if user is None:
        raise NotFound(_NOT_VALID)

    if reset.password:
        if not new_password:
            raise ValidationError("choose a new password")
        problems = passwords.complaints(new_password, email=user.email)
        if problems:
            raise ValidationError("that password will not do: " + ", and ".join(problems))
    elif new_password:
        raise ValidationError("this link does not change the password")

    step: int | None = None
    if reset.authenticator:
        if not totp_secret:
            raise ValidationError("enrol a new authenticator first")
        step = totp.code_matches(totp_secret, totp_code or "")
        if step is None:
            raise ValidationError("that code is not right. Check the time on your phone and try again.")
    elif totp_secret:
        raise ValidationError("this link does not change the authenticator")

    codes: list[str] = []
    if reset.password:
        user.password_hash = passwords.hash_password(new_password)
    if reset.authenticator:
        user.totp_secret = crypto.seal_totp_secret(totp_secret, user_id=user.id)
        user.totp_last_counter = step
        codes = list(profile.replace_recovery_codes(session, user))
    # One flush for the credentials and the spend, after every hash is made,
    # so SQLite's one write lock is held for the writes and not the hashing.
    # The DELETE's row count is the verdict: a second request that looked the
    # link up before this one spent it is refused here, and the password,
    # secret and recovery codes it set are rolled back with the batch -- not
    # left beside the first person's as the last word (#284).
    _delete(session, reset, refusal=NotFound(_NOT_VALID))
    return codes


def withdraw(session: Session, reset: AccountReset, *, by: User | None) -> None:
    """Delete a link that has not been used.

    The account stays as `issue` left it -- shut. Withdrawing is "this link
    should not be followed", not "never mind": the way back in is a new link.

    A Conflict, not a quiet success, when the link was followed or replaced
    after the caller read it: an owner withdrawing a link they think leaked
    must not be told it is withdrawn when somebody has just used it.
    """
    if by is not None and by.role is not Role.owner:
        raise ValidationError("only an owner can withdraw a reset link")
    _delete(session, reset, refusal=Conflict(_OVERTAKEN))


def pending(session: Session) -> list[AccountReset]:
    """Every link not yet used, withdrawn or swept, newest first.

    Lapsed ones included, for `RETENTION`: an account whose link expired still
    cannot be signed into, and the list is where an owner finds that out. Tell
    them apart with `AccountReset.expires_at`.
    """
    return list(session.execute(select(AccountReset).order_by(AccountReset.created_at.desc())).scalars())


def sweep(session: Session, *, now: datetime | None = None) -> list[AccountReset]:
    """Links that lapsed more than `RETENTION` ago.

    Returned rather than deleted: `account_resets` is audited, so the caller
    deletes them through the ORM inside a batch -- see
    `housekeeping._sweep_account_resets`.
    """
    cutoff = (now or utcnow()) - RETENTION
    return list(session.execute(select(AccountReset).where(AccountReset.expires_at <= cutoff)).scalars())


def link_for(token: str, *, base: str) -> str:
    return f"{base.rstrip('/')}/reset/{token}"
