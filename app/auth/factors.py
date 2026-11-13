"""Which second factors an account has, on the web edition (#456).

An account is secured by its password **and at least one second factor**: an
authenticator, one or more passkeys, or both. Either does every job the other
does. The desktop editions have no second factor at all
(`capabilities.second_factor`), and nothing here applies to them.

Three states the code used to tell apart by `users.totp_secret IS NULL`
alone, which stopped being enough once a passkey could stand in for the
authenticator:

* **Has an authenticator.** `totp_secret` is set. Whether this server's key
  can open it is a separate question (`keycheck.locked_by_key`, #287).
* **Passkey only, by choice.** No authenticator, and at least one passkey.
  An ordinary account: it signs in with the password and a passkey, passes a
  step-up the same way, and is never asked for a code.
* **Cleared by a reset** (#284). No authenticator *and no passkey at all*.
  A reset that clears the second factors removes every passkey at issue
  (`profile.shut_every_door`) and the authenticator with it, and removing the
  last second factor any other way is refused (`refuse_removing_last`), so
  this is the one way an account reaches it. Every way in stays shut, exactly
  as before: the code step refuses it, its recovery codes were deleted, and a
  password alone never yields a session.

Derived on every ask from the rows themselves, never stored: a flag beside
them would be one more thing a reset, a withdrawal or the sweep could leave
saying the wrong thing. A passkey registered for another host name counts
towards "not cleared" -- a reset removes those too -- but not towards what the
member can use here (`usable`).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import capabilities, config
from ..errors import Conflict
from ..models import Passkey, User
from . import keycheck

#: Said when the act would leave the account with no second factor. One
#: sentence, the same for the authenticator and for the last passkey.
LAST_ONE = (
    "an account needs a passkey or an authenticator as its second step, and this is "
    "the last one. Add the other first."
)


@dataclass(frozen=True, slots=True)
class SecondFactors:
    #: An authenticator is enrolled (it may still be one the key cannot open).
    authenticator: bool
    #: The authenticator is enrolled and this server's key cannot open it (#287).
    locked_by_key: bool
    #: Passkeys that work on this instance.
    passkeys: int
    #: No authenticator and no passkey anywhere: a reset cleared them (#284).
    cleared: bool

    @property
    def usable(self) -> int:
        """Second factors this member can pass here, now."""
        return (1 if self.authenticator and not self.locked_by_key else 0) + self.passkeys


def _count(session: Session, user: User, *, here_only: bool) -> int:
    query = select(func.count()).select_from(Passkey).where(Passkey.user_id == user.id)
    if here_only:
        query = query.where(Passkey.rp_id == config.settings.rp_id)
    return session.execute(query).scalar_one()


def of(session: Session, user: User) -> SecondFactors:
    authenticator = user.totp_secret is not None
    anywhere = 0 if authenticator else _count(session, user, here_only=False)
    return SecondFactors(
        authenticator=authenticator,
        locked_by_key=keycheck.locked_by_key(user),
        passkeys=_count(session, user, here_only=True),
        cleared=capabilities.second_factor() and not authenticator and anywhere == 0,
    )


def cleared(session: Session, user: User) -> bool:
    """A reset cleared every second factor and none has been enrolled since."""
    if not capabilities.second_factor() or user.totp_secret is not None:
        return False
    return _count(session, user, here_only=False) == 0


def passkey_only(session: Session, user: User) -> bool:
    """No authenticator, by choice: a passkey stands in for it."""
    return (
        capabilities.second_factor()
        and user.totp_secret is None
        and _count(session, user, here_only=False) > 0
    )


def refuse_removing_last(session: Session, user: User, *, removing: Passkey | None = None) -> None:
    """Refuse an act that would leave no second factor this member can pass.

    ``removing`` is the passkey about to go; None means the authenticator.
    Counted as what works here and now: an authenticator the key cannot open
    (#287) and a passkey for another host name do not keep anybody's account
    reachable. A passkey for another host may itself always be removed -- it
    is not what is keeping the account reachable here.
    """
    if not capabilities.second_factor():
        return
    found = of(session, user)
    if removing is None:
        left = found.passkeys
    else:
        if removing.rp_id != config.settings.rp_id:
            return
        left = found.usable - 1
    if left < 1:
        raise Conflict(LAST_ONE, code="profile.last_second_factor")
