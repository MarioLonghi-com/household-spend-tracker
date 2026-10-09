"""The demo seed, run the way CI and a newcomer run it.

A subprocess with its own data directory, because the seed binds its engine from
the environment at import -- which is also the only way to prove the thing the
script is actually for: that `make seed` leaves a database somebody can sign
into.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _run_seed(tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "SPENDTRACKER_DATA_DIR": str(tmp_path),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'seed.sqlite3'}",
    }
    return subprocess.run(
        [sys.executable, "-m", "scripts.seed_demo", "--reset", "--months", "1"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.fixture(scope="module")
def seeded(tmp_path_factory) -> tuple[subprocess.CompletedProcess, Path]:
    """One run, shared: seeding is argon2 plus a month of activity."""
    tmp_path = tmp_path_factory.mktemp("seed")
    done = _run_seed(tmp_path)
    assert done.returncode == 0, f"the seed failed:\n{done.stdout}\n{done.stderr}"
    return done, tmp_path / "seed.sqlite3"


def test_the_seed_builds_its_schema_through_the_migrations(seeded):
    """`create_all` builds what the models say today and skips every migration.

    That left a demo database with no `alembic_version` row at all, so a later
    `alembic upgrade head` failed on "table households already exists" and the
    app threw 500s against a schema no upgrade path had produced.
    """
    _, database = seeded
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        stamped = db.execute("select version_num from alembic_version").fetchall()
        assert len(stamped) == 1, f"expected exactly one alembic stamp, got {stamped}"
    finally:
        db.close()

    # Read in process rather than shelling out to `.venv/bin/alembic`: CI
    # installs into the runner's own Python and has no .venv, so the first
    # version of this test failed there and nowhere else -- the same
    # works-on-my-machine shape as the issue it was written for.
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(REPO / "alembic.ini"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert stamped[0][0] == head, (
        f"the seed stamped {stamped[0][0]}, but the migration head is {head}"
    )


def test_the_seeded_owner_can_use_the_recovery_link(seeded):
    """The sign-in screen offers "I've lost my authenticator"; for the seeded
    owner that used to lead nowhere.

    `_seed_owner` built the user by hand and so minted no recovery codes -- the
    one account anybody evaluating this project signs in with was the one
    account that could not exercise recovery, and the admin screen showed it a
    flat `0 left` that read as a bug in the screen. Reported from a fresh macOS
    clone, in issue #1.
    """
    from app.auth.setup import RECOVERY_CODE_COUNT

    done, database = seeded
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        unused = db.execute(
            "select count(*) from recovery_codes where used_at is null"
        ).fetchone()[0]
        assert unused == RECOVERY_CODE_COUNT, f"{unused} codes, expected {RECOVERY_CODE_COUNT}"

        # Hashed, never stored in the clear -- the same rule as a password.
        stored = [r[0] for r in db.execute("select code_hash from recovery_codes")]
        assert all(h.startswith("$argon2") for h in stored), stored[:1]
        for code in stored:
            assert code not in done.stdout
    finally:
        db.close()

    # And printed, because a code nobody was told is the same as no code.
    assert "recovery codes" in done.stdout
    printed = [
        word
        for line in done.stdout.splitlines()
        if "recovery codes" in line or line.strip().startswith(("a", "b", "c", "d", "e", "f"))
        for word in line.split()
        if len(word) == 10 and all(c in "0123456789abcdef" for c in word)
    ]
    assert len(printed) >= 5, f"the codes were not printed legibly:\n{done.stdout}"


def test_the_seeded_instance_looks_like_a_finished_wizard(seeded):
    """Going through `setup` rather than alongside it means one definition of
    what a configured instance is, so the seed cannot drift from it again."""
    _, database = seeded
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        assert db.execute("select state from instance").fetchone()[0] == "configured"
        assert db.execute("select count(*) from batches where kind='setup'").fetchone()[0] == 1
        # The code that proved the pairing is spent, exactly as it is for a
        # human who finishes the wizard.
        assert db.execute("select totp_last_counter from users").fetchone()[0] is not None
    finally:
        db.close()

    # The setup token is consumed, so the seeded instance cannot be re-owned by
    # anybody who reads the data directory.
    assert not (database.parent / "setup-token").exists()


def test_the_seeded_opening_balance_is_marked_as_one(seeded):
    """Otherwise the demo's first month reports 2,500 of income it never had.

    `seed_demo` writes its opening balance by hand rather than through
    `accounts.create_account`, so it does not pick up the `system` mark that
    service applies -- and an unmarked opening-balance payee is, to every flow
    report, an ordinary payee who paid you the account's whole balance. Caught
    by opening the Income vs Expense report on the demo and drilling into the
    one figure that looked odd: 2,500 of "Uncategorised" income in month one,
    which turned out to be the Santander starting balance.

    This is the fixture people look at first, so it is the worst place to ship
    the exact defect `SystemPayee` exists to prevent.
    """
    _, database = seeded
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = db.execute(
            "select name, system from payees where name = 'Opening balance'"
        ).fetchall()
        assert rows, "the seed wrote no opening-balance payee at all"
        for name, system in rows:
            assert system == "opening_balance", (
                f"{name!r} is unmarked, so its transaction counts as income"
            )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# #226 -- `--reset` does not wipe somebody's ledger
# --------------------------------------------------------------------------- #


def _ledger_with(path: Path, *emails: str) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)")
        db.executemany("INSERT INTO users (email) VALUES (?)", [(one,) for one in emails])
        db.execute("CREATE TABLE households (id INTEGER PRIMARY KEY, name TEXT)")
        db.execute("INSERT INTO households (name) VALUES ('Real Household')")


def _seed(tmp_path: Path, database: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "SPENDTRACKER_DATA_DIR": str(tmp_path),
        "DATABASE_URL": f"sqlite:///{database}",
    }
    return subprocess.run(
        [sys.executable, "-m", "scripts.seed_demo", "--reset", "--months", "1", *args],
        cwd=REPO, env=env, capture_output=True, text=True, input=stdin, timeout=120,
    )


def _survivors(database: Path) -> tuple[list, list]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        return (
            [row[0] for row in db.execute("SELECT email FROM users ORDER BY id")],
            [row[0] for row in db.execute("SELECT name FROM households")],
        )


def test_reset_refuses_a_database_with_somebody_real_in_it(tmp_path):
    database = tmp_path / "real.sqlite3"
    _ledger_with(database, "demo@example.com", "owner@example.org")
    done = _seed(tmp_path, database)
    assert done.returncode != 0
    assert str(database.resolve()) in done.stdout
    assert "Refusing to wipe it" in done.stderr
    assert _survivors(database) == (["demo@example.com", "owner@example.org"], ["Real Household"])


def test_force_still_wants_the_path_typed_back(tmp_path):
    database = tmp_path / "real.sqlite3"
    _ledger_with(database, "owner@example.org")
    done = _seed(tmp_path, database, "--force", stdin="yes\n")
    assert done.returncode != 0
    assert "Nothing was changed" in done.stderr
    assert _survivors(database) == (["owner@example.org"], ["Real Household"])


def test_only_the_demo_owner_counts_as_nobody(tmp_path):
    from scripts import seed_demo

    database = tmp_path / "demo.sqlite3"
    _ledger_with(database, "demo@example.com", " Demo@Example.com ")
    assert seed_demo._people_not_the_demo(database) == 0
    assert seed_demo._people_not_the_demo(tmp_path / "absent.sqlite3") == 0
    assert not (tmp_path / "absent.sqlite3").exists()
    _ledger_with(tmp_path / "two.sqlite3", "demo@example.com", "someone@example.org")
    assert seed_demo._people_not_the_demo(tmp_path / "two.sqlite3") == 1
