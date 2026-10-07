"""What this instance *is*, as opposed to what is in it.

Everything else in `services/` answers a question about a household's ledger.
This one answers the questions an owner asks about the installation: how big it
has got, where its files are, which version it is, what it depends on, when it
was last backed up, and what it has been writing in its log.

The reason it is a module rather than a handful of lines in the router is that
almost none of it is a database read. It is the filesystem, the package
metadata, the SQLite pragmas and -- for one operation, and only when somebody
presses the button -- the network. Keeping that in one place means the router
stays what every other router here is: thin.

> [!important] Nothing here is reachable by a member, and nothing here is
> reachable by an agent key. Every route that calls into this module depends on
> `OwnerOnly`. The figures name households and count their rows, the paths say
> where the database is, and the log may contain anything the app has ever said
> about a request.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import socket
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import __version__, build, config, logging_setup
from ..api import dbview
from ..auth import setup as setup_service
from ..models import Base, Household, Receipt, Transaction


def _settings():
    """The settings object that is current *now*.

    Resolved at call time rather than bound at import -- see the same note in
    `app/logging_setup.py`. Every path and every flag this module reports has
    to come from the live configuration, or a reloaded one leaves this module
    describing an instance that no longer exists. It does not raise; it reports
    the wrong directory, which is worse.
    """
    return config.settings


#: The repository this is a copy of. Used for the About links and for the
#: version check, and written out rather than read from `git remote`: a
#: deployment is a copy of the files, not a clone, and the one that most needs
#: to know where its upstream is is the one with no `.git` directory.
REPOSITORY = "https://github.com/MarioLonghi-com/household-spend-tracker"
#: Whose it is. Asked for by name in the review; kept here so the About block
#: on the client is data from the server rather than a second hard-coded copy.
AUTHOR = "https://mariolonghi.com/projects"

#: Where `git` would be if this is a working copy rather than a deployment.
_ROOT = Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------- #
# Where everything is
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Place:
    """One path this instance uses, and whether it is actually there.

    `exists` is sent rather than inferred on the client because half the value
    of this list is spotting the one that is missing -- a `dist` directory that
    is not there is why the SPA 404s, and it was issue #1 from a fresh clone.

    `optional` is the other half of that, and it earns its place: the setup
    token is deleted the moment setup finishes and the `/db` snapshot only
    exists on an instance that has run `make snapshot`. Flagging those two as
    missing makes a healthy instance display two faults, and a screen that
    cries wolf twice is a screen nobody reads the third time.
    """

    what: str
    path: str
    exists: bool
    bytes: int | None
    note: str
    optional: bool = False


def _place(what: str, path: Path, note: str, *, optional: bool = False) -> Place:
    try:
        size = path.stat().st_size if path.is_file() else None
        there = path.exists()
    except OSError:  # pragma: no cover - a path this process cannot stat
        size, there = None, False
    return Place(
        what=what, path=str(path), exists=there, bytes=size, note=note, optional=optional
    )


def database_path() -> Path | None:
    """The SQLite file, or None on anything else.

    `sqlite:////abs/path` and `sqlite:///relative/path` are the two shapes, and
    the difference is one slash -- so it is parsed here once rather than in
    each of the three callers that want the file.
    """
    url = _settings().database_url
    if not url.startswith("sqlite:"):
        return None
    _, _, tail = url.partition("sqlite:///")
    return Path(tail).resolve() if tail else None


def backup_dir() -> Path:
    return _settings().data_dir / "backups"


def places() -> list[Place]:
    """Every path worth knowing, in the order somebody would want them."""
    found = [
        _place("Installation", _ROOT, "the code this process is running"),
        _place("Data directory", _settings().data_dir, "everything that is not the code"),
    ]
    db = database_path()
    if db is not None:
        found.append(_place("Database", db, "the ledger itself"))
        # WAL mode, so these two exist while the instance is running and hold
        # committed data that is not yet in the main file. Anyone copying the
        # database by hand needs to know they are there.
        found.append(
            _place(
                "Write-ahead log",
                db.with_name(db.name + "-wal"),
                "uncheckpointed writes",
                optional=True,
            )
        )
    found += [
        _place("Secret key", _settings().data_dir / "secret.key", "back this up with the database — without it every session and every authenticator is void"),
        _place(
            "Backups", backup_dir(), "where the backup operation below writes", optional=True
        ),
        _place("Log files", logging_setup.log_dir(), "what this instance has been saying"),
        _place("Client build", _ROOT / "app" / "static" / "dist", "the built SPA; `/` 404s without it"),
        _place(
            "Browsable snapshot",
            dbview.snapshot_path(),
            "the redacted copy `/db` serves, if one was built",
            optional=True,
        ),
        _place(
            "Setup token",
            setup_service.setup_token_path(),
            "present only while setup is unfinished",
            optional=True,
        ),
    ]
    return found


# --------------------------------------------------------------------------- #
# How big it has got
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TableRows:
    name: str
    rows: int
    #: Bytes the values in this table occupy. See `table_report` for what the
    #: number is and is not.
    bytes: int | None


@dataclass(frozen=True, slots=True)
class HouseholdData:
    id: str
    name: str
    transactions: int
    receipts: int


@dataclass(frozen=True, slots=True)
class Size:
    """The database on disk, as three files rather than one.

    A WAL database is the main file plus `-wal` plus `-shm`, and reporting only
    the first says a busy instance is smaller than it is -- sometimes by a lot,
    because the WAL is only folded back in at a checkpoint.
    """

    total_bytes: int
    main_bytes: int
    wal_bytes: int
    page_size: int | None
    page_count: int | None
    free_pages: int | None


def size(session: Session) -> Size:
    db = database_path()
    main = wal = 0
    if db is not None:
        for path, which in ((db, "main"), (db.with_name(db.name + "-wal"), "wal")):
            try:
                byte_count = path.stat().st_size
            except OSError:
                byte_count = 0
            if which == "main":
                main = byte_count
            else:
                wal = byte_count

    def pragma(name: str) -> int | None:
        try:
            return int(session.execute(text(f"PRAGMA {name}")).scalar_one())
        except Exception:  # pragma: no cover - not SQLite
            return None

    return Size(
        total_bytes=main + wal,
        main_bytes=main,
        wal_bytes=wal,
        page_size=pragma("page_size"),
        page_count=pragma("page_count"),
        free_pages=pragma("freelist_count"),
    )


@dataclass(frozen=True, slots=True)
class DatabaseEngine:
    """What this database *is*, as opposed to how big it is.

    The screen reported size, pages and free pages, and `database_url_scheme`
    as a note under the size -- the bare word `sqlite`. It never said which
    SQLite, whether it was in WAL mode, or where the file was; the path was
    filed under Places with the log directory, which is where you look for
    paths and not where you look for the database. All three were a terminal
    away and all three are one query or one existing helper. Issue #52.
    """

    #: "SQLite", or the URL's scheme for anything else.
    name: str
    #: "3.45.1". None when the engine will not say, which is every non-SQLite.
    version: str | None
    #: "wal" or "delete". The WAL/SHM entries in Places already assume WAL; this
    #: is the instance confirming it rather than the reader inferring it.
    journal_mode: str | None
    #: The resolved file, through `database_path()` and never from
    #: `settings.database_url` -- that one can carry a password.
    path: str | None


def engine(session: Session) -> DatabaseEngine:
    """The database's identity, for the Database section of the screen.

    `session.execute` the same way `size()` does, on the connection already
    open. A non-SQLite URL gets the scheme and nothing else: the host is not
    worth the risk of reassembling a URL that had credentials in it.
    """
    url = _settings().database_url
    if not url.startswith("sqlite:"):
        return DatabaseEngine(
            name=url.partition(":")[0], version=None, journal_mode=None, path=None
        )

    def ask(sql: str) -> str | None:
        try:
            value = session.execute(text(sql)).scalar()
        except Exception:  # pragma: no cover - not SQLite after all
            return None
        return str(value) if value is not None else None

    here = database_path()
    return DatabaseEngine(
        name="SQLite",
        version=ask("SELECT sqlite_version()"),
        journal_mode=ask("PRAGMA journal_mode"),
        path=str(here) if here else None,
    )


def _dbstat_bytes(session: Session) -> dict[str, int] | None:
    """Per-table bytes from SQLite's own accounting, when it has it.

    `dbstat` is a compile-time option (`SQLITE_ENABLE_DBSTAT_VTAB`). It is on
    in most distribution builds and off in the Python.org macOS one, so this is
    tried and not relied on -- and when it is absent `table_report` measures the
    values instead and says which number it gave you.
    """
    try:
        rows = session.execute(text("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")).all()
    except Exception:
        return None
    return {str(name): int(total or 0) for name, total in rows}


def _measured_bytes(session: Session, table: str, columns: list[str]) -> int | None:
    """The length of every value in the table, added up.

    Not the same number as `dbstat`'s and deliberately smaller: it is the data,
    with none of SQLite's page, index or free-space overhead. `cast(x AS BLOB)`
    is what makes `length` mean bytes rather than characters for text, and it
    leaves a NULL as NULL so `coalesce` can count it as nothing.
    """
    if not columns:
        return None
    # `+`, not `,`. `SUM(a, b, c)` is a three-argument call to an aggregate
    # that takes one, which SQLite refuses -- and the refusal was swallowed by
    # the `except` below, so every byte figure in the report came back empty.
    parts = " + ".join(f'COALESCE(LENGTH(CAST("{name}" AS BLOB)), 0)' for name in columns)
    try:
        total = session.execute(text(f'SELECT SUM({parts}) FROM "{table}"')).scalar()  # noqa: S608
    except Exception:  # pragma: no cover - a table this cannot measure
        return None
    return int(total or 0)


@dataclass(frozen=True, slots=True)
class TableReport:
    rows: list[TableRows]
    #: `"dbstat"` or `"measured"` -- what the byte figures mean. Named in the
    #: CSV and on the screen, because the two are not comparable and a reader
    #: who does not know which they have will compare them.
    method: str


def table_report(session: Session) -> TableReport:
    """Every table in the schema: its row count, and how big it is.

    The row counts are exact and cheap. The byte figures are the expensive half
    -- without `dbstat` every table is scanned -- which is why this is behind
    the CSV button rather than on the page.

    The list comes from the ORM metadata rather than from `sqlite_master`, so a
    table this app does not own (Alembic's `alembic_version`, or anything a
    viewer left behind) is not reported as part of the ledger. `alembic_version`
    is added back by name because "which migration is this at" is exactly what
    somebody reading this report wants.
    """
    dbstat = _dbstat_bytes(session)
    out: list[TableRows] = []
    for table in sorted(Base.metadata.tables.values(), key=lambda one: one.name):
        rows = int(session.execute(select(func.count()).select_from(table)).scalar_one())
        if dbstat is not None:
            byte_count: int | None = dbstat.get(table.name, 0)
        else:
            byte_count = _measured_bytes(session, table.name, [c.name for c in table.columns])
        out.append(TableRows(name=table.name, rows=rows, bytes=byte_count))
    return TableReport(rows=out, method="dbstat" if dbstat is not None else "measured")


def per_household(session: Session) -> list[HouseholdData]:
    """How much of the ledger each household accounts for.

    Two grouped reads rather than two per household. Counts, never amounts --
    `services/insights.py` has the long version of why nothing here adds two
    currencies together, and it applies to a screen about disk usage as much as
    to a report.
    """
    houses = list(session.execute(select(Household).order_by(Household.name)).scalars())
    transactions = dict(
        session.execute(
            select(Transaction.household_id, func.count()).group_by(Transaction.household_id)
        ).all()
    )
    receipts = dict(
        session.execute(
            select(Receipt.household_id, func.count()).group_by(Receipt.household_id)
        ).all()
    )
    return [
        HouseholdData(
            id=house.id,
            name=house.name,
            transactions=int(transactions.get(house.id, 0)),
            receipts=int(receipts.get(house.id, 0)),
        )
        for house in houses
    ]


# --------------------------------------------------------------------------- #
# What it is, and what it is made of
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Package:
    name: str
    version: str


def packages() -> list[Package]:
    """Every distribution installed in this environment, by name and version.

    Read from the installed metadata rather than from `requirements.txt`: that
    file says what was locked, and this question is what is actually running --
    which is the one that matters when a dependency is the suspect.
    """
    from importlib import metadata

    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            found[name] = dist.version or "?"
    return [Package(name=name, version=found[name]) for name in sorted(found, key=str.lower)]


def addresses(port: int | None = None) -> list[str]:
    """The URLs this machine can be reached at, loopback first.

    Worked out from the host's own resolution rather than from the request,
    because the whole point is to tell somebody standing at this machine what
    to type into a phone -- and the request they are making came in on
    localhost, which is the one address that will not work from a phone.
    """
    out = []
    if port:
        out.append(f"http://127.0.0.1:{port}")
    seen = set(out)
    try:
        # A UDP connect sends nothing; it just makes the kernel pick the
        # interface it would route out of, which is the address the other
        # machines on this network can reach. Reading `gethostbyname` instead
        # answers 127.0.0.1 as often as not.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routed nowhere
            host = probe.getsockname()[0]
        finally:
            probe.close()
        url = f"http://{host}:{port}" if port else f"http://{host}"
        if url not in seen:
            out.append(url)
    except OSError:  # pragma: no cover - a machine with no route at all
        pass
    return out


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Backup:
    name: str
    path: str
    bytes: int
    made_at: datetime


def backups() -> list[Backup]:
    directory = backup_dir()
    if not directory.is_dir():
        return []
    found = [
        Backup(
            name=path.name,
            path=str(path),
            bytes=path.stat().st_size,
            made_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
        )
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".sqlite3"
    ]
    return sorted(found, key=lambda one: one.made_at, reverse=True)


def latest_backup() -> Backup | None:
    made = backups()
    return made[0] if made else None


def find_backup(name: str) -> Backup | None:
    """The backup called `name`, found in the listing -- never joined onto a path.

    The same rule as `logging_setup.tail`: a name that is not in the listing is
    not a backup, so `../secret.key` and `/etc/passwd` are simply not found,
    with no normalisation to get subtly wrong.
    """
    return next((one for one in backups() if one.name == name), None)


def delete_backup(name: str) -> Backup | None:
    """Remove one backup file. Returns what went, or None when there was none.

    This reverses a sentence the screen used to carry -- "nothing here deletes
    an old backup" -- because #133 asked for it (2026-09-25). What stays true
    of that sentence is enforced elsewhere: nothing deletes one *on its own*.
    There is no pruning, no retention window and no timer; a backup goes when
    an owner names it and then confirms, and not otherwise.
    """
    found = find_backup(name)
    if found is None:
        return None
    Path(found.path).unlink(missing_ok=True)
    return found


class BackupFailed(RuntimeError):
    """A backup that did not happen. Said out loud rather than returning None."""


def make_backup() -> Backup:
    """A consistent copy of the database, as one file.

    `VACUUM INTO` and not a file copy. The database runs in WAL mode, so the
    main file on its own is missing every write since the last checkpoint --
    copying it is how you get a backup that restores to last Tuesday and looks
    perfectly valid. `VACUUM INTO` is SQLite writing out a complete, compacted
    database from the connection's own consistent view, with the instance still
    serving.

    It does **not** copy `secret.key`, and that is on purpose: the key is a
    secret at 0600 in one place, and a backup routine that scatters copies of it
    around is a worse failure than the one it prevents. The path to it is in
    `places()` with the sentence saying to keep it with the backup.
    """
    db = database_path()
    if db is None:
        raise BackupFailed("this instance is not on SQLite, so back it up with its own tooling")

    directory = backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"spendtracker-{stamp}.sqlite3"
    if target.exists():  # pragma: no cover - two backups inside one second
        raise BackupFailed("a backup from this second already exists; try again")

    # Its own connection, in autocommit: `VACUUM` refuses to run inside a
    # transaction, and every session this app hands out is in one.
    import sqlite3

    with sqlite3.connect(db, isolation_level=None) as connection:
        try:
            connection.execute("VACUUM INTO ?", (str(target),))
        except sqlite3.Error as problem:
            raise BackupFailed(str(problem)) from problem

    return Backup(
        name=target.name,
        path=str(target),
        bytes=target.stat().st_size,
        made_at=datetime.fromtimestamp(target.stat().st_mtime, UTC),
    )


# --------------------------------------------------------------------------- #
# Is there a newer one?
# --------------------------------------------------------------------------- #


#: The one outbound request this application makes, and it is made only when an
#: owner presses the button. No telemetry, nothing on a timer, and nothing about
#: this instance in the request -- it is a plain GET for a public list of tags.
UPSTREAM_TAGS = "https://api.github.com/repos/MarioLonghi-com/household-spend-tracker/tags"
UPSTREAM_TIMEOUT_SECONDS = 8


@dataclass(frozen=True, slots=True)
class Upstream:
    checked_at: datetime
    running: str
    latest: str | None
    newer: bool
    #: What to tell the user when there is no answer. Null when there is one.
    problem: str | None


def _as_numbers(version: str) -> tuple[int, ...]:
    """`v1.2.10` -> (1, 2, 10). Anything unparseable sorts as nothing.

    Stops at the first piece that does not begin with a digit, so `1.2.0-rc1`
    is (1, 2, 0) and compares as the release it is a candidate for rather than
    as something unreadable. An empty tuple is falsy, which is what
    `check_upstream` filters on to ignore a tag like `demo`.
    """
    parts: list[int] = []
    for piece in version.lstrip("vV").split("."):
        digits = ""
        for char in piece:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _trust() -> ssl.SSLContext:
    """A TLS context that can actually verify GitHub on this machine.

    The default context reads the *system* trust store, and the python.org
    macOS build does not use the one macOS keeps -- so every https request from
    it fails with `CERTIFICATE_VERIFY_FAILED: unable to get local issuer
    certificate` until somebody runs `Install Certificates.command`. This app
    is developed on exactly that build, so the check would have shipped looking
    broken. `certifi` is declared in requirements.in for this (#46); when it
    is somehow missing, the default context is still the right fallback.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover - certifi absent or unreadable
        return ssl.create_default_context()


#: A page of tags is a few kilobytes. Anything past this is not a tag list,
#: and it is not read to find out (#225).
UPSTREAM_MAX_BYTES = 1 << 20


def check_upstream() -> Upstream:
    """Ask the repository for its newest tag and compare it with what is running.

    Every failure is an answer rather than an exception: an instance on a
    network with no route out is the normal case for this app, and "could not
    reach GitHub" is a perfectly good thing to print on the screen. The one
    thing it must not do is look like the app is broken.
    """
    now = datetime.now(UTC)

    def unknown(why: str) -> Upstream:
        return Upstream(
            checked_at=now, running=__version__, latest=None, newer=False, problem=why
        )

    request = urllib.request.Request(  # noqa: S310 - a constant https URL
        UPSTREAM_TAGS,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "household-spend-tracker"},
    )
    from . import outbound

    try:
        # No redirect followed: GitHub's tag list does not move, and a 3xx is
        # an answer from something that is not it (#219).
        with outbound.opener(_trust()).open(
            request, timeout=UPSTREAM_TIMEOUT_SECONDS
        ) as answer:
            body = outbound.read_within(
                answer, limit=UPSTREAM_MAX_BYTES, seconds=UPSTREAM_TIMEOUT_SECONDS * 2
            )
        tags = json.loads(body.decode("utf-8"))
    except outbound.TooLarge:
        return unknown("the repository's answer was larger than a tag list")
    except outbound.TooSlow:
        return unknown("the repository took too long to answer")
    except urllib.error.HTTPError as problem:
        # Answered, and the answer was no. 404 is the one worth naming: it is
        # what GitHub returns for a repository that has moved or gone private,
        # and "HTTP Error 404" reads as a bug in the URL rather than as that.
        if problem.code == 404:
            return unknown(
                f"{REPOSITORY} answered 404: it is not public, or it has moved. "
                "Compare the version by hand against its releases page."
            )
        return unknown(f"the repository answered {problem.code}: {problem.reason}")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as problem:
        return unknown(f"could not reach the repository: {problem}")
    except RecursionError:
        return unknown("the repository's answer was not a tag list")
    if not isinstance(tags, list):
        return unknown("the repository's answer was not a tag list")

    names = [str(tag.get("name", "")) for tag in tags if isinstance(tag, dict)]
    versioned = [name for name in names if _as_numbers(name)]
    if not versioned:
        return unknown(
            "the repository has no version tags yet, so there is nothing to compare with"
        )
    latest = max(versioned, key=_as_numbers)
    return Upstream(
        checked_at=now,
        running=__version__,
        latest=latest,
        newer=_as_numbers(latest) > _as_numbers(__version__),
        problem=None,
    )


# --------------------------------------------------------------------------- #
# The whole picture
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Instance:
    app_name: str
    version: str
    #: Which commit, since `version` only moves at a release. See `app/build.py`.
    build: build.Build
    environment: str
    python: str
    platform: str
    schema_revision: str | None
    started_at: datetime | None
    #: Which process this is. The one number that tells two instances on one
    #: machine apart, and the one the screen could not give you -- so "is the
    #: dev run or the real one answering on 8848" ended in a terminal.
    process_id: int
    database_url_scheme: str
    engine: DatabaseEngine
    size: Size
    households: list[HouseholdData]
    places: list[Place]
    packages: list[Package]
    addresses: list[str]
    latest_backup: Backup | None
    logging_style: str
    logs: list[logging_setup.LogFile]
    repository: str
    author: str


def describe(session: Session, *, port: int | None = None, started_at: datetime | None = None) -> Instance:
    """Everything the Application management page opens with.

    Deliberately not the table report: that one scans, and this one is drawn on
    every visit.
    """
    try:
        revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # pragma: no cover - a database with no alembic table
        revision = None

    return Instance(
        app_name=_settings().app_name,
        version=__version__,
        build=build.current(),
        environment=_settings().environment,
        python=sys.version.split()[0],
        platform=f"{_platform.system()} {_platform.release()} ({_platform.machine()})",
        schema_revision=str(revision) if revision else None,
        started_at=started_at,
        process_id=os.getpid(),
        database_url_scheme=_settings().database_url.partition(":")[0],
        engine=engine(session),
        size=size(session),
        households=per_household(session),
        places=places(),
        packages=packages(),
        addresses=addresses(port),
        latest_backup=latest_backup(),
        logging_style=logging_setup.current().key,
        logs=logging_setup.files(),
        repository=REPOSITORY,
        author=AUTHOR,
    )
