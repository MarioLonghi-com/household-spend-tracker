"""Deleting the rows that have already stopped counting.

Every expiry in the auth schema was enforced **on read** and nowhere else. A
session past `expires_at` does not authenticate anyone, a pending sign-in past
its five minutes cannot be claimed, a trusted device past thirty days does not
skip the code -- and none of those rows was ever deleted. `ratelimit.prune()`
was written to enforce a thirty-day window on `login_attempts`, documented the
window, and had no caller anywhere in the application: the only one in the
repository was a test.

So the instance kept, since first boot, one row per sign-in attempt ever made,
each carrying an email address and an IP. That is the finding this module
closes, and the shape of it is worth keeping: **a retention policy written in a
docstring is not a retention policy.**

All five auth tables here are `__audit__ = False` -- they record what happened
at the door, not what happened to the ledger -- so these are bulk deletes with
nothing for the audit hook to miss, and each says so where the grep can see it.

`receipt_blobs` joins them for the same reason and a different one: it is also
unaudited, and it is also a retention window that would otherwise have no
caller. The receipts themselves are hard-deleted like everything else in the
ledger; what gets swept is only bytes nothing references any more.

`agent_keys` is the exception and the reason this module now has two shapes in
it. It **is** audited, so its sweep cannot be a bulk statement and cannot run
without a batch. It is at the bottom, in `_sweep_agent_keys`, with the whole
argument written out -- because the mistake available here is to read the five
deletes above and copy one. `invitations` is audited too and goes the same way,
in `_sweep_invitations`.

Retention, in one place. Each figure is the constant the sweep uses, and
`tests/test_housekeeping.py` reads this list against them:

- sessions: until the absolute or the idle window ends, whichever is first
- pending sign-ins, trusted devices, step-up grants: until `expires_at`
- login attempts: 30 days (`ratelimit.RETENTION`), each an address as typed
- invitations: 30 days after they stopped being usable -- accepted, withdrawn
  or expired (`invitations.RETENTION`), each the invitee's address (#211)
- account resets: 30 days after they expired (`account_resets.RETENTION`).
  A used or withdrawn one is deleted there and then, so only lapsed links
  wait, and only so an owner can see that one lapsed (#284)
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..audit.batch import batch
from ..audit.guard import AuditedSession
from ..config import settings
from ..models import BatchKind, PendingSignIn, TrustedDevice, WebSession, utcnow
from ..services import account_resets as account_reset_service
from ..services import agent_imports as agent_import_service
from ..services import agent_keys as agent_key_service
from ..services import agent_requests as agent_request_service
from ..services import backup_bundle, importing
from ..services import invitations as invitation_service
from ..services import receipts as receipt_service
from . import ratelimit, stepup


def sweep(engine: Engine) -> dict[str, int]:
    """Delete what has expired. Returns how many rows went, per table.

    Its own session and its own transaction, like `ratelimit.record` and
    `ratelimit.prune`: this runs on a timer with no request behind it, and it
    must not be able to join, extend or roll back somebody else's transaction.
    """
    now = utcnow()
    idle_cutoff = now - timedelta(seconds=settings.session_idle_seconds)
    removed: dict[str, int] = {}

    with Session(engine) as own:
        # Both clocks, matching `sessions.lookup` exactly: a session dies when
        # its absolute window ends *or* when it has not been seen inside the
        # idle window. Deleting on `expires_at` alone would leave a row that
        # stopped authenticating anyone a fortnight ago.
        stale_sessions = delete(WebSession).where(  # audit-exempt: sessions are not audited
            (WebSession.expires_at <= now) | (WebSession.last_seen_at <= idle_cutoff)
        )
        removed["sessions"] = own.execute(stale_sessions).rowcount or 0

        # A half-finished sign-in nobody came back to. `issue_pending` drops the
        # previous one for that user, so these are only ever abandoned by people
        # who never tried again.
        stale_pending = delete(PendingSignIn).where(  # audit-exempt: not audited
            PendingSignIn.expires_at <= now
        )
        removed["pending_sign_ins"] = own.execute(stale_pending).rowcount or 0

        # The thirty days are fixed from issuance and deliberately do not slide,
        # so an expired row is expired for good and there is nothing to keep.
        stale_devices = delete(TrustedDevice).where(  # audit-exempt: not audited
            TrustedDevice.expires_at <= now
        )
        removed["trusted_devices"] = own.execute(stale_devices).rowcount or 0

        # A step-up grant nobody came back to spend. Five minutes, fixed at
        # issue: there is no state here worth keeping past it, and the row is
        # a credential hash, so "expired" and "deletable" are the same instant.
        removed["step_up_grants"] = stepup.sweep(own, now=now)

        # What agent keys asked for, past the thirty days worth keeping. Not
        # audited, so a bulk delete like the four above it.
        removed["agent_requests"] = agent_request_service.sweep(own, now=now)

        # Import previews an agent staged and nobody committed. Also unaudited,
        # and also a window that would otherwise have no caller -- which is the
        # exact shape of the finding at the top of this module.
        #
        # A person's staged import is never swept: they are entitled to come
        # back to it. See `importing.sweep_stale_agent_previews` for why this
        # is the answer to issue #47 rather than giving a key a DELETE.
        removed["staged_agent_imports"] = importing.sweep_stale_agent_previews(own, now=now)

        # What a retry should have been answered with, past the day in which a
        # retry is still a retry. Not audited, so a bulk delete like the rest.
        removed["agent_replays"] = agent_import_service.sweep(own, now=now)

        # Receipt bytes nothing points at any more. Not an auth table, but this
        # is the module with the timer, and a retention window that lives
        # anywhere else is a retention window with no caller -- which is the
        # exact finding this file exists to close.
        #
        # Its own two passes and a twenty-four-hour grace period, because undo
        # replays change images and `receipt_blobs` has none: see
        # `services/receipts.py::sweep_orphan_blobs`.
        removed["receipt_blobs"] = receipt_service.sweep_orphan_blobs(own)

        own.commit()

    removed["login_attempts"] = ratelimit.prune(engine)
    removed["agent_keys"] = _sweep_agent_keys(engine, now=now)
    removed["invitations"] = _sweep_invitations(engine, now=now)
    removed["account_resets"] = _sweep_account_resets(engine, now=now)
    # A backup's download zip the response never got to remove: a full copy of
    # the ledger, and perhaps its key, left in the backups directory by a crash
    # mid-download. Files rather than rows, and here for the same reason as the
    # receipt blobs above -- this is the module with the timer.
    from ..services import platform  # it imports the API package; late, to stay acyclic

    removed["backup_downloads"] = backup_bundle.sweep_leftovers(platform.backup_dir())
    refresh_planner_statistics(engine)
    return removed


#: Rows SQLite samples per index when it gathers statistics. Its own
#: recommended figure for `PRAGMA optimize`: enough to tell a selective index
#: from a useless one, and milliseconds whatever the size of the ledger.
ANALYSIS_LIMIT = 400


def refresh_planner_statistics(engine: Engine) -> None:
    """Give SQLite's query planner statistics to choose indexes with.

    Nothing ever ran `ANALYZE`, so the planner chose between indexes blind --
    and chose wrongly: matching a statement line against the ledger took the
    `(account_id, import_id)` index over `(account_id, date)`, 9.1 ms a query
    against 0.03 ms (issue #100).

    `ANALYZE` under an `analysis_limit`, rather than `PRAGMA optimize`, which
    is the textbook call here and does nothing from this connection: before
    SQLite 3.46 it only analyses tables *the connection running it* has already
    queried, and this is a fresh connection that has queried none. Measured on
    3.45, not assumed. With the limit, `ANALYZE` is exactly what `optimize`
    would have run, bounded to a few hundred rows per index.

    Runs from the sweep's timer, on its own connection and its own
    transaction, for the same reasons the sweeps above do. Statistics are not
    data: nothing here is audited and nothing here can lose a row.
    """
    if engine.dialect.name != "sqlite":
        return
    with engine.connect() as conn:
        conn.exec_driver_sql(f"PRAGMA analysis_limit={ANALYSIS_LIMIT}")
        conn.exec_driver_sql("ANALYZE")
        conn.commit()


def _sweep_agent_keys(engine: Engine, *, now) -> int:
    """Expired and long-revoked keys, deleted **through the ORM**.

    > [!danger] The one sweep in this module that cannot be a bulk statement.

    Every delete above is a Core `delete()` carrying an `# audit-exempt:`
    comment, because every table above is `__audit__ = False`. `agent_keys` is
    audited. A Core DELETE bypasses the ORM events the log listens to, and
    `guard._no_bulk_writes_to_audited_tables` refuses one outright -- so
    copying the pattern sitting a few lines up fails loudly rather than
    quietly. Loud beats quiet; neither ships.

    An `AuditedSession`, not the plain `Session` the rest of this function
    uses: the `before_flush` hook fires on any session, but the bulk and raw-SQL
    guards are what make "no write reaches an audited table outside a batch" a
    promise rather than a hope, and a sweep is exactly the kind of caller that
    should be held to it.

    Bound to the `engine` this function was *given*, never to `db.SessionLocal`.
    The harness rebuilds the engine per test and reloads `app.db`, so a
    module-level factory here would sweep the wrong database -- the same trap
    `routers/auth.py` documents, and one this build has already walked into
    once.

    **The actor is the key's own owner.** `batches.actor_id` is NOT NULL and
    `ON DELETE RESTRICT`, so a batch needs a real user, and this app has no
    system account to borrow. The owner is the honest answer anyway: it was
    their key, and "your key expired and was tidied up" is a sentence about
    them. Grouped by owner *and* household so each batch carries the
    `household_id` that puts it in the right History.
    """
    gone = 0
    with AuditedSession(bind=engine, expire_on_commit=False) as own:
        doomed = agent_key_service.sweep(own, now=now)
        if not doomed:
            return 0
        grouped: dict[tuple[str, str], list] = {}
        for key in doomed:
            grouped.setdefault((key.user_id, key.household_id), []).append(key)
        for (user_id, household_id), keys in grouped.items():
            with batch(
                own,
                kind=BatchKind.admin,
                actor_id=user_id,
                household_id=household_id,
                source={"housekeeping": "agent_keys"},
            ):
                for key in keys:
                    own.delete(key)
            gone += len(keys)
        own.commit()
    return gone


def _sweep_invitations(engine: Engine, *, now) -> int:
    """Invitations past `invitations.RETENTION`, deleted **through the ORM**.

    Audited, like `agent_keys`, and for the same reasons on every count: an
    `AuditedSession` bound to the engine given, a batch per actor, the actor
    being the person who sent the invitation. The log's image of the deleted
    row does not carry the address: `Invitation.__audit_redact__` names it, or
    the sweep would only move the address from one table to another.
    """
    gone = 0
    with AuditedSession(bind=engine, expire_on_commit=False) as own:
        doomed = invitation_service.sweep(own, now=now)
        if not doomed:
            return 0
        grouped: dict[str, list] = {}
        for invitation in doomed:
            grouped.setdefault(invitation.invited_by_id, []).append(invitation)
        for inviter_id, invitations in grouped.items():
            with batch(
                own,
                kind=BatchKind.admin,
                actor_id=inviter_id,
                source={"housekeeping": "invitations"},
            ):
                for invitation in invitations:
                    own.delete(invitation)
            gone += len(invitations)
        own.commit()
    return gone


def _sweep_account_resets(engine: Engine, *, now) -> int:
    """Reset links past `account_resets.RETENTION`, deleted **through the ORM**.

    Audited, so the same shape as `_sweep_invitations`. The actor is the
    account the link was for rather than whoever issued it: the issuer may be
    nobody (a link from the server), `batches.actor_id` needs a real user, and
    "your reset link lapsed and was tidied up" is a sentence about them.
    """
    gone = 0
    with AuditedSession(bind=engine, expire_on_commit=False) as own:
        doomed = account_reset_service.sweep(own, now=now)
        if not doomed:
            return 0
        for reset in doomed:
            with batch(
                own,
                kind=BatchKind.admin,
                actor_id=reset.user_id,
                source={"housekeeping": "account_resets"},
            ):
                own.delete(reset)
            gone += 1
        own.commit()
    return gone
