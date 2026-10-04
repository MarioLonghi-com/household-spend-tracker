"""Migrations must describe the models, exactly.

The previous build had no migration tool at all -- ``create_all`` creates what
is missing and alters nothing that exists, so the next column added to a model
never reached a database that already had real data in it. This test is what
stops that from being possible: a model change without its migration fails here
rather than at query time, months later.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app.models import Base

ROOT = Path(__file__).resolve().parent.parent


def _config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_head_produces_exactly_the_models(tmp_path, monkeypatch):
    db = tmp_path / "migrated.sqlite3"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("DATABASE_URL", url)

    command.upgrade(_config(url), "head")

    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    engine.dispose()

    assert diff == [], f"the models and the migrations disagree:\n{diff}"


def test_downgrade_then_upgrade_is_clean(tmp_path, monkeypatch):
    db = tmp_path / "roundtrip.sqlite3"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _config(url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(url, future=True)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    engine.dispose()

    assert diff == []


def test_running_the_migrations_in_process_leaves_the_apps_loggers_on(tmp_path, monkeypatch):
    """`env.py`'s `fileConfig` used to disable every logger that already existed.

    Run in-process, that silenced `spendtracker.*` for the rest of the process,
    which is how a migration test made the log-stream tests fail on whatever
    xdist worker it shared with them.
    """
    import logging

    from alembic import command
    from alembic.config import Config

    ours = logging.getLogger("spendtracker.migration-guard")
    assert not ours.disabled
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'guard.sqlite3'}")
    command.upgrade(Config("alembic.ini"), "head")
    assert not ours.disabled
    assert not logging.getLogger("spendtracker").disabled


def test_the_migrations_have_exactly_one_head():
    """Two branches that each add a migration on the same parent merge cleanly
    in git and leave Alembic with two heads, and `upgrade head` refuses. Said
    here, at review, rather than by the first upgrade after the merge."""
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(_config("sqlite://")).get_heads()
    assert len(heads) == 1, f"more than one migration head: {sorted(heads)} -- re-point one down_revision"


# --------------------------------------------------------------------------- #
# 541128a33fd4: credentials a reset link cleared, across a rollback (#284)
# --------------------------------------------------------------------------- #

#: The revision before account reset links.
BEFORE_RESETS = "ef3af4e09c4a"
KNOWN = "a password somebody knows"


def _add_members(path: Path, *members: tuple[str, str | None, bytes | None]) -> None:
    import sqlite3

    with sqlite3.connect(path) as conn:
        for number, (email, password_hash, secret) in enumerate(members):
            conn.execute(
                "INSERT INTO users (id, email, email_canonical, display_name, password_hash, role,"
                " totp_secret, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'member', ?, datetime('now', ?), datetime('now'))",
                (f"user{number}", email, email, email, password_hash, secret, f"+{number} seconds"),
            )


def _members(path: Path) -> dict[str, tuple]:
    import sqlite3

    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT email, password_hash, totp_secret FROM users").fetchall()
    return {email: (hashed, None if secret is None else bytes(secret)) for email, hashed, secret in rows}


def test_a_password_a_reset_cleared_is_unknown_again_after_a_rollback_and_back(tmp_path, monkeypatch):
    """The older schema's NOT NULL gets a real argon2id hash nobody can match,
    checked at the same cost as any other; upgrading again puts the NULL back,
    so the next link still knows it has a password to set. The other member's
    hash goes through untouched both ways."""
    from app.auth import passwords

    db = tmp_path / "resets.sqlite3"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    known = passwords.hash_password(KNOWN)
    _add_members(
        db,
        ("cleared@example.com", None, b"sealed-one"),
        ("kept@example.com", known, b"sealed-two"),
    )
    before = _members(db)

    command.downgrade(cfg, BEFORE_RESETS)
    written, secret = _members(db)["cleared@example.com"]
    assert written.startswith("$argon2id$v=19$m=65536,t=3,p=4$") and secret == b"sealed-one"
    for guess in (KNOWN, "", written):
        assert not passwords.verify_password(written, guess)
    assert _members(db)["kept@example.com"] == (known, b"sealed-two")

    command.upgrade(cfg, "head")
    assert _members(db) == before


def test_rolling_back_says_which_accounts_it_leaves_shut_and_what_opens_them(tmp_path, monkeypatch):
    """`make upgrade-check` shows an operator this migration's declaration and
    nothing else about rolling it back. The downgrade leaves an account whose
    password a reset cleared with nothing on the older version that can open
    it -- `reset_authenticator` sets only the secret -- so the declaration, and
    the changelog entry beside it, have to say that, and not only the
    authenticator half."""
    from app.auth import passwords
    from scripts.upgrade import all_migrations

    db = tmp_path / "rollback.sqlite3"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    known = passwords.hash_password(KNOWN)
    _add_members(
        db,
        ("password@example.com", None, b"sealed-one"),
        ("authenticator@example.com", known, None),
        ("untouched@example.com", known, b"sealed-three"),
    )

    command.downgrade(cfg, BEFORE_RESETS)
    after = _members(db)
    # What the downgrade does: each credential a reset cleared becomes one
    # nothing opens, and the account a reset did not touch keeps both.
    cleared_password, kept_secret = after["password@example.com"]
    assert kept_secret == b"sealed-one" and not passwords.verify_password(cleared_password, KNOWN)
    assert after["authenticator@example.com"] == (known, b"")
    assert after["untouched@example.com"] == (known, b"sealed-three")

    declared = all_migrations()["541128a33fd4"].note
    changelog = (ROOT / "CHANGELOG.md").read_text()
    entry = changelog[changelog.index("- `541128a33fd4`") :].split("\n\n", 1)[0]
    for said in (declared, entry):
        flat = " ".join(said.replace("*", "").split())
        # And both say so: which accounts stay shut, what opens each, and
        # that the operator's tool alone does not open the first.
        assert "password a reset had cleared" in flat, said
        assert "backup is restored" in flat, said
        assert "reset_authenticator" in flat and "only if its password was not reset too" in flat, said
        assert "followed before rolling back" in flat, said


def test_an_authenticator_a_reset_cleared_is_cleared_again_after_a_rollback_and_back(tmp_path, monkeypatch):
    """The downgrade writes an empty secret where a reset left NULL. Upgrading
    again used to keep it, and every key check then blamed a `secret.key`
    that is right: the empty one is the first member's, usually the owner's,
    and that is the one a restore samples. Two members, and the key checked
    both ways, so the check is seen to still catch a wrong one."""
    import base64
    import secrets

    from app.auth import crypto, keycheck, passwords, totp
    from app.services import backup_bundle
    from scripts import restore

    db = tmp_path / "cleared.sqlite3"
    url = f"sqlite:///{db}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = _config(url)
    command.upgrade(cfg, "head")
    known = passwords.hash_password(KNOWN)
    # `_add_members` numbers its ids in order: the owner is user0, created first.
    sealed = crypto.seal_totp_secret(totp.new_secret(), user_id="user1")
    _add_members(db, ("owner@example.com", known, None), ("member@example.com", known, sealed))

    command.downgrade(cfg, BEFORE_RESETS)
    assert _members(db)["owner@example.com"] == (known, b"")

    command.upgrade(cfg, "head")
    assert _members(db) == {"owner@example.com": (known, None), "member@example.com": (known, sealed)}

    right = crypto.settings.secret_key
    assert keycheck.check(db, right) == keycheck.KeyCheck(enrolled=1, refused=())
    assert backup_bundle.a_sealed_secret(db) == ("user1", sealed)
    assert restore._key_verdict(db, right) == (True, "the key opens a real authenticator secret in it")

    wrong = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    assert keycheck.check(db, wrong).refused == ("member@example.com",)
    assert restore._key_verdict(db, wrong)[0] is False


# --------------------------------------------------------------------------- #
# What the code says about the schema
# --------------------------------------------------------------------------- #


def test_a_column_the_code_calls_not_null_is_not_null():
    """A docstring or comment that argues from "`table.column` is NOT NULL" is
    held to the column. `users.totp_secret` became nullable with #284, and two
    docstrings -- `AgentKey`'s security argument among them -- went on resting
    on its NOT NULL, the kind of doc a reader trusts when writing the next
    unguarded `open_totp_secret(user.totp_secret, ...)`."""
    import re

    claim = re.compile(
        r"`{1,2}(?P<table>[a-z_]+)\.(?P<column>[a-z_]+)`{1,2}(?P<between>[^`.;]{0,40}?)(?<!IS )NOT NULL"
    )
    checked, wrong = 0, []
    for path in sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]):
        flat = " ".join(path.read_text().replace("#:", " ").replace("#", " ").split())
        for found in claim.finditer(flat):
            table = Base.metadata.tables.get(found["table"])
            if table is None or found["column"] not in table.c:
                continue
            checked += 1
            if table.c[found["column"]].nullable:
                wrong.append(f"{path.relative_to(ROOT)}: {found[0]!r}")
    assert not wrong, "these say a column is NOT NULL, and it is nullable:\n  " + "\n  ".join(wrong)
    # `batches.actor_id` and `import_lines.raw` are both claimed and both true:
    # if the pattern silently stopped matching, the loop would pass by doing nothing.
    assert checked >= 2
