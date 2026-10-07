"""Proving, again and just now, that the person at the keyboard is the owner.

A session cookie proves this browser was signed in at some point. For most of
what the app does that is the right bar, and `profile.py` explains why the two
acts it owns ask for the current password on top of it.

Minting an agent key needs a higher one, for a reason neither of those has: the
credential it produces **outlives the session that made it**. Changing a
password ends every other session; re-enrolling an authenticator revokes every
trusted device. Both are loud, and a person who got their laptop back finds out.
A key is quiet -- it keeps working from somewhere else, for ninety days, and
nothing about the browser it was minted from says so afterwards.

So the bar is the sign-in bar: the password *and* a live authenticator code,
both checked now. What that buys is a `step_up_grants` row, good for one act
and five minutes, which the issuing route spends.

Every act whose effect outlives the session pays it, and there is more than
one. Each calls `require` before it writes or reads anything:

* `POST /me/keys` -- an agent key, for the reason above.
* `POST /admin/application/backups/{name}/download` with ``include_key`` -- the
  zip then carries every password hash and the key that opens every member's
  authenticator, portable and forever (#204).
* `POST /me/passkeys/options` -- a passkey is a way in that outlives this
  session, and on its own it is both factors (#120).
* `POST /admin/invitations` with ``role: owner``, and
  `POST /admin/users/{id}/role` making somebody an owner -- an owner account
  survives every reset the inviting owner can make to their own password,
  authenticator or recovery codes (#205).

Two properties are load-bearing and each has a test:

* **Single use.** The row is deleted when it is claimed, whether or not the act
  that follows succeeds -- exactly as `sessions.claim_pending` spends a pending
  sign-in. A grant is worth one key, never two.
* **Non-sliding.** `expires_at` is fixed at issue. A grant that renewed itself
  on inspection would be a session with extra privileges rather than a proof of
  a single moment, which is the same mistake `trusted_devices.expires_at` has a
  comment about.

The TOTP code is consumed through `totp.verify_and_consume`, so it is dead
afterwards -- a step-up and a sign-in cannot share one code. That is the
intended cost and it is the same one `check_code` already imposes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..errors import ProofRefused
from ..models import StepUpGrant, User, utcnow
from . import passwords, ratelimit, service, tokens, totp

#: Long enough to read the form and press the button, and no longer. The
#: fifteen minutes `profile.REENROL_SECONDS` allows are for installing an
#: authenticator app and scanning a QR; nothing here takes that.
GRANT_SECONDS = 5 * 60

#: Its own rate-limit counter, beside "password", "totp" and "recovery". A
#: grant attempt is not a sign-in attempt: locking someone out of the app
#: because they fumbled a code on the keys screen would be the wrong blast
#: radius, and letting key-minting borrow the sign-in budget would be the wrong
#: one in the other direction.
KIND = "stepup"

#: One sentence for every way this can fail, for the reason `service._REFUSAL`
#: gives: which of the two was wrong is only ever useful to someone guessing.
_REFUSAL = "that password and code do not match"


def grant(
    session: Session,
    engine: Engine,
    user: User,
    *,
    password: str,
    code: str,
    ip: str | None = None,
) -> tuple[str, datetime]:
    """Check both factors and return ``(value, expires_at)``.

    Only the value's hash is stored. The caller hands the value back on the act
    it is paying for.

    A member the server's key cannot open (#287) is refused first, with
    `key_replaced` and a sentence of its own, so the screen can say why: no
    code from that authenticator can be checked, a recovery code is not taken
    here either, and a new authenticator is set up from the profile. Not the
    sign-in's sentence, which says to use a recovery code: whatever is typed
    here, recovery code included, gets this same refusal. Before the limiter
    and before the password, so nothing is counted -- and nothing about the
    password is said, which checking it first would have done (a right one
    would be the only way to reach this refusal). The member learns only that
    they are in recovery mode, which `GET /me/authenticator` tells them anyway.
    """
    service.refuse_a_replaced_key(
        user, signed_in=True, sentence=service.KEY_REPLACED_STEP_UP
    )
    held = ratelimit.reserve(engine, email_canonical=user.email_canonical, ip=ip, kind=KIND)

    # Both factors are checked before either failure is reported, so the
    # refusal cannot be used to learn which one was wrong -- and the password is
    # checked first because `verify_and_consume` burns a code, and burning one
    # for a caller who does not even have the password is a free way to make
    # somebody's authenticator useless for thirty seconds at a time.
    if not passwords.verify_password(user.password_hash, password):
        raise ProofRefused(_REFUSAL)
    if not totp.verify_and_consume(user, code):
        raise ProofRefused(_REFUSAL)
    ratelimit.release(engine, held)

    # An earlier grant for this user is dropped, like `issue_pending` does. Two
    # live grants means one act paid for twice, and the second one is always
    # the one somebody forgot about.
    now = utcnow()
    for old in session.execute(
        select(StepUpGrant).where(StepUpGrant.user_id == user.id)
    ).scalars():
        session.delete(old)

    value, id_hash = tokens.issue()
    expires_at = now + timedelta(seconds=GRANT_SECONDS)
    session.add(
        StepUpGrant(id_hash=id_hash, user_id=user.id, created_at=now, expires_at=expires_at)
    )
    session.flush()
    return value, expires_at


def claim(engine: Engine, value: str | None, *, user_id: str, now: datetime | None = None) -> bool:
    """Spend a grant. True only if it was this user's and still live.

    On its own session and its own transaction, for the reason
    `sessions.claim_pending` records: the spend has to be committed before the
    rest of the request runs, or SQLite holds a write lock that the act itself
    then blocks on.

    The row goes either way. A grant offered to the wrong user, or presented
    past its window, is still spent -- there is nothing to be gained by keeping
    a credential somebody has already shown they hold.

    One DELETE ... RETURNING, so the row count is the verdict (#206): a read
    followed by a delete let two concurrent requests both find the grant and
    both be told yes -- one grant, two keys. See `sessions.claim_pending`.
    """
    if not value:
        return False
    table = StepUpGrant.__table__
    with engine.begin() as own:
        row = own.execute(  # audit-exempt: step-up grants are not audited
            delete(table)
            .where(table.c.id_hash == tokens.fingerprint(value))
            .returning(table.c.user_id, table.c.expires_at)
        ).first()
    if row is None:
        return False
    owner_id, expires_at = row
    return owner_id == user_id and expires_at > (now or utcnow())


def require(engine: Engine, value: str | None, *, user_id: str) -> None:
    """Spend a grant or refuse the act. The shape every caller wants."""
    if not claim(engine, value, user_id=user_id):
        raise ProofRefused("confirm your password and authenticator code first")


def drop_for(session: Session, user_id: str) -> int:
    """Every unspent grant this user holds. What a password change does.

    A grant was bought with the old password; changing it because somebody else
    knows it has to take away what they already bought with it.
    """
    rows = list(
        session.execute(select(StepUpGrant).where(StepUpGrant.user_id == user_id)).scalars()
    )
    for row in rows:
        session.delete(row)
    return len(rows)


def sweep(session: Session, *, now: datetime | None = None) -> int:
    """Grants nobody came back to spend. Called by `housekeeping.sweep`.

    A bulk statement, which is allowed here and nowhere near an audited table:
    `step_up_grants` is `__audit__ = False`, so there is nothing for the hook
    to miss. The comment is what the CI grep reads.
    """
    return (
        session.execute(  # audit-exempt: step-up grants are not audited
            delete(StepUpGrant.__table__).where(
                StepUpGrant.__table__.c.expires_at <= (now or utcnow())
            )
        ).rowcount
        or 0
    )
