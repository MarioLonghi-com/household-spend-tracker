"""The sweep that deletes what has expired -- and the wiring that runs it.

`ratelimit.prune()` existed, documented a thirty-day window, and had no caller
anywhere in the application. Every test here asserts a **count that changed**,
because the version of this that asserts "the sweep returned a dict" would have
passed against the code that had no caller at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from app.audit.batch import batch
from app.audit.guard import AuditedSession
from app.auth import housekeeping
from app.config import settings
from app.models import (
    AgentKey,
    AgentRequest,
    AgentScope,
    Base,
    Batch,
    BatchKind,
    Change,
    ChangeOp,
    Household,
    LoginAttempt,
    PendingSignIn,
    Role,
    StepUpGrant,
    TrustedDevice,
    User,
    WebAuthnChallenge,
    WebSession,
    utcnow,
)


@pytest.fixture()
def db(tmp_path):
    """A file-backed database, so the sweep's own session is a second connection.

    Not the shared in-memory `engine` fixture: `sqlite://` hands every session
    in a thread the *same* DBAPI connection, which would hide exactly the
    cross-transaction behaviour this module is about.
    """
    eng = create_engine(f"sqlite:///{tmp_path / 'sweep.sqlite3'}")

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_connection, _record):  # pragma: no cover - driver glue
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    Base.metadata.create_all(eng)
    factory = sessionmaker(
        class_=AuditedSession, bind=eng, expire_on_commit=False
    )
    with factory() as s:
        user = User(
            email="jane@example.com",
            email_canonical="jane@example.com",
            display_name="Jane",
            password_hash="argon2-placeholder",
            role=Role.owner,
            totp_secret=b"sealed-placeholder",
        )
        # The audit hook refuses a write to `users` with no batch open, and it
        # is installed globally -- so even a fixture builds its user the way the
        # setup wizard does. The first user is the actor of its own creation.
        s.execute(text("PRAGMA defer_foreign_keys=ON"))
        with batch(s, kind=BatchKind.setup, actor_id=user.id, own_transaction=False):
            s.add(user)
        s.commit()
        yield eng, factory, user
    eng.dispose()


@pytest.fixture()
def household_id(db) -> str:
    """A household for the agent keys to belong to.

    `agent_keys.household_id` is a real foreign key with PRAGMA foreign_keys on,
    so the sweep cannot be tested against an invented id.
    """
    _engine, factory, user = db
    with factory() as s:
        house = Household(name="Doe", base_currency="EUR")
        with batch(s, kind=BatchKind.admin, actor_id=user.id):
            s.add(house)
        s.commit()
        return house.id


def _count(factory, model) -> int:
    with factory() as s:
        return s.execute(select(func.count()).select_from(model)).scalar_one()


def test_a_session_past_its_absolute_expiry_is_deleted(db):
    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        s.add(WebSession(id_hash="dead", user_id=user.id, created_at=now, last_seen_at=now,
                         expires_at=now - timedelta(minutes=1)))
        s.add(WebSession(id_hash="live", user_id=user.id, created_at=now, last_seen_at=now,
                         expires_at=now + timedelta(days=10)))
        s.commit()
    assert _count(factory, WebSession) == 2

    removed = housekeeping.sweep(engine)

    assert removed["sessions"] == 1
    with factory() as s:
        survivors = set(s.execute(select(WebSession.id_hash)).scalars())
    assert survivors == {"live"}


def test_a_session_idle_past_the_window_is_deleted_although_it_has_not_expired(db):
    """Both clocks, the same two `sessions.lookup` checks on read.

    Deleting on `expires_at` alone leaves a row that stopped authenticating
    anybody a fortnight ago -- which is the row the finding was about.
    """
    engine, factory, user = db
    now = utcnow()
    long_idle = now - timedelta(seconds=settings.session_idle_seconds + 60)
    with factory() as s:
        s.add(WebSession(id_hash="idle", user_id=user.id, created_at=long_idle,
                         last_seen_at=long_idle, expires_at=now + timedelta(days=10)))
        s.commit()

    removed = housekeeping.sweep(engine)

    assert removed["sessions"] == 1
    assert _count(factory, WebSession) == 0


def test_an_abandoned_pending_sign_in_is_deleted(db):
    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        s.add(PendingSignIn(id_hash="stale", user_id=user.id, created_at=now,
                            expires_at=now - timedelta(seconds=1)))
        s.add(PendingSignIn(id_hash="fresh", user_id=user.id, created_at=now,
                            expires_at=now + timedelta(minutes=5)))
        s.commit()

    removed = housekeeping.sweep(engine)

    assert removed["pending_sign_ins"] == 1
    with factory() as s:
        assert set(s.execute(select(PendingSignIn.id_hash)).scalars()) == {"fresh"}


def test_an_expired_trusted_device_is_deleted(db):
    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        s.add(TrustedDevice(id_hash="old", user_id=user.id, created_at=now, last_used_at=now,
                            expires_at=now - timedelta(days=1)))
        s.add(TrustedDevice(id_hash="current", user_id=user.id, created_at=now, last_used_at=now,
                            expires_at=now + timedelta(days=20)))
        s.commit()

    removed = housekeeping.sweep(engine)

    assert removed["trusted_devices"] == 1
    with factory() as s:
        assert set(s.execute(select(TrustedDevice.id_hash)).scalars()) == {"current"}


def test_a_step_up_grant_nobody_came_back_to_spend_is_deleted(db):
    """Five minutes, fixed at issue -- so "expired" and "deletable" are the
    same instant, and there is no grace period to get wrong."""
    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        s.add(StepUpGrant(id_hash="spent-never", user_id=user.id,
                          created_at=now - timedelta(minutes=10),
                          expires_at=now - timedelta(seconds=1)))
        s.add(StepUpGrant(id_hash="still-good", user_id=user.id, created_at=now,
                          expires_at=now + timedelta(minutes=4)))
        s.commit()

    removed = housekeeping.sweep(engine)

    assert removed["step_up_grants"] == 1
    with factory() as s:
        assert set(s.execute(select(StepUpGrant.id_hash)).scalars()) == {"still-good"}


def test_login_attempts_past_the_retention_window_are_deleted(db):
    """The thirty days `Access Control Investigation.md` states, actually kept."""
    from app.auth.ratelimit import RETENTION

    engine, factory, _ = db
    now = utcnow()
    with factory() as s:
        s.add(LoginAttempt(email_canonical="jane@example.com", ip="100.64.0.7", ok=False,
                           kind="password", at=now - RETENTION - timedelta(days=1)))
        s.add(LoginAttempt(email_canonical="jane@example.com", ip="100.64.0.7", ok=False,
                           kind="password", at=now - timedelta(days=1)))
        s.commit()

    removed = housekeeping.sweep(engine)

    assert removed["login_attempts"] == 1
    assert _count(factory, LoginAttempt) == 1


def _a_key(user, household_id, **overrides) -> AgentKey:
    now = utcnow()
    fields = dict(
        token_hash="hash-" + str(overrides.pop("n", 1)),
        label="a key",
        user_id=user.id,
        household_id=household_id,
        scope=AgentScope.read,
        may_commit=False,
        created_at=now,
        expires_at=now + timedelta(days=90),
    )
    fields.update(overrides)
    return AgentKey(**fields)


def test_the_sweep_of_an_audited_table_does_not_raise_and_does_delete(db, household_id):
    """The mistake the spec calls the single most likely one, asserted.

    Five bulk deletes sit above this one in `housekeeping.sweep`, each correct
    because its table is `__audit__ = False`. `agent_keys` is audited, so the
    same shape would be refused by `guard._no_bulk_writes_to_audited_tables` --
    or, done through the ORM without a batch, by `hook`'s `NoOpenBatch`.

    So this asserts three things at once: the sweep does not raise, the row
    count drops by exactly one, and the deletion was *logged*, which is the
    whole reason it could not be a bulk statement.
    """
    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        # Planted inside a batch, because `agent_keys` is audited -- a fixture
        # is not exempt from the rule the sweep is being tested against.
        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=household_id):
            s.add(_a_key(user, household_id, n=1,
                         expires_at=now - service_keep() - timedelta(days=1)))
            s.add(_a_key(user, household_id, n=2))
        s.commit()
    assert _count(factory, AgentKey) == 2

    removed = housekeeping.sweep(engine)

    assert removed["agent_keys"] == 1
    with factory() as s:
        assert set(s.execute(select(AgentKey.token_hash)).scalars()) == {"hash-2"}
        logged = list(
            s.execute(
                select(Change).where(
                    Change.table_name == "agent_keys", Change.op == ChangeOp.delete
                )
            ).scalars()
        )
    assert len(logged) == 1, "an audited delete that logged nothing is the bug"
    assert logged[0].before["label"] == "a key"
    # And the credential is not in the before-image, so an undo cannot put a
    # working key back -- the reason `token_hash` is redacted.
    assert "token_hash" not in logged[0].before


def test_the_sweeps_batch_is_actored_by_the_keys_own_owner(db, household_id):
    """`batches.actor_id` is NOT NULL and RESTRICT, and there is no system user.

    The owner is the honest actor anyway: it was their key.
    """
    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=household_id):
            s.add(_a_key(user, household_id, n=3,
                         expires_at=now - service_keep() - timedelta(days=1)))
        s.commit()

    housekeeping.sweep(engine)

    with factory() as s:
        swept = list(
            s.execute(select(Batch).where(Batch.kind == BatchKind.admin)).scalars()
        )
    mine = [b for b in swept if (b.source or {}).get("housekeeping") == "agent_keys"]
    assert len(mine) == 1
    assert mine[0].actor_id == user.id
    assert mine[0].household_id == household_id, "so it lands in the right History"


def test_a_key_revoked_yesterday_is_kept_and_one_revoked_last_month_is_not(db, household_id):
    """The window exists so History still reads correctly after a revocation."""
    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=household_id):
            s.add(_a_key(user, household_id, n=4, revoked_at=now - timedelta(days=1)))
            s.add(_a_key(
                user, household_id, n=5,
                revoked_at=now - service_keep() - timedelta(days=1),
            ))
        s.commit()

    removed = housekeeping.sweep(engine)

    assert removed["agent_keys"] == 1
    with factory() as s:
        assert set(s.execute(select(AgentKey.token_hash)).scalars()) == {"hash-4"}


def service_keep():
    from app.services.agent_keys import KEEP_DEAD

    return KEEP_DEAD


def test_a_key_that_lapsed_yesterday_is_kept_as_long_as_one_revoked_yesterday(
    db, household_id
):
    """Expiry and revocation get the same grace, because they answer the same question.

    Expiry used to purge at `expires_at` exactly, so a lapsed key survived one
    housekeeping interval -- about six hours -- and then vanished from the list
    `for_user` promises not to drop entries from: *"what did I give out and
    when did it stop working"*. An expired key is already inert, refused by
    `lookup` long before the sweep sees it, so keeping the row costs no access.
    """
    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=household_id):
            # Dead, and well inside the window. Both must stay.
            s.add(_a_key(user, household_id, n=6, expires_at=now - timedelta(days=1)))
            s.add(_a_key(user, household_id, n=7, revoked_at=now - timedelta(days=1)))
            # Dead and past it. Must go.
            s.add(_a_key(
                user, household_id, n=8,
                expires_at=now - service_keep() - timedelta(days=1),
            ))
        s.commit()

    removed = housekeeping.sweep(engine)

    assert removed["agent_keys"] == 1
    with factory() as s:
        assert set(s.execute(select(AgentKey.token_hash)).scalars()) == {"hash-6", "hash-7"}


def test_the_sweep_leaves_everything_that_is_still_live(db, household_id):
    """Planted back: nothing live must go. A sweep that deletes the lot also
    makes every count-based assertion above pass."""
    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        s.add(WebSession(id_hash="live", user_id=user.id, created_at=now, last_seen_at=now,
                         expires_at=now + timedelta(days=10)))
        s.add(PendingSignIn(id_hash="fresh", user_id=user.id, created_at=now,
                            expires_at=now + timedelta(minutes=5)))
        s.add(TrustedDevice(id_hash="current", user_id=user.id, created_at=now, last_used_at=now,
                            expires_at=now + timedelta(days=20)))
        s.add(LoginAttempt(email_canonical="jane@example.com", ip="100.64.0.7", ok=False,
                           kind="password", at=now))
        s.add(StepUpGrant(id_hash="unspent", user_id=user.id, created_at=now,
                          expires_at=now + timedelta(minutes=4)))
        s.add(WebAuthnChallenge(id_hash="unanswered", user_id=user.id, purpose="register",
                                created_at=now, expires_at=now + timedelta(minutes=4)))
        with batch(s, kind=BatchKind.admin, actor_id=user.id, household_id=household_id):
            s.add(_a_key(user, household_id, n=9))
        # Unaudited, so no batch -- and inside the retention window, so it stays.
        s.add(AgentRequest(agent_key_id="a-key", at=now, method="GET",
                           route="/api/agent/v1/manifest", status=200))
        s.commit()
    # An invitation still outstanding (#211).
    from app.models import Invitation

    _invitation(factory, user, created_at=now, expires_at=now + timedelta(hours=48))

    removed = housekeeping.sweep(engine)
    # The file (#103): whatever the fixture's own writes left in the WAL is
    # checkpointed, which is not a row deleted.
    assert removed.pop("wal_pages") >= 0

    assert removed == {"sessions": 0, "pending_sign_ins": 0, "trusted_devices": 0,
                       "step_up_grants": 0, "webauthn_challenges": 0, "agent_requests": 0, "agent_replays": 0,
                       "receipt_blobs": 0, "login_attempts": 0, "agent_keys": 0,
                       # An agent's staged-and-forgotten imports. A person's
                       # are never touched -- see `sweep_stale_agent_previews`.
                       "staged_agent_imports": 0,
                       # A backup's download zip a crash left behind (#133).
                       "backup_downloads": 0,
                       "invitations": 0,
                       "account_resets": 0,
                       # Nothing deleted here, so no free pages to reclaim.
                       "free_pages": 0}
    for model in (WebSession, PendingSignIn, TrustedDevice, LoginAttempt, StepUpGrant,
                  AgentKey, AgentRequest, Invitation):
        assert _count(factory, model) == 1, f"{model.__tablename__} lost a live row"


def test_booting_the_app_actually_runs_a_sweep(monkeypatch, tmp_path):
    """The finding was never the function. It was that nothing called it.

    So this asserts the wiring and not the behaviour: boot the real app through
    its real lifespan, and the sweep has to have happened by the time the first
    request is answered.
    """
    import importlib

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'boot.sqlite3'}")
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path))

    import app.config as config

    importlib.reload(config)
    import app.db as db_module

    importlib.reload(db_module)
    Base.metadata.create_all(db_module.engine)

    from tests.conftest import _stamp_head

    _stamp_head(db_module.engine)

    import app.auth.housekeeping as hk

    calls: list[object] = []
    real = hk.sweep

    def counted(engine):
        calls.append(engine)
        return real(engine)

    monkeypatch.setattr(hk, "sweep", counted)

    import app.main as main

    importlib.reload(main)

    from fastapi.testclient import TestClient

    with TestClient(main.app, base_url="https://testserver") as client:
        assert client.get("/api/health").status_code == 200

    assert calls, "the app booted without ever sweeping"
    assert calls[0] is db_module.engine


# --------------------------------------------------------------------------- #
# Invitations (#211)
# --------------------------------------------------------------------------- #


def _invitation(factory, user, **when) -> str:
    from app.auth import tokens
    from app.models import Invitation

    with factory() as s:
        row = Invitation(
            token_hash=tokens.issue()[1],
            email=when.pop("email", "invitee@example.com"),
            invited_by_id=user.id,
            **when,
        )
        with batch(s, kind=BatchKind.admin, actor_id=user.id):
            s.add(row)
        s.commit()
        return row.id


def test_invitations_ended_past_the_retention_window_are_deleted_and_the_rest_kept(db):
    from app.models import Invitation
    from app.services.invitations import RETENTION

    engine, factory, user = db
    now = utcnow()
    long_ago = now - RETENTION - timedelta(days=1)
    lately = now - RETENTION + timedelta(days=1)
    future = now + timedelta(hours=48)

    gone = {
        _invitation(factory, user, created_at=long_ago, expires_at=long_ago),
        _invitation(factory, user, created_at=long_ago, expires_at=future, accepted_at=long_ago),
        _invitation(factory, user, created_at=long_ago, expires_at=future, revoked_at=long_ago),
    }
    kept = {
        _invitation(factory, user, created_at=lately, expires_at=lately),
        _invitation(factory, user, created_at=lately, expires_at=future, revoked_at=lately),
        _invitation(factory, user, created_at=now, expires_at=future),  # still outstanding
    }
    assert _count(factory, Invitation) == 6

    removed = housekeeping.sweep(engine)

    assert removed["invitations"] == 3
    with factory() as s:
        left = set(s.execute(select(Invitation.id)).scalars())
    assert left == kept and not (left & gone)


def test_the_log_of_a_swept_invitation_does_not_keep_the_address(db):
    """Deleting the row to stop keeping the address, and then keeping it in the
    before-image, would only move it."""
    from app.services.invitations import RETENTION

    engine, factory, user = db
    long_ago = utcnow() - RETENTION - timedelta(days=1)
    swept = _invitation(
        factory, user, created_at=long_ago, expires_at=long_ago, email="someone.private@example.com"
    )

    housekeeping.sweep(engine)

    with factory() as s:
        deletes = s.execute(
            select(Change).where(Change.row_id == swept, Change.op == ChangeOp.delete.value)
        ).scalars().all()
        logged = s.execute(select(Change).where(Change.table_name == "invitations")).scalars().all()
    assert len(deletes) == 1
    assert "email" in (deletes[0].redacted or [])
    assert not any("someone.private" in f"{c.before}{c.after}" for c in logged)


def test_the_retention_list_says_what_the_sweeps_do():
    """The module docstring states each window; this holds it to the constants."""
    from app.auth import ratelimit
    from app.services import invitations

    doc = housekeeping.__doc__
    assert f"login attempts: {ratelimit.RETENTION.days} days" in doc
    assert f"invitations: {invitations.RETENTION.days} days" in doc

    from app.services import account_resets

    assert f"account resets: {account_resets.RETENTION.days} days" in doc


# --------------------------------------------------------------------------- #
# Account reset links (#284)
# --------------------------------------------------------------------------- #


def test_reset_links_lapsed_past_the_retention_window_are_deleted_and_the_rest_kept(db):
    from app.auth import tokens
    from app.models import AccountReset
    from app.services.account_resets import RETENTION

    engine, factory, user = db
    now = utcnow()
    with factory() as s:
        other = User(email="sam@example.com", email_canonical="sam@example.com",
                     display_name="Sam", password_hash="argon2-placeholder", role=Role.member,
                     totp_secret=None)
        with batch(s, kind=BatchKind.admin, actor_id=user.id):
            s.add(other)
            s.flush()
            lapsed = AccountReset(user_id=user.id, token_hash=tokens.issue()[1], password=True,
                                  created_at=now - RETENTION - timedelta(days=4),
                                  expires_at=now - RETENTION - timedelta(days=1))
            lately = AccountReset(user_id=other.id, token_hash=tokens.issue()[1],
                                  authenticator=True, created_by_id=user.id,
                                  created_at=now - RETENTION,
                                  expires_at=now - RETENTION + timedelta(days=1))
            s.add_all([lapsed, lately])
        s.commit()
        lapsed_id, lately_id = lapsed.id, lately.id

    removed = housekeeping.sweep(engine)

    assert removed["account_resets"] == 1
    with factory() as s:
        assert set(s.execute(select(AccountReset.id)).scalars()) == {lately_id}
        deleted = s.execute(
            select(Change).where(Change.row_id == lapsed_id, Change.op == ChangeOp.delete.value)
        ).scalar_one()
        swept_by = s.get(Batch, deleted.batch_id)
    # Actored by the account the link was for: the issuer may be nobody.
    assert swept_by.actor_id == user.id
    assert "token_hash" in (deleted.redacted or [])


# --------------------------------------------------------------------------- #
# The file itself: WAL checkpoint, VACUUM, ANALYZE after a large write (#103)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def wal_db(tmp_path):
    """A file database in WAL mode, the way `app/db.py` opens the real one."""
    path = tmp_path / "tend.sqlite3"
    eng = create_engine(f"sqlite:///{path}")

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_connection, _record):  # pragma: no cover - driver glue
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE filler (id INTEGER PRIMARY KEY, body BLOB)"))
    yield eng, path
    eng.dispose()


def _fill(eng, rows: int, size: int = 4000) -> None:
    with eng.begin() as conn:
        conn.execute(
            text("INSERT INTO filler (body) VALUES (randomblob(:size))"),
            [{"size": size}] * rows,
        )


def _rows(eng) -> int:
    with eng.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM filler")).scalar_one()


def test_the_checkpoint_empties_the_wal_and_keeps_every_row(wal_db):
    eng, path = wal_db
    _fill(eng, 300)
    wal = path.with_name(path.name + "-wal")
    before = wal.stat().st_size

    done = housekeeping.tend_the_file(eng)

    assert before > 1_000_000, "the import left the WAL at its high-water mark"
    assert wal.stat().st_size == 0
    assert done["wal_pages"] > 0
    assert _rows(eng) == 300


def test_a_file_that_is_mostly_free_pages_is_vacuumed(wal_db):
    eng, path = wal_db
    _fill(eng, 3000)
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM filler WHERE id > 200"))
    # The deletes reach the main file's free list only at a checkpoint.
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        free_before = conn.execute(text("PRAGMA freelist_count")).scalar_one()
    size_before = path.stat().st_size

    done = housekeeping.tend_the_file(eng)

    with eng.connect() as conn:
        free_after = conn.execute(text("PRAGMA freelist_count")).scalar_one()
    assert free_before >= housekeeping.VACUUM_MIN_FREE_PAGES
    assert free_after == 0
    assert done["free_pages"] == free_before
    assert path.stat().st_size < size_before / 3
    assert _rows(eng) == 200, "a VACUUM rewrites the file; it must not lose a row"


def test_a_file_with_a_few_free_pages_is_left_alone(wal_db):
    eng, path = wal_db
    _fill(eng, 3000)
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM filler WHERE id > 2900"))
    with eng.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        free_before = conn.execute(text("PRAGMA freelist_count")).scalar_one()

    done = housekeeping.tend_the_file(eng)

    with eng.connect() as conn:
        free_after = conn.execute(text("PRAGMA freelist_count")).scalar_one()
    assert free_before > 0
    assert free_after == free_before, "a tenth free is not worth rewriting the file"
    assert done["free_pages"] == 0
    assert _rows(eng) == 2900


def test_the_sweep_ends_by_tending_the_file(db, monkeypatch):
    eng, _factory, _user = db
    seen: list[object] = []
    monkeypatch.setattr(
        housekeeping, "tend_the_file", lambda engine: seen.append(engine) or {"wal_pages": 7}
    )
    removed = housekeeping.sweep(eng)
    assert seen == [eng]
    assert removed["wal_pages"] == 7


def _analysed(eng) -> int:
    with eng.connect() as conn:
        exists = conn.execute(
            text("SELECT count(*) FROM sqlite_master WHERE name = 'sqlite_stat1'")
        ).scalar_one()
        if not exists:
            return 0
        return conn.execute(
            text("SELECT count(*) FROM sqlite_stat1 WHERE tbl = 'login_attempts'")
        ).scalar_one()


def _attempts(factory, count: int) -> None:
    with factory() as s:
        for n in range(count):
            s.add(LoginAttempt(email_canonical=f"someone{n}@example.com", ip="192.0.2.1"))
        s.commit()


def test_a_commit_below_the_threshold_leaves_the_statistics_alone(db, monkeypatch):
    eng, factory, _user = db
    monkeypatch.setattr(housekeeping, "LARGE_WRITE_ROWS", 20)
    _attempts(factory, 19)
    assert _analysed(eng) == 0


def test_a_large_commit_refreshes_the_statistics_at_once(db, monkeypatch):
    eng, factory, _user = db
    monkeypatch.setattr(housekeeping, "LARGE_WRITE_ROWS", 20)
    before = _analysed(eng)
    _attempts(factory, 20)
    assert before == 0
    assert _analysed(eng) > 0


def test_rows_written_across_flushes_count_together(db, monkeypatch):
    eng, factory, _user = db
    monkeypatch.setattr(housekeeping, "LARGE_WRITE_ROWS", 20)
    with factory() as s:
        for n in range(10):
            s.add(LoginAttempt(email_canonical=f"first{n}@example.com", ip="192.0.2.3"))
        s.flush()
        for n in range(10):
            s.add(LoginAttempt(email_canonical=f"second{n}@example.com", ip="192.0.2.4"))
        s.commit()
    assert _count(factory, LoginAttempt) == 20
    assert _analysed(eng) > 0


def test_rows_rolled_back_do_not_count(db, monkeypatch):
    eng, factory, _user = db
    monkeypatch.setattr(housekeeping, "LARGE_WRITE_ROWS", 20)
    with factory() as s:
        for n in range(15):
            s.add(LoginAttempt(email_canonical=f"gone{n}@example.com", ip="192.0.2.2"))
        s.flush()
        s.rollback()
        for n in range(10):
            s.add(LoginAttempt(email_canonical=f"kept{n}@example.com", ip="192.0.2.3"))
        s.commit()
    assert _count(factory, LoginAttempt) == 10
    assert _analysed(eng) == 0
