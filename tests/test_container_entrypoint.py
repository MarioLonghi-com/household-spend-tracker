"""The container entrypoint's guards.

Two bugs reached a real terminal on a Linux box before this file existed, and
neither was visible to `docker build`:

- Chainguard's `-dev` variant runs as **nonroot**, so `python -m venv /venv`
  died with `[Errno 13] Permission denied: '/venv'`.
- `/var/lib/spend-tracker` did not exist in the image, so Docker would have
  created the named volume **root-owned** and the first boot would have died
  writing `secret.key`.

The Dockerfile fixes are checked by CI actually *starting* the image against a
fresh named volume. What is tested here is the part that runs when it is
already wrong -- a volume created by an older image keeps its ownership for
ever, and no rebuild touches it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import re

import pytest

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

_SPEC = importlib.util.spec_from_file_location(
    "spendtracker_entrypoint",
    pathlib.Path(__file__).resolve().parent.parent / "deploy" / "entrypoint.py",
)
entrypoint = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(entrypoint)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path / "ledger"))
    return tmp_path / "ledger"


def test_a_writable_directory_is_no_complaint(data_dir):
    assert entrypoint._data_dir_is_writable() is None
    # And it made the directory, which is what a first run needs.
    assert data_dir.is_dir()


def test_it_leaves_nothing_behind_when_it_probes(data_dir):
    """The probe file must not become a file somebody has to explain."""
    entrypoint._data_dir_is_writable()
    assert list(data_dir.iterdir()) == []


def test_an_unwritable_directory_is_a_sentence_and_not_a_traceback(tmp_path, monkeypatch):
    """The failure this exists for.

    Uncaught, it is a `PermissionError` three frames into config import, out of
    `_load_or_create_secret_key`, and the cause -- a volume created root-owned
    by an older image -- is not in the traceback anywhere.
    """
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(locked / "ledger"))
    try:
        said = entrypoint._data_dir_is_writable()
    finally:
        locked.chmod(0o700)

    assert said is not None
    # The three things somebody needs: what is wrong, why a rebuild will not
    # fix it, and the two commands that will.
    assert "not writable" in said
    assert "keeps the ownership it was created with" in said
    assert "docker compose down -v" in said
    assert "--fix-ownership" in said


def test_fix_ownership_refuses_when_it_is_not_root(data_dir, capsys, monkeypatch):
    """Because `chown` as anybody else fails, and the fix is one flag away."""
    monkeypatch.setattr(os, "getuid", lambda: entrypoint.RUNS_AS)
    assert entrypoint.fix_ownership() == 1
    said = capsys.readouterr().err
    assert "needs root" in said
    assert "--user root" in said


def test_fix_ownership_is_reachable_from_the_command_line(data_dir, monkeypatch, capsys):
    """`docker compose run --rm --user root app --fix-ownership`.

    A flag on the entrypoint rather than the `chown` one-liner the message used
    to print, because **the runtime image has no `chown`** -- no shell and no
    coreutils is most of the point of the Chainguard base, so
    `--entrypoint chown` answers "executable file not found in $PATH" and the
    person following the instructions starts debugging the instructions.
    """
    monkeypatch.setattr(entrypoint.sys, "argv", ["entrypoint.py", "--fix-ownership"])
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    assert entrypoint.main() == 1  # not root, so it refuses -- but it got there
    assert "needs root" in capsys.readouterr().err


def test_the_uid_it_names_is_the_one_the_dockerfile_sets():
    """Two places say 65532 and they must not drift.

    The message tells somebody to chown to this number. If the Dockerfile's
    `USER` moved and this did not, the instructions would hand them a working
    command that fixes the ownership to the wrong owner.
    """
    dockerfile = (pathlib.Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    assert f"USER {entrypoint.RUNS_AS}:{entrypoint.RUNS_AS}" in dockerfile
    assert f"--chown={entrypoint.RUNS_AS}:{entrypoint.RUNS_AS}" in dockerfile


# --------------------------------------------------------------------------- #
# The documentation, which is the part that goes stale silently
# --------------------------------------------------------------------------- #

DOCKER_MD = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "DOCKER.md"
#: The runbooks whose commands are checked against the scripts' real flags.
RUNBOOKS = (DOCKER_MD, DOCKER_MD.parent / "TROUBLESHOOTING.md")


def _bash_blocks(text: str) -> list[str]:
    """Only the fenced ```bash blocks -- the lines somebody actually types.

    Prose is allowed to mention a command *in order to say it does not work*,
    which is exactly what the troubleshooting section does about `chown`.
    """
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def _help_text(main) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.suppress(SystemExit):
        main(["--help"])
    return buffer.getvalue()


def test_every_flag_the_docker_guide_tells_people_to_type_exists():
    """A wrong flag in a runbook is found at 23:00 by the person following it.

    Parsed out of the page rather than listed here, so a command added to it
    without a flag to back it is caught by the same assertion.
    """
    from scripts import backup, reset_account, restore, upgrade, version

    parsers = {
        "scripts.backup": backup.main,
        "scripts.reset_account": reset_account.main,
        "scripts.restore": restore.main,
        "scripts.upgrade": upgrade.main,
        "scripts.version": version.main,
    }
    helps = {name: _help_text(main) for name, main in parsers.items()}

    checked = 0
    for runbook in RUNBOOKS:
        for block in _bash_blocks(runbook.read_text()):
            # One logical command per entry, with line continuations folded back.
            for command in block.replace("\\\n", " ").splitlines():
                found = re.search(r"-m (scripts\.\w+)(.*)", command)
                if found is None or found[1] not in parsers:
                    continue
                for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]+", found[2]):
                    assert flag in helps[found[1]], (
                        f"deploy/{runbook.name} tells people to run `{found[1]} {flag}`, "
                        f"and {found[1]} has no such flag"
                    )
                    checked += 1
    # If the parsing silently stopped matching, the loop above passes by doing
    # nothing. This is what says it did something.
    assert checked >= 5, f"only {checked} flags checked; the guide or this parser has moved"


def test_the_guide_does_not_tell_anyone_to_chown_in_an_image_without_chown():
    """The first draft of the recovery instructions did exactly that.

    `--entrypoint chown` answers "executable file not found in $PATH": no shell
    and no coreutils is most of the point of the Chainguard runtime base. The
    prose is allowed to say so; a command block is not allowed to ask for it.
    """
    text = DOCKER_MD.read_text()
    for block in _bash_blocks(text):
        assert "--entrypoint chown" not in block
    assert "--fix-ownership" in text


def test_the_guide_passes_dash_T_wherever_it_captures_output():
    """Without it `docker compose run` discards everything in a non-TTY shell.

    The seed prints the demo password, the TOTP secret and ten recovery codes,
    and they are not recoverable afterwards. Measured: without `-T` the command
    exits 0 and prints nothing at all.

    `--fix-ownership` is the one exception, and deliberately: it prints two
    lines nobody needs to capture, and it is the command somebody runs by hand
    when they are already stuck.
    """
    for block in _bash_blocks(DOCKER_MD.read_text()):
        for command in block.replace("\\\n", " ").splitlines():
            if "docker compose run" in command and "--fix-ownership" not in command:
                assert "-T" in command, f"needs -T: {command.strip()}"


# --------------------------------------------------------------------------- #
# Where it listens
# --------------------------------------------------------------------------- #


def test_it_binds_every_interface_unless_told_otherwise(monkeypatch):
    """compose.yaml publishes `127.0.0.1:8848:8848`; that publish only reaches
    a listener on 0.0.0.0 inside the container, so that stays the default."""
    monkeypatch.delenv("SPENDTRACKER_HOST", raising=False)
    assert entrypoint.bind_host() == "0.0.0.0"
    monkeypatch.setenv("SPENDTRACKER_HOST", "   ")
    assert entrypoint.bind_host() == "0.0.0.0"


def test_a_tailscale_sidecar_can_keep_it_on_loopback(monkeypatch):
    """In a shared network namespace with a Tailscale sidecar there is no
    published port: the namespace *is* the tailnet node, and 0.0.0.0 would put
    plain HTTP on `<node>.ts.net:8848` beside the HTTPS on 443."""
    monkeypatch.setenv("SPENDTRACKER_HOST", " 127.0.0.1 ")
    assert entrypoint.bind_host() == "127.0.0.1"


def test_the_sidecar_compose_file_keeps_the_app_on_loopback():
    """The file people copy must set it, or the docstring above is a wish."""
    root = pathlib.Path(__file__).resolve().parent.parent
    compose = (root / "deploy" / "tailnet" / "compose.yaml").read_text()
    assert 'SPENDTRACKER_HOST: "127.0.0.1"' in compose
    # A real `ports:` key, not the comment that says there is none.
    assert re.search(r"^\s*ports:", compose, re.M) is None, "a published port would bypass the sidecar"
    assert "AllowFunnel" not in (root / "deploy" / "tailnet" / "serve.json").read_text()


def test_the_sidecar_checks_it_can_reach_the_app():
    """A restarted sidecar leaves the app in its old namespace: a 502 for
    every request while the app's own healthcheck stays green (#37). Only a
    check run *from the sidecar*, on the loopback `tailscale serve` proxies
    to, sees it."""
    root = pathlib.Path(__file__).resolve().parent.parent
    compose = (root / "deploy" / "tailnet" / "compose.yaml").read_text()
    sidecar = compose.split("\n  app:\n")[0].split("\n  tailscale:\n")[1]
    assert re.search(
        r"^    healthcheck:\n      test: .*http://127\.0\.0\.1:8848/api/health", sidecar, re.M
    ), "the sidecar's healthcheck must fetch the app on loopback"


# --------------------------------------------------------------------------- #
# The first start (#167, decision B2)
# --------------------------------------------------------------------------- #
#
# An empty database migrates without SPENDTRACKER_AUTO_MIGRATE, because there
# is nothing in it to lose and a double-click install has no terminal to pass
# the flag from. Every other mismatch still reaches `app/schema_check.py`
# unmigrated, which refuses to boot. Each case runs `main()` for real against
# its own data directory -- the real `alembic upgrade head` subprocess
# included -- and stops it at the `exec` of uvicorn.

ROOT = pathlib.Path(__file__).resolve().parent.parent
UNKNOWN_REVISION = "ffffffffffff"


class _Served(Exception):
    """Raised in place of `os.execvp`: the entrypoint got as far as serving."""


@pytest.fixture()
def first_start(tmp_path, monkeypatch):
    """A fresh data directory per test, and `main()` that stops at the exec."""
    where = tmp_path / "volume"
    where.mkdir()
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(where))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SPENDTRACKER_AUTO_MIGRATE", raising=False)
    monkeypatch.chdir(ROOT)  # where the image runs it from: alembic.ini, migrations/
    monkeypatch.setattr(entrypoint.sys, "argv", ["entrypoint.py"])

    def _exec(file, args):
        raise _Served(args)

    monkeypatch.setattr(entrypoint.os, "execvp", _exec)

    migrations = []
    real_migrate = entrypoint._migrate

    def _counting_migrate():
        migrations.append(True)
        return real_migrate()

    monkeypatch.setattr(entrypoint, "_migrate", _counting_migrate)

    def run():
        try:
            return entrypoint.main()
        except _Served:
            return "served"

    run.db = where / "spendtracker.sqlite3"
    run.migrations = migrations
    return run


def _alembic(db: pathlib.Path, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, revision)


def _inspect(db: pathlib.Path) -> tuple[str | None, set[str]]:
    """(stamp, table names) as the app's own guard reads them."""
    from sqlalchemy import create_engine, inspect

    from app import schema_check

    engine = create_engine(f"sqlite:///{db}")
    try:
        return schema_check.stamped(engine), set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _refusal(db: pathlib.Path) -> str:
    """What `lifespan` would say about this file at boot.

    Asked of a copy: once `app.db` is imported, every engine's connect hook
    sets `journal_mode=WAL`, which rewrites two header bytes -- the app's doing
    when it boots, not the entrypoint's, and not what these tests measure.
    """
    import shutil

    from sqlalchemy import create_engine

    from app import schema_check

    copy = db.with_name("refusal-copy.sqlite3")
    shutil.copyfile(db, copy)
    engine = create_engine(f"sqlite:///{copy}")
    try:
        with pytest.raises(schema_check.SchemaOutOfDate) as refused:
            schema_check.verify(engine)
    finally:
        engine.dispose()
    return str(refused.value)


def _model_tables() -> set[str]:
    from app.models import Base

    return set(Base.metadata.tables)


@pytest.mark.parametrize("left_by", ["nothing", "a zero-byte file", "a refused start"])
def test_an_empty_volume_is_migrated_without_the_flag(first_start, capsys, left_by):
    """D6, first half. A brand-new volume has no file at all; a first start
    that died early can leave a zero-byte one; and a start that the old
    entrypoint left to `schema_check` left one in WAL mode with no tables.
    All three are nothing to lose."""
    import sqlite3

    from app import schema_check

    if left_by == "a zero-byte file":
        first_start.db.touch()
    if left_by == "a refused start":
        with contextlib.closing(sqlite3.connect(first_start.db)) as db:
            db.execute("pragma journal_mode=WAL")
    assert entrypoint.database_is_empty() is True

    assert first_start() == "served"

    stamp, tables = _inspect(first_start.db)
    assert stamp == schema_check.expected_head()
    assert _model_tables() <= tables
    assert "alembic_version" in tables
    assert first_start.migrations == [True]
    said = capsys.readouterr().out
    assert "had no tables at all" in said
    assert "without SPENDTRACKER_AUTO_MIGRATE" in said


@pytest.mark.parametrize("first_table", ["notes", "households"])
def test_tables_without_a_stamp_are_refused_and_left_alone(first_start, first_table):
    """D6. One table and no `alembic_version` is somebody's data, built by
    something other than the migrations. Not migrated; the guard refuses it."""
    import sqlite3

    with contextlib.closing(sqlite3.connect(first_start.db)) as db:
        db.execute(f"create table {first_table} (id integer primary key, body text)")
        db.execute(f"insert into {first_table} (body) values ('keep me'), ('and me')")
        db.commit()
    before = first_start.db.read_bytes()
    assert entrypoint.database_is_empty() is False

    assert first_start() == "served"  # the app's lifespan is what refuses

    assert first_start.migrations == []
    assert first_start.db.read_bytes() == before
    assert "tables but no Alembic stamp" in _refusal(first_start.db)
    assert first_start.db.read_bytes() == before


@pytest.mark.parametrize("revision", [UNKNOWN_REVISION, "000000000000"])
def test_a_stamp_this_code_does_not_know_is_refused_and_left_alone(first_start, revision):
    """D6. Most likely a database from a newer build: never migrated here."""
    import sqlite3

    with contextlib.closing(sqlite3.connect(first_start.db)) as db:
        db.execute("create table alembic_version (version_num varchar(32) primary key)")
        db.execute("insert into alembic_version values (?)", (revision,))
        db.commit()
    before = first_start.db.read_bytes()
    assert entrypoint.database_is_empty() is False

    assert first_start() == "served"

    assert first_start.migrations == []
    assert first_start.db.read_bytes() == before
    assert f"stamped {revision}, which is not a revision this code knows" in _refusal(
        first_start.db
    )
    assert first_start.db.read_bytes() == before


def test_a_database_at_head_starts_without_migrating(first_start, capsys):
    from app import schema_check

    _alembic(first_start.db, "head")
    before = first_start.db.read_bytes()

    assert first_start() == "served"

    assert first_start.migrations == []
    assert first_start.db.read_bytes() == before
    assert _inspect(first_start.db)[0] == schema_check.expected_head()
    assert "had no tables at all" not in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["1", "yes"])
def test_the_flag_still_migrates_a_ledger_behind_head(first_start, monkeypatch, flag):
    """`SPENDTRACKER_AUTO_MIGRATE=1` is unchanged: a deliberate migration of an
    existing ledger, which is not empty and would otherwise be refused."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app import schema_check

    head = schema_check.expected_head()
    behind = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_revision(
        head
    ).down_revision
    _alembic(first_start.db, behind)
    assert _inspect(first_start.db)[0] == behind
    assert entrypoint.database_is_empty() is False
    monkeypatch.setenv("SPENDTRACKER_AUTO_MIGRATE", flag)

    assert first_start() == "served"

    assert first_start.migrations == [True]
    assert _inspect(first_start.db)[0] == head


def test_a_lone_wal_is_not_an_empty_database(first_start):
    """A `-wal` with no database beside it is data in an odd state, not nothing."""
    first_start.db.with_name(first_start.db.name + "-wal").write_bytes(b"not nothing")
    assert entrypoint.database_is_empty() is False
    assert first_start() == "served"
    assert first_start.migrations == []
    assert not first_start.db.exists()


def test_a_database_that_is_not_sqlite_is_never_migrated_unasked(first_start, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://somewhere/ledger")
    assert entrypoint.database_path() is None
    assert entrypoint.database_is_empty() is False
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{first_start.db}?timeout=5")
    assert entrypoint.database_path() == first_start.db
    assert entrypoint.database_is_empty() is True
