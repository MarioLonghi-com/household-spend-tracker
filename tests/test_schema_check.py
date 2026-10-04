"""The boot-time schema check.

Every branch here corresponds to a way somebody actually arrives at a database
the code cannot trust, and the point of each is the sentence it prints -- so the
tests assert on the message, not just on the raising.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app import schema_check
from app.models import Base


def _engine(tmp_path, name="probe.sqlite3"):
    return create_engine(f"sqlite:///{tmp_path / name}")


def _stamp(engine, revision: str) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "create table if not exists alembic_version "
            "(version_num varchar(32) not null primary key)"
        )
        connection.exec_driver_sql("delete from alembic_version")
        connection.exec_driver_sql(
            "insert into alembic_version (version_num) values (?)", (revision,)
        )


def test_a_current_database_starts(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    _stamp(engine, schema_check.expected_head())

    assert schema_check.verify(engine) == schema_check.expected_head()


def test_an_empty_database_says_to_migrate(tmp_path):
    """The fresh-clone case: `make dev` before `make migrate`."""
    engine = _engine(tmp_path)
    with engine.connect():  # bring the file into existence, and nothing else
        pass

    with pytest.raises(schema_check.SchemaOutOfDate) as raised:
        schema_check.verify(engine)
    assert "never been migrated" in str(raised.value)
    assert "make migrate" in str(raised.value)


def test_tables_with_no_stamp_are_refused(tmp_path):
    """The one that actually happened.

    A demo database built by an older `seed_demo` used `create_all`, so it had
    every table and no `alembic_version` at all. The app started, `/api/health`
    answered ok, and signing in threw `no such table: pending_sign_ins` out of
    the driver.
    """
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)

    with pytest.raises(schema_check.SchemaOutOfDate) as raised:
        schema_check.verify(engine)
    message = str(raised.value)
    assert "no Alembic stamp" in message
    # It has to say what to do, and both routes are legitimate.
    assert f"alembic stamp {schema_check.expected_head()}" in message
    assert "make seed" in message


def test_a_database_behind_the_code_names_both_revisions(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    earlier = next(
        r for r in schema_check.known_revisions() if r != schema_check.expected_head()
    )
    _stamp(engine, earlier)

    with pytest.raises(schema_check.SchemaOutOfDate) as raised:
        schema_check.verify(engine)
    message = str(raised.value)
    assert earlier in message and schema_check.expected_head() in message
    assert "make migrate" in message


def test_a_database_ahead_of_the_code_says_so_differently(tmp_path):
    """Deploying an older build over a migrated database.

    "Run the migrations" is the wrong advice here -- the migration that would
    fix it does not exist in this checkout.
    """
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    _stamp(engine, "f00ddeadbeef")

    with pytest.raises(schema_check.SchemaOutOfDate) as raised:
        schema_check.verify(engine)
    message = str(raised.value)
    assert "not a revision this code knows about" in message
    assert "ahead of the code" in message
    assert "make migrate" not in message, "that advice cannot work here"


def test_several_stamps_are_refused_rather_than_guessed(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(engine)
    _stamp(engine, schema_check.expected_head())
    with engine.begin() as connection:
        connection.execute(
            text("insert into alembic_version (version_num) values ('f00ddeadbeef')")
        )

    with pytest.raises(schema_check.SchemaOutOfDate) as raised:
        schema_check.verify(engine)
    assert "several revisions" in str(raised.value)


def test_the_app_refuses_to_start_on_a_stale_database(tmp_path, monkeypatch):
    """End to end: the process stops rather than serving broken routes."""
    import importlib

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'stale.sqlite3'}")
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path))

    import app.config as config

    importlib.reload(config)
    import app.db as db

    importlib.reload(db)
    Base.metadata.create_all(db.engine)  # tables, deliberately unstamped

    import app.main as main

    importlib.reload(main)

    from fastapi.testclient import TestClient

    with (
        pytest.raises(schema_check.SchemaOutOfDate),
        TestClient(main.app, base_url="https://testserver"),
    ):
        pass  # pragma: no cover - the lifespan raises before this runs
