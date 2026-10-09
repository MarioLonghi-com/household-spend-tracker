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

And one for memory, because the pool above is unbounded (#102):

``soft_heap_limit`` and a host-sized ``cache_size``
    Each connection keeps its own page cache, and a sort or a GROUP BY with
    ``temp_store=MEMORY`` its own temporary b-trees. Measured on a 95 MiB
    ledger (129,024 rows), each connection that read the register and a
    report held about 48 MiB with a 32 MiB cache: ten at once was 456 MiB
    above the process's baseline, which is a 1 GB VM's memory limit reached
    by a burst nobody would call unusual. Bounding the pool would bring back
    the deadlock above, so the bound is on memory instead: SQLite's soft heap
    limit is process-wide, and SQLite gives cache pages back to stay under
    it. It is an eighth of the memory this process may use -- the cgroup's
    limit in a container, the machine's otherwise -- and ten such
    connections measured 178 MiB above the baseline under a 128 MiB limit.
    The per-connection cache is a sixty-fourth of the same figure, between 2
    and 32 MiB: one connection reading alone still gets a real cache, and a
    small host does not promise each of forty connections more than it has.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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

#: Where a container's memory limit is written: cgroup v2, then v1. A limit
#: of "max", or v1's "no limit" figure, is no limit at all.
CGROUP_LIMITS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
#: cgroup v1 writes "no limit" as a page-rounded LONG_MAX; anything past this
#: is that, not a real machine.
_NO_LIMIT = 1 << 60

MIB = 1 << 20
#: The per-connection page cache never goes below SQLite's own default nor
#: above the figure this app used to set for everyone.
CACHE_FLOOR = 2 * MIB
CACHE_CEILING = 32 * MIB


def host_memory() -> int | None:
    """The bytes this process may use: its cgroup's limit, else the machine's."""
    for path in CGROUP_LIMITS:
        try:
            text = path.read_text().strip()
        except OSError:
            continue
        if text.isdigit() and 0 < int(text) < _NO_LIMIT:
            return int(text)
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, AttributeError):  # pragma: no cover - no sysconf
        return None


def memory_budget(memory: int | None = None) -> tuple[int, int]:
    """(soft heap limit, per-connection cache), both in bytes, for `memory`.

    See the module docstring for the measurements. Without a figure for the
    host, the cache is the old 32 MiB and the heap is left unlimited -- which
    is how every instance ran before this, so it is the cautious unknown.
    """
    memory = host_memory() if memory is None else memory
    if not memory:
        return 0, CACHE_CEILING
    cache = min(max(memory // 64, CACHE_FLOOR), CACHE_CEILING)
    return memory // 8, cache


SOFT_HEAP_LIMIT, CACHE_BYTES = memory_budget()

#: How long a connection waits for SQLite's one write lock before it is told
#: "database is locked" (see `_sqlite_pragmas`). Read when a connection is
#: opened, so a test can shorten it and dispose of the pool; the 409 a request
#: gets when it runs out names it in the log (#273).
BUSY_TIMEOUT_MS = 5000

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
    cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # SQLite's documented setting under WAL. FULL fsyncs the log on every
    # COMMIT, and several paths here commit per row; NORMAL syncs at
    # checkpoints. A crash of the process loses nothing; a power cut can lose
    # the last few commits but cannot corrupt the file (#238).
    cursor.execute("PRAGMA synchronous=NORMAL")
    # Up to 32 MiB of page cache per connection, not the default 2 MiB, which
    # is a fiftieth of a ledger with a few years of history in it -- sized from
    # the host, under a process-wide limit. See the module docstring (#102).
    cursor.execute(f"PRAGMA cache_size=-{CACHE_BYTES // 1024}")
    if SOFT_HEAP_LIMIT:
        cursor.execute(f"PRAGMA soft_heap_limit={SOFT_HEAP_LIMIT}")
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
