"""Engine and session plumbing.

Two settings here are load-bearing for the audit log rather than for
performance, and both have a test asserting them:

``expire_on_commit=False``
    A commit that expires instances destroys the before-image of anything
    modified afterwards -- the attribute reports a new value and no old one, so
    the audit log records an update it cannot undo.

``PRAGMA foreign_keys=ON``
    The schema relies on foreign keys, and SQLite leaves them off by default.

``PRAGMA busy_timeout``
    SQLite defaults it to zero, which turns every lock contention into an
    immediate "database is locked" rather than a short wait.

A third is load-bearing for availability:

``max_overflow=-1`` on a file database
    Several paths open a *second* session while the request still holds its
    first -- `ratelimit.record`, `sessions.claim_pending`, `stepup.claim`,
    `totp.verify_and_consume`. SQLAlchemy's default pool is 5 + 10 and the
    threadpool has 40 workers, so fifteen concurrent failed sign-ins each held
    one connection and waited thirty seconds for another that could never come
    free: every request on the instance, `/api/health` included, stalled and
    then answered 500. An unauthenticated stranger could do that at will. A
    pool that can always open one more connection cannot deadlock this way; a
    SQLite connection is a file handle, and the threadpool already bounds how
    many requests can be asking.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from .config import settings
from .permissions import private_dir

_connect_args: dict = {}
_pool_args: dict = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}
    private_dir(settings.data_dir)
    if make_url(settings.database_url).database not in (None, "", ":memory:"):
        # See the module docstring: never make a request wait for a connection
        # that another waiting request is holding.
        _pool_args = {"poolclass": QueuePool, "pool_size": 5, "max_overflow": -1}

engine: Engine = create_engine(
    settings.database_url,
    echo=settings.echo_sql,
    connect_args=_connect_args,
    **_pool_args,
)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record) -> None:  # pragma: no cover - driver glue
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    # SQLite's default busy timeout is *zero*: a connection that meets a write
    # lock fails immediately with "database is locked" rather than waiting for
    # it. Every sync endpoint runs on a threadpool worker, and this app
    # deliberately opens extra short-lived sessions on their own transactions --
    # `ratelimit.record`, `ratelimit.prune`, `sessions.claim_pending`,
    # `housekeeping.sweep`. `claim_pending` already carries the scar: its
    # docstring records "database is locked on every wrong code". Five seconds
    # is far longer than any write here takes and far shorter than a user waits.
    cursor.execute("PRAGMA busy_timeout=5000")
    # SQLite's documented setting under WAL. FULL fsyncs the log on every
    # COMMIT, and several paths here commit per row; NORMAL syncs at
    # checkpoints. A crash of the process loses nothing; a power cut can lose
    # the last few commits but cannot corrupt the file (#238).
    cursor.execute("PRAGMA synchronous=NORMAL")
    # 32 MiB of page cache per connection, not the default 2 MiB, which is a
    # fiftieth of a ledger with a few years of history in it.
    cursor.execute("PRAGMA cache_size=-32768")
    # The temporary b-trees nearly every grouped read builds, in memory
    # rather than in a file.
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.close()


from .audit.guard import AuditedSession  # noqa: E402  -- after engine, to avoid a cycle

SessionLocal = sessionmaker(
    class_=AuditedSession,
    bind=engine,
    autoflush=False,
    #: Not a performance choice -- see the module docstring.
    expire_on_commit=False,
)


def get_session() -> Iterator[Session]:
    """FastAPI dependency. One request, one session, one transaction."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
