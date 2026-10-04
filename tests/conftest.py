"""Fixtures.

Every test gets a throwaway in-memory database, the audit hook installed, and a
household with two of everything -- two users, two accounts, two currencies.
One card in a fixture is why the previous build let a refund fund the wrong
card; one currency is why three of its reports added EUR to GBP.
"""

from __future__ import annotations

import importlib
import os
import secrets
import tempfile
import time
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pyotp
import pytest
from fastapi.testclient import TestClient

# One data directory per process. Under pytest-xdist every worker is its own
# process and inherits the same SPENDTRACKER_DATA_DIR (CI points it at
# ./ci-data), and two things write into it at import rather than per test:
# app/config.py creates `secret.key` with O_TRUNC, not O_EXCL, so two workers
# booting at once can each write a different key into one file; and
# scripts/db_view.py binds `settings` at import and builds `snapshot.sqlite3`
# beside it. A plain `pytest` run is the worker called `main`.
_worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
_given = (os.environ.get("SPENDTRACKER_DATA_DIR") or "").strip()
if _given:
    _data_dir = Path(_given).resolve() / _worker
    _data_dir.mkdir(parents=True, exist_ok=True)
else:
    _data_dir = Path(tempfile.mkdtemp(prefix=f"spendtracker-test-{_worker}-"))
os.environ["SPENDTRACKER_DATA_DIR"] = str(_data_dir)

from argon2 import PasswordHasher  # noqa: E402
from sqlalchemy import create_engine, event, text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

import app.audit.guard  # noqa: E402,F401  -- installs the bulk-statement guard
import app.audit.hook  # noqa: E402,F401  -- installs the flush hook
import app.auth.passwords as _passwords  # noqa: E402

# Test-grade argon2. app/auth/passwords.py builds one PasswordHasher at import
# with argon2-cffi's defaults (t=3, m=64 MiB, p=4), and the setup wizard every
# `client` test walks hashes a password and ten recovery codes: 798 ms per
# `_setup_owner` at those parameters, 135 ms at these (#241). Still real
# argon2id -- the `$argon2id$` prefix, verify and needs_rehash all behave --
# only the work factor is lower.
#
# A module attribute rather than a setting, so production code grows no
# test-only branch; the `client` fixture's `importlib.reload` does not reload
# `passwords`, so the swap holds for the session. The seed subprocess and the
# upgrade rehearsal still hash at production cost.
#
# `_DUMMY_HASH` is re-made with the same hasher. It was hashed at import at the
# default cost, and verify reads the cost from the hash, so leaving it would
# make "no such user" ~20x slower than "wrong password" -- exactly the timing
# gap test_auth_crypto.py exists to catch.
_passwords._hasher = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1)
_passwords._DUMMY_HASH = _passwords._hasher.hash(secrets.token_urlsafe(32))


# No fsync on the throwaway file databases. The `client` fixture builds one
# per test and SQLite's default synchronous level fsyncs every commit: boot
# measured 31 ms with it and 20 ms without. Registered on the Engine class like
# app/db.py's own listener; nothing here asserts durability across a crash.
@event.listens_for(Engine, "connect")
def _no_fsync_under_test(dbapi_connection, _record):  # pragma: no cover - driver glue
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA synchronous=OFF")
    cursor.close()

from app.audit.batch import batch  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountType,
    Base,
    BatchKind,
    Household,
    HouseholdMember,
    Role,
    User,
    utcnow,
)

JAN = date(2026, 1, 1)


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", future=True, connect_args={"check_same_thread": False})

    @event.listens_for(eng, "connect")
    def _fk(dbapi_connection, _record):  # pragma: no cover - driver glue
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine) -> Iterator[Session]:
    from app.audit.guard import AuditedSession

    factory = sessionmaker(
        class_=AuditedSession, bind=engine, autoflush=False, expire_on_commit=False, future=True
    )
    with factory() as s:
        yield s


def _bootstrap_user(session: Session, *, email: str, name: str, role: Role) -> User:
    """Create a user, which needs a batch, which needs a user.

    The first user is the actor of its own creation -- ids are assigned at
    construction, so the batch can name an actor that has not been inserted yet,
    and deferred foreign keys let both land in one transaction. This is exactly
    what the setup wizard does; the fixture exercises the real path.
    """
    user = User(
        email=email,
        email_canonical=email.lower(),
        display_name=name,
        password_hash="argon2-placeholder",
        role=role,
        totp_secret=b"sealed-placeholder",
    )
    session.execute(text("PRAGMA defer_foreign_keys=ON"))
    with batch(session, kind=BatchKind.setup, actor_id=user.id, own_transaction=False):
        session.add(user)
    session.commit()
    return user


@pytest.fixture()
def owner(session) -> User:
    return _bootstrap_user(session, email="jane@example.com", name="Jane", role=Role.owner)


@pytest.fixture()
def member(session, owner) -> User:
    return _bootstrap_user(session, email="partner@example.com", name="Partner", role=Role.member)


@pytest.fixture()
def household(session, owner, member) -> Household:
    """One household both users belong to."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id):
        house = Household(name="Doe-Smith", base_currency="EUR")
        session.add(house)
        session.flush()
        for user in (owner, member):
            session.add(
                HouseholdMember(
                    household_id=house.id, user_id=user.id, added_by_id=owner.id, added_at=utcnow()
                )
            )
    return house


@pytest.fixture()
def other_household(session, member) -> Household:
    """A household the owner is NOT a member of, so 404s have something to prove."""
    with batch(session, kind=BatchKind.admin, actor_id=member.id):
        house = Household(name="Somebody Else", base_currency="GBP")
        session.add(house)
        session.flush()
        session.add(
            HouseholdMember(
                household_id=house.id, user_id=member.id, added_by_id=member.id, added_at=utcnow()
            )
        )
    return house


@pytest.fixture()
def accounts(session, household, owner) -> dict[str, Account]:
    """Two accounts, two currencies, one of them a card."""
    with batch(session, kind=BatchKind.admin, actor_id=owner.id, household_id=household.id):
        checking = Account(
            household_id=household.id, name="Checking", type=AccountType.checking, currency="EUR"
        )
        card = Account(
            household_id=household.id, name="Visa", type=AccountType.credit_card, currency="EUR"
        )
        pounds = Account(
            household_id=household.id, name="UK Savings", type=AccountType.savings, currency="GBP"
        )
        session.add_all([checking, card, pounds])
    return {"checking": checking, "card": card, "pounds": pounds}


@pytest.fixture()
def jan() -> date:
    return JAN


@pytest.fixture()
def feb() -> date:
    return JAN + timedelta(days=31)


@pytest.fixture()
def clock(monkeypatch):
    """Move the world forward, rather than reaching for a future code.

    The drift window is one step either side, and every accepted code burns its
    step for good. So a test that signs in three times cannot do it at a single
    instant -- and reaching further into the future does not help, because the
    server will not look that far. In reality it is the clock that moves, so the
    clock moves here.
    """
    offset = {"seconds": 0}
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + offset["seconds"])

    def advance(steps: int = 1) -> int:
        offset["seconds"] += steps * 30
        return int(time.time())

    return advance


# --------------------------------------------------------------------------- #
# A whole application, for the tests that go through HTTP
# --------------------------------------------------------------------------- #

PASSWORD = "a sufficiently long password"
#: The CSRF middleware wants a request that says where it came from.
HEADERS = {"sec-fetch-site": "same-origin"}


def _stamp_head(engine) -> None:
    """Tell the boot-time schema check this database is current.

    `create_all` plus a stamp, not `alembic upgrade head`: the real upgrade takes
    about 1.4 seconds and the client fixture runs per test, which would add four
    minutes to the suite. The stamp is not a lie -- `test_migrations.py`
    separately proves `upgrade head` and `create_all` produce exactly the same
    schema, so this asserts something already verified rather than assumed.
    """
    from app import schema_check

    head = schema_check.expected_head()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "create table if not exists alembic_version "
            "(version_num varchar(32) not null primary key)"
        )
        connection.exec_driver_sql("delete from alembic_version")
        connection.exec_driver_sql(
            "insert into alembic_version (version_num) values (?)", (head,)
        )


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """A whole app on its own database, booted through its real lifespan."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.sqlite3'}")
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path))

    import app.config as config

    importlib.reload(config)
    import app.db as db

    importlib.reload(db)
    from app.models import Base

    Base.metadata.create_all(db.engine)
    _stamp_head(db.engine)

    import app.main as main

    importlib.reload(main)

    # https, because the cookies are Secure and httpx will not send a Secure
    # cookie over http. Testing over http would mean weakening the real flag.
    try:
        with TestClient(main.app, base_url="https://testserver") as test_client:
            test_client.app_module = main
            yield test_client
    finally:
        # Each test reloads app.db and so builds a new engine; without this
        # the old one's pooled connections are closed only by the garbage
        # collector, which is where `ResourceWarning: unclosed database` came
        # from, hundreds of times a run.
        db.engine.dispose()


def _setup_owner(client, tmp_path_token: str | None = None) -> dict:
    """Walk the real wizard: token, owner, a working code, done."""
    import app.auth.setup as setup_service

    token = setup_service.current_setup_token()
    started = client.post(
        "/api/setup/begin",
        json={
            "token": token,
            "email": "Jane.Doe@gmail.com",
            "display_name": "Jane",
            "password": PASSWORD,
        },
        headers=HEADERS,
    )
    assert started.status_code == 200, started.text
    body = started.json()

    code = pyotp.TOTP(body["secret"]).at(int(time.time()))
    enrolled = client.post(
        "/api/setup/enrol", json={"blob": body["blob"], "code": code}, headers=HEADERS
    )
    assert enrolled.status_code == 200, enrolled.text

    done = client.post(
        "/api/setup/complete",
        json={"blob": enrolled.json()["blob"], "codes_saved": True},
        headers=HEADERS,
    )
    assert done.status_code == 200, done.text
    # The enrolment code is returned so a test can prove it is spent; the
    # recovery codes so a test can prove they can be redeemed.
    return {
        "user": done.json(),
        "secret": body["secret"],
        "enrolment_code": code,
        "recovery_codes": body["recovery_codes"],
    }


