"""Reset links and new owners, read back out of the audit log (#286).

An owner can reset any account on the instance, another owner's included, and
it takes no step-up (#283, decision 3): somebody holding one owner's session
can lock the other out. The accepted safeguard is that it cannot be done
quietly. The person reset is told on the link page who did it, and every
owner is told here, for the last `WINDOW`: each reset link issued, each
account that became an owner, and what the server did to an account's way in.

**Read from `changes`, never stored a second time.** The audit log already
holds every one of these as a row image, written in the same transaction as
the act. A table of notices beside it would be a second record to keep in
step with the first, and the one that could be got wrong or quietly emptied.
This is a query over the log and nothing else.

What counts:

- **reset**: an insert into `account_resets`. A link that replaced an earlier
  one is a delete and an insert in one batch, and counts once, as the insert,
  with the switches the new link carries.
- **owner_added**: an insert into `users` with the role owner -- the first
  owner's wizard, an owner-role invitation accepted, the server making one.
- **promoted**: an update of `users` whose role went from anything else to
  owner. An undo that puts an owner back counts too, which is right:
  somebody became an owner.
- **reenabled**: an update of `users` whose `disabled_at` went from a time to
  NULL, **from the server** only. An owner re-enabling somebody on the Admin
  screen is an ordinary act on a screen every owner can see; the server
  doing it to an account an owner had shut is the thing to be told.
- **authenticator_replaced**: a batch of `scripts.reset_authenticator`, which
  gives a member a new authenticator directly, with no link. Its row images
  cannot say so -- `totp_secret` is redacted from them -- so the batch's own
  mark is what is read.

One change can say two things -- the server promoting a disabled account and
re-enabling it in one write -- and is then two items with one `id`; `key`
tells them apart.

Housekeeping's batches (`source["housekeeping"]`) never count: they only
sweep what has lapsed, and a sweep is not anybody resetting anything.

**Who did it.** An owner's act is a batch whose actor is that owner. Two
exceptions read differently:

- *From the server.* A command run beside the ledger is nobody's account, but
  `batches.actor_id` is NOT NULL and there is no system account, so it
  borrows the account it acts on as the actor and marks the batch
  `source["via"] = "scripts.<command>"` -- `scripts.reset_authenticator`, and
  the reset command of #285. That mark is what `from_the_server` reads. A
  reset says it outright as well: `created_by_id` is NULL in its image when
  no owner issued it.
- *An owner-role invitation accepted.* The wizard's batch has the new person
  as its actor, but the owner who chose the role is the one who sent the
  link, and the invitation's own image in the same batch names them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.expression import UnaryExpression
from sqlalchemy.sql.operators import custom_op

from ..models import Batch, Change, ChangeOp, Role, User, utcnow

#: How far back an owner is told. Long enough to cover a holiday; short enough
#: that the notice is about now rather than a history page.
WINDOW = timedelta(days=14)

#: The prefix a server command's batch carries in `source["via"]`.
SERVER_VIA = "scripts."
#: The one server command whose act no row image shows.
REPLACES_AUTHENTICATOR = "scripts.reset_authenticator"

What = Literal["reset", "owner_added", "promoted", "reenabled", "authenticator_replaced"]

_OWNER = Role.owner.value


@dataclass(frozen=True, slots=True)
class SignInChange:
    #: The change's position in the log: newer is larger. Shared by the two
    #: items one change can make.
    id: int
    #: Unique: the position and what was read there.
    key: str
    what: What
    user_id: str
    user_name: str
    #: What was reset. False for everything but a reset, and an
    #: `authenticator_replaced`, which is True for the authenticator.
    password: bool
    authenticator: bool
    #: None when it came from the server.
    by_id: str | None
    by_name: str | None
    from_server: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class _Reading:
    what: What
    user_id: str
    by_id: str | None
    password: bool = False
    authenticator: bool = False


def from_the_server(batch: Batch) -> bool:
    """Whether a batch was opened by a command beside the ledger rather than
    by somebody signed in. See the module docstring."""
    via = (batch.source or {}).get("via")
    return isinstance(via, str) and via.startswith(SERVER_VIA)


def recent(session: Session, *, now: datetime | None = None) -> list[SignInChange]:
    """Everything above from the last `WINDOW`, newest first."""
    since = (now or utcnow()) - WINDOW
    found = _from_row_images(session, since) + _authenticators_replaced(session, since)
    return sorted(found, key=lambda one: (one.id, one.key), reverse=True)


def _unindexed(column):
    """`+column`: the same value, which SQLite will not lead with an index on.

    SQLite's own way to keep the planner off one index without naming another
    ("disqualifying WHERE clause terms using unary-'+'"); SQLAlchemy's SQLite
    dialect renders no `INDEXED BY` hint.
    """
    return UnaryExpression(column, operator=custom_op("+"), type_=column.type)


def _from_row_images(session: Session, since: datetime) -> list[SignInChange]:
    rows = session.execute(
        select(Change, Batch)
        .join(Batch, Batch.id == Change.batch_id)
        .where(
            Batch.started_at >= since,
            # Bounded by the window, whatever the planner knows. Offered
            # `ix_changes_row_history`, SQLite without statistics led with it
            # and read every account change since the ledger began, looking
            # at the date only after the join. With `table_name` kept off any
            # index, the only way in is the window's batches, off
            # `ix_batches_started_at`, and their changes off
            # `ix_changes_batch_id`.
            _unindexed(Change.table_name).in_(("account_resets", "users")),
            Change.op.in_((ChangeOp.insert, ChangeOp.update)),
        )
    ).all()

    found: list[SignInChange] = []
    for change, batch in rows:
        if "housekeeping" in (batch.source or {}):
            continue
        for reading in _read(session, change, batch):
            found.append(
                _item(
                    session,
                    id=change.seq,
                    reading=reading,
                    fallback_name=(change.after or {}).get("display_name"),
                    at=batch.started_at,
                )
            )
    return found


def _read(session: Session, change: Change, batch: Batch) -> list[_Reading]:
    """What one row image says, if it says anything this notice is for."""
    after = change.after or {}
    before = change.before or {}
    server = from_the_server(batch)

    if change.table_name == "account_resets":
        if change.op is not ChangeOp.insert:
            return []
        by_id = after.get("created_by_id")
        return [
            _Reading(
                "reset",
                after.get("user_id") or "",
                None if server else by_id,
                password=bool(after.get("password")),
                authenticator=bool(after.get("authenticator")),
            )
        ]

    if change.op is ChangeOp.insert:
        if after.get("role") != _OWNER:
            return []
        by_id = None if server else (_inviter(session, batch.id) or batch.actor_id)
        return [_Reading("owner_added", change.row_id, by_id)]

    found: list[_Reading] = []
    by_id = None if server else batch.actor_id
    if before.get("role") != _OWNER and after.get("role") == _OWNER:
        found.append(_Reading("promoted", change.row_id, by_id))
    if server and before.get("disabled_at") is not None and after.get("disabled_at") is None:
        found.append(_Reading("reenabled", change.row_id, None))
    return found


def _authenticators_replaced(session: Session, since: datetime) -> list[SignInChange]:
    """`scripts.reset_authenticator` runs, one item per batch.

    Its actor is the member it gave the authenticator to (see the script).
    The item's position is the batch's last change, so it sorts among the
    others by when it happened.
    """
    # A correlated max, not a join grouped by batch: `GROUP BY batches.id`
    # walked the whole of `batches` in id order to group it -- every batch the
    # ledger ever wrote, read on every owner's page load to return the few of
    # the last fortnight. This reads the window off `ix_batches_started_at`,
    # and one probe of `ix_changes_batch_id` per batch in it.
    last = (
        select(func.max(Change.seq))
        .where(Change.batch_id == Batch.id)
        .correlate(Batch)
        .scalar_subquery()
    )
    rows = session.execute(
        select(Batch, last).where(
            Batch.started_at >= since,
            Batch.source["via"].as_string() == REPLACES_AUTHENTICATOR,
        )
    ).all()
    return [
        _item(
            session,
            id=seq,
            reading=_Reading("authenticator_replaced", batch.actor_id, None, authenticator=True),
            at=batch.started_at,
        )
        for batch, seq in rows
        # The join this replaced dropped a batch with no changes; so does this.
        if seq is not None
    ]


def _item(
    session: Session,
    *,
    id: int,
    reading: _Reading,
    at: datetime,
    fallback_name: str | None = None,
) -> SignInChange:
    return SignInChange(
        id=id,
        key=f"{id}:{reading.what}",
        what=reading.what,
        user_id=reading.user_id,
        user_name=_name(session, reading.user_id, fallback=fallback_name),
        password=reading.password,
        authenticator=reading.authenticator,
        by_id=reading.by_id,
        by_name=_name(session, reading.by_id) if reading.by_id else None,
        from_server=reading.by_id is None,
        at=at,
    )


def _inviter(session: Session, batch_id: str) -> str | None:
    """Who sent the invitation accepted in this batch, if one was."""
    image = session.execute(
        select(Change.after).where(
            Change.batch_id == batch_id,
            Change.table_name == "invitations",
            Change.op == ChangeOp.update,
        )
    ).scalars().first()
    return (image or {}).get("invited_by_id")


def _name(session: Session, user_id: str, *, fallback: str | None = None) -> str:
    user = session.get(User, user_id) if user_id else None
    if user is not None:
        return user.display_name
    return fallback or "an account since removed"
