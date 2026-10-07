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

All six auth tables here are `__audit__ = False` -- they record what happened
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
- pending sign-ins, trusted devices, step-up grants, WebAuthn challenges:
  until `expires_at`
- login attempts: 30 days (`ratelimit.RETENTION`), each an address as typed
- invitations: 30 days after they stopped being usable -- accepted, withdrawn
  or expired (`invitations.RETENTION`), each the invitee's address (#211)
- account resets: 30 days after they expired (`account_resets.RETENTION`).
  A used or withdrawn one is deleted there and then, so only lapsed links
  wait, and only so an owner can see that one lapsed (#284)

The database file, too (#103). Nothing in `app/` ever checkpointed the WAL:
SQLite's automatic checkpoint copies pages back into the main file but never
shrinks the `-wal` file, so after one large import it stayed at its high-water
mark for the life of the process. Every sweep now ends with
`wal_checkpoint(TRUNCATE)`, and a `VACUUM` when most of the file is free pages
-- a household deleted, a big import undone. And the planner's statistics are
refreshed after any commit that wrote `LARGE_WRITE_ROWS` rows or more, not only
on this timer, so a restore or a first import is not planned blind for six
hours (`note_large_writes`).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import delete, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
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
from . import passkeys, ratelimit, stepup

log = logging.getLogger("spendtracker.housekeeping")


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

        # A passkey challenge nobody came back with: five minutes, single use,
        # and nothing worth keeping once it can no longer be answered.
        removed["webauthn_challenges"] = passkeys.sweep(own, now=now)

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
    # Last, after every delete above has committed: the checkpoint can only
    # copy back what is committed, and the free pages a VACUUM would reclaim
    # are the ones those deletes just made.
    removed.update(tend_the_file(engine))
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


#: When a file counts as "mostly free pages": more than half of it, and at
#: least this many pages (4 MiB at SQLite's default 4 KiB page). Below that a
#: VACUUM reclaims nothing worth rewriting the file for.
VACUUM_FREE_FRACTION = 0.5
VACUUM_MIN_FREE_PAGES = 1024


def tend_the_file(engine: Engine) -> dict[str, int]:
    """Give the WAL back to the main file, and the free pages back to the disk.

    **The checkpoint.** SQLite checkpoints automatically every thousand pages,
    but a PASSIVE checkpoint never truncates the `-wal` file: it is reused from
    the start, at whatever size it once reached. One large import leaves a
    WAL of tens of megabytes beside a ledger that is mostly smaller than that,
    and `make backup`'s warning about copying the file exists because of it.
    `TRUNCATE` copies every committed page back and resets the file to zero
    bytes. It waits up to the busy timeout for readers, and a reader still
    open after that leaves the checkpoint partial rather than failing: the
    next sweep finishes it.

    **The VACUUM.** Deleting rows frees pages inside the file but never
    shrinks it. When more than `VACUUM_FREE_FRACTION` of the file is free --
    a household deleted, a large import undone -- the file is rebuilt. Only
    then: a VACUUM rewrites every live page and holds the write lock while it
    does, and when most pages are free there is little to rewrite. A VACUUM
    that meets a lock is skipped and tried again on the next sweep; it is
    never worth stalling a request for.

    Returns the pages each step handled, for the sweep's log line:
    `wal_pages` checkpointed, `free_pages` reclaimed.
    """
    if engine.dialect.name != "sqlite":
        return {}
    done = {"wal_pages": 0, "free_pages": 0}
    with engine.connect() as conn:
        # (busy, frames in the log, frames copied back); -1s outside WAL mode.
        # A TRUNCATE reports the log *after* resetting it -- zeros -- so a
        # PASSIVE pass first says how much there was, and does most of the
        # copying without waiting on anyone.
        _busy, frames, _copied = conn.exec_driver_sql("PRAGMA wal_checkpoint(PASSIVE)").one()
        busy, _frames, _copied = conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)").one()
        done["wal_pages"] = max(frames or 0, 0)
        if busy:
            log.info("housekeeping: WAL checkpoint was partial; a reader held it")

        pages = conn.exec_driver_sql("PRAGMA page_count").scalar_one()
        free = conn.exec_driver_sql("PRAGMA freelist_count").scalar_one()
        conn.rollback()
        if free < VACUUM_MIN_FREE_PAGES or free <= pages * VACUUM_FREE_FRACTION:
            return done
    try:
        # VACUUM cannot run inside a transaction, and SQLAlchemy begins one
        # implicitly: AUTOCOMMIT hands the statement to SQLite as it is.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("VACUUM")
            conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            done["free_pages"] = free - conn.exec_driver_sql(
                "PRAGMA freelist_count"
            ).scalar_one()
    except OperationalError:
        log.info("housekeeping: VACUUM skipped; the database was busy")
    return done


#: How many rows one commit has to write before the planner's statistics are
#: refreshed straight away rather than on the next sweep. A statement of a
#: few months is a few hundred rows and changes no index's shape; a first
#: import, a YNAB history or a restore is thousands.
LARGE_WRITE_ROWS = 1000

_WRITTEN_KEY = "housekeeping.rows_written"


@event.listens_for(AuditedSession, "after_flush")
def _count_written(session, _flush_context) -> None:
    """Rows this transaction has written so far, across every flush."""
    session.info[_WRITTEN_KEY] = (
        session.info.get(_WRITTEN_KEY, 0) + len(session.new) + len(session.deleted)
    )


@event.listens_for(AuditedSession, "after_rollback")
def _forget_written(session) -> None:
    session.info.pop(_WRITTEN_KEY, None)


@event.listens_for(AuditedSession, "after_commit")
def note_large_writes(session) -> None:
    """`ANALYZE` after a commit that wrote `LARGE_WRITE_ROWS` rows or more.

    The statistics behind the planner's index choices were refreshed only by
    the sweep, at boot and every six hours. A restore boots the app and is
    analysed at once, but a first import into a fresh install -- or a YNAB
    history, or an agent's backfill -- arrived into a ledger whose statistics
    described it as empty, and the planner chose indexes for an empty ledger
    for up to six hours (#103). Counted in the ORM rather than in each import
    route, so every path that writes thousands of rows is covered, including
    ones not written yet.

    Runs after the commit, on its own connection, so the write lock it needs
    for `sqlite_stat1` is free; `analysis_limit` keeps it to milliseconds. A
    failure here is logged and swallowed: the rows are committed, and stale
    statistics are a slower query, never a wrong one.
    """
    written = session.info.pop(_WRITTEN_KEY, 0)
    if written < LARGE_WRITE_ROWS:
        return
    try:
        bind = session.get_bind()
    except Exception:  # pragma: no cover - an unbound session wrote nothing here
        return
    engine = bind if isinstance(bind, Engine) else getattr(bind, "engine", None)
    if engine is None:
        return
    try:
        refresh_planner_statistics(engine)
    except OperationalError:
        log.info("housekeeping: ANALYZE after a large write skipped; the database was busy")


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
