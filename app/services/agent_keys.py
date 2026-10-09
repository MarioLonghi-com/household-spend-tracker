"""Credentials a member issues to a program acting on their behalf.

The shape is the one this app already uses four times over -- an opaque 256-bit
value, stored as its SHA-256, verified by the lookup itself. What is different
is the lifetime: a session dies when the browser goes away and a trusted device
expires in thirty days, but a key is a thing somebody pastes into a config file
and forgets. Everything here is arranged around that.

* **Issuing needs both factors, now.** `auth/stepup.py` explains why the bar is
  higher than the rest of the profile screen's.
* **Expiry is mandatory and does not slide.** A key that renewed itself on use
  is a key that lives forever, which is the whole failure.
* **Revocation is a row read away**, so it is instant. That is the property
  `Access Control Investigation` turned JWTs down for.
* **Scopes are fixed at issue.** Widening one means issuing a new key, which
  costs a minute and removes a class of "who changed this, and when".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import tokens
from ..errors import Forbidden, NotFound, ValidationError
from ..models import AgentKey, AgentScope, Household, HouseholdMember, User, utcnow

#: What the token looks like on the wire. The prefix is not decoration: a fixed,
#: unique one is what makes a leaked key greppable in a log, matchable by
#: secret scanning, and scrubbable by a log filter.
PREFIX = "stk_"

#: Ninety days by default, a year at the most. Long enough that a key is not a
#: chore; short enough that one pasted into a script and forgotten stops
#: working while somebody still remembers what it was for.
DEFAULT_DAYS = 90
MAX_DAYS = 365

#: How stale `last_used_at` is allowed to get before a read writes it. The same
#: arithmetic as `sessions.touch`, for the same reason: a polling agent should
#: not turn every GET into a WAL write and an fsync.
TOUCH_SECONDS = 60

#: A dead key -- revoked or simply lapsed -- is kept this long so History can
#: still say what it did, and so the keys screen can answer "what did I give
#: out, and when did it stop working". Then it is swept. `Batch.agent_key_id`
#: is SET NULL and the name is denormalised into `Batch.source`, so the
#: sentence survives the row either way.
#:
#: It covers both arms deliberately. Expiry used to purge at `expires_at`
#: exactly, which gave a lapsed key roughly six hours -- one housekeeping
#: interval -- before it vanished from the list `for_user` promises not to drop
#: entries from. Nothing about reaching `expires_at` makes the question that
#: list answers less worth answering than revoking does, and an expired key is
#: already inert: `lookup` refuses it long before the sweep is involved.
KEEP_DEAD = timedelta(days=30)


def issue(
    session: Session,
    *,
    user: User,
    household: Household,
    label: str,
    agent_name: str | None = None,
    scope: AgentScope | str = AgentScope.read,
    may_commit: bool = False,
    days: int = DEFAULT_DAYS,
) -> tuple[AgentKey, str]:
    """Mint a key. Returns the row and the token, which is shown exactly once.

    Call inside a batch: `agent_keys` is audited, so issuing one is an act that
    belongs in History like any other.
    """
    name = (label or "").strip()
    if not name:
        raise ValidationError(
            "give the key a label, so you know what it is for", code="agent_key.needs_label"
        )
    if not 1 <= days <= MAX_DAYS:
        raise ValidationError(
            f"a key can last between a day and {MAX_DAYS} days",
            code="agent_key.lifetime",
            params={"max_days": MAX_DAYS},
        )

    member = session.execute(
        select(HouseholdMember).where(
            HouseholdMember.household_id == household.id,
            HouseholdMember.user_id == user.id,
        )
    ).scalar_one_or_none()
    if member is None:
        # 404, not 403, like everything else keyed on a household: a key cannot
        # be issued into a household its owner is not in, and refusing in a way
        # that confirms the id is real would be the one place this app leaks it.
        raise NotFound("no such household", code="household.not_found")

    chosen = AgentScope(scope)
    if may_commit and not chosen.may_write:
        # A read-only key that may commit an import is a contradiction with a
        # checkbox. Refused here rather than ignored, because ignoring it would
        # store a flag that reads as true and behaves as false.
        raise ValidationError("a read-only key has nothing to commit", code="agent_key.read_only_commit")

    value, digest = tokens.issue()
    # One reading of the clock for both columns, like `sessions.issue`. Letting
    # `created_at` come from the ORM default means it is stamped at flush, a
    # moment after `expires_at` was computed -- so a ninety-day key measures
    # eighty-nine days and some hours, and any arithmetic on the pair is off by
    # a hair for no reason anybody would ever guess.
    now = utcnow()
    key = AgentKey(
        token_hash=digest,
        label=name[:80],
        agent_name=(agent_name or "").strip()[:80] or None,
        user_id=user.id,
        household_id=household.id,
        scope=chosen,
        may_commit=may_commit,
        created_at=now,
        expires_at=now + timedelta(days=days),
    )
    session.add(key)
    session.flush()
    return key, PREFIX + value


def lookup(session: Session, presented: str | None, *, now: datetime | None = None) -> AgentKey | None:
    """The key behind a token, if it is still good. None in every other case.

    Deliberately one answer for absent, malformed, unknown, revoked, expired,
    owner-disabled and owner-evicted alike: the difference between them is only
    ever useful to somebody guessing, and the caller turns all of them into one
    401.
    """
    if not presented or not presented.startswith(PREFIX):
        return None
    now = now or utcnow()
    key = session.execute(
        select(AgentKey).where(
            AgentKey.token_hash == tokens.fingerprint(presented[len(PREFIX) :])
        )
    ).scalar_one_or_none()
    if key is None:
        return None
    if key.revoked_at is not None or key.expires_at <= now:
        return None
    # A disabled member's keys die with them, exactly as `deps.current_user`
    # refuses a session belonging to one.
    owner = session.get(User, key.user_id)
    if owner is None or owner.disabled_at is not None:
        return None
    # And so do the keys of somebody who is no longer in the household the key
    # is scoped to. Membership was checked once, in `issue`, and eviction used
    # to leave the programmatic door open for the rest of the key's life --
    # up to a year -- while the browser lost access the instant the row went.
    # Checked here rather than revoked by `remove_member` because a revocation
    # can be forgotten at a call site and this cannot: it is the same lookup
    # every request already makes.
    membership = session.execute(
        select(HouseholdMember.id).where(
            HouseholdMember.household_id == key.household_id,
            HouseholdMember.user_id == key.user_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        return None
    return key


def touch(session: Session, key: AgentKey, *, now: datetime | None = None) -> None:
    """Record that a key is in use, sparingly and without a batch.

    This is the write `__audit_redact__` exists to make possible. `last_used_at`
    is omitted from both change images, so the hook sees `before == after`,
    writes no change row and demands no open batch -- which is the only way a
    GET can record the use of the credential that authorised it.
    """
    now = now or utcnow()
    if key.last_used_at is not None and (now - key.last_used_at).total_seconds() < TOUCH_SECONDS:
        return
    key.last_used_at = now


def revoke(session: Session, key: AgentKey, *, by: User, now: datetime | None = None) -> AgentKey:
    """Stop a key working. The next request it makes is a 401.

    Call inside a batch. Only the person whose authority the key borrows can do
    this -- an owner revoking somebody else's key is a different act, and it is
    not one this screen offers.
    """
    if key.user_id != by.id:
        raise Forbidden(
            "only the person who issued a key can revoke it", code="agent_key.not_issuer"
        )
    if key.revoked_at is None:
        key.revoked_at = now or utcnow()
    return key


def for_user(session: Session, user: User) -> list[AgentKey]:
    """Every key this person has, newest first, including dead ones.

    Revoked and expired keys are shown rather than hidden: "what did I give out
    and when did it stop working" is the question this list exists to answer,
    and a list that silently drops entries answers it wrongly.
    """
    return list(
        session.execute(
            select(AgentKey)
            .where(AgentKey.user_id == user.id)
            .order_by(AgentKey.created_at.desc())
        ).scalars()
    )


def sweep(session: Session, *, now: datetime | None = None) -> list[AgentKey]:
    """Keys long past expiry, and keys revoked long enough ago. Returns them.

    > [!danger] This is the one place in housekeeping that cannot be a bulk
    > statement.

    Every other sweep in `auth/housekeeping.py` is a `delete()` carrying an
    `# audit-exempt:` comment, because every other table there is
    `__audit__ = False`. **This table is audited.** A Core DELETE bypasses ORM
    events, so the audit log would never see it -- and the `before_flush` hook
    raises `NoOpenBatch` if a write reaches an audited table with no batch open,
    so copying the pattern sitting directly above it fails loudly rather than
    quietly. Loud is better, but neither is shipping.

    So: this returns the rows, and the caller deletes them through the ORM
    inside a `BatchKind.admin` batch with a real actor.
    """
    now = now or utcnow()
    dead_by = now - KEEP_DEAD
    return list(
        session.execute(
            select(AgentKey).where(
                (AgentKey.expires_at <= dead_by)
                | (AgentKey.revoked_at.is_not(None) & (AgentKey.revoked_at <= dead_by))
            )
        ).scalars()
    )
