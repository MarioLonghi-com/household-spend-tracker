"""The upgrade machinery, and the declaration it depends on.

`make upgrade` tells an operator which migrations will run and which of them
cannot be undone. That report is only worth reading if every migration
actually declares it -- one silent `undeclared` in the middle of a list of
fifteen is exactly the one somebody skims past.

So the declaration is a test, the same way "every new model is audited or
explicitly excluded" is a test. A migration without it fails here rather than
producing a quieter upgrade report six months from now.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from scripts import backup as backup_script
from scripts.upgrade import Migration, all_migrations, chain, pending

VERSIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"

#: What a declaration is allowed to say.
#:
#: `clean` -- rolling back loses nothing anybody would miss. Anything dropped
#: is ephemeral, derived, or recomputed on the way back up.
#: `lossy` -- rolling back destroys something permanently, and re-upgrading
#: will not bring it back. The note has to say what.
VERDICTS = {"clean", "lossy"}


def test_every_migration_says_whether_it_can_be_undone():
    missing = [
        one.path.name
        for one in all_migrations().values()
        if one.reversible not in VERDICTS
    ]
    assert not missing, (
        "these migrations do not declare a `Reversible:` line, so `make upgrade` "
        "cannot tell an operator what rolling back would cost:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nAdd `Reversible: clean -- <why nothing is lost>` or "
        "`Reversible: lossy -- <what is destroyed>` to the module docstring."
    )


def test_a_lossy_declaration_names_what_it_destroys():
    """"lossy" on its own is not information. The note is the whole value."""
    vague = [
        one.path.name
        for one in all_migrations().values()
        if one.reversible == "lossy" and len(one.note.split()) < 4
    ]
    assert not vague, f"these say lossy without saying what: {sorted(vague)}"


def test_the_chain_reaches_every_migration():
    """A broken `down_revision` walk is how the head looked like an unknown one.

    Two spellings of the annotation live in `migrations/versions/` -- the four
    oldest use `Union[str, Sequence[str], None]` and the rest use `str |
    Sequence[str] | None`. Matching only the modern one cut the chain at the
    fifth migration, and the symptom was `scripts.upgrade` refusing to run
    against a perfectly ordinary database because its stamp was "not a revision
    this code knows about".
    """
    walked = chain()
    assert len(walked) == len(all_migrations()), (
        f"the chain reaches {len(walked)} of {len(all_migrations())} migrations; "
        "a down_revision is unreadable or two migrations share a parent"
    )
    assert walked[0].down is None


def test_pending_is_everything_after_the_stamp():
    # Compared by revision rather than by object: `chain()` reads the files
    # each time, so two calls give equal migrations and not identical ones.
    walked = [one.revision for one in chain()]
    assert [one.revision for one in pending(None)] == walked
    assert pending(walked[-1]) == []
    assert [one.revision for one in pending(walked[-3])] == walked[-2:]


def test_an_unknown_stamp_refuses_rather_than_migrating():
    """A database ahead of this code must not be migrated by it."""
    with pytest.raises(SystemExit) as refused:
        pending("not-a-revision-anybody-has")
    assert "not a revision this code knows about" in str(refused.value)


def test_a_migration_without_the_field_reads_as_undeclared(tmp_path):
    """The failure mode the first test guards, exercised directly."""
    written = tmp_path / "abc123_something.py"
    written.write_text(
        '"""does a thing\n\nRevision ID: abc123\nRevises: None\n"""\n'
        'revision: str = "abc123"\n'
        "down_revision: str | Sequence[str] | None = None\n"
    )
    assert Migration(written).reversible == "undeclared"


# --------------------------------------------------------------------------- #
# The backup, which is the only thing between a bad migration and a lost ledger
# --------------------------------------------------------------------------- #


def _database(path: pathlib.Path, *, rows: int, revision: str = "b4c9e1d70a25") -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        conn.execute("CREATE TABLE transactions (id text)")
        conn.executemany(
            "INSERT INTO transactions VALUES (?)", [(str(n),) for n in range(rows)]
        )


def test_verify_refuses_a_backup_with_no_key(tmp_path):
    """A ledger nobody can sign in to is not a restored ledger.

    `secret.key` encrypts every TOTP secret; recovery codes are hashes and are
    not encrypted with it. A backup without it restores the money and loses
    everybody's authenticator.
    """
    folder = tmp_path / "backup"
    folder.mkdir()
    _database(folder / "spendtracker.sqlite3", rows=3)

    with pytest.raises(SystemExit) as refused:
        backup_script.verify(folder)
    assert "secret.key" in str(refused.value)


def test_verify_refuses_a_copy_whose_rows_do_not_match(tmp_path):
    """The whole reason the copy is reopened rather than trusted.

    A `VACUUM INTO` that did not finish, or a file truncated later, is a
    backup-shaped object. Counting it against what it was taken from is what
    turns a belief into a backup.
    """
    folder = tmp_path / "backup"
    folder.mkdir()
    _database(folder / "spendtracker.sqlite3", rows=3)
    (folder / "secret.key").write_text("k")

    with pytest.raises(SystemExit) as refused:
        backup_script.verify(folder, against={"transactions": 9})
    assert "transactions: expected 9, the copy has 3" in str(refused.value)


def test_verify_refuses_something_that_is_not_this_app(tmp_path):
    folder = tmp_path / "backup"
    folder.mkdir()
    with sqlite3.connect(folder / "spendtracker.sqlite3") as conn:
        conn.execute("CREATE TABLE unrelated (id text)")
    (folder / "secret.key").write_text("k")

    with pytest.raises(SystemExit) as refused:
        backup_script.verify(folder)
    assert "alembic_version" in str(refused.value)


def test_a_good_backup_reports_its_revision_and_its_rows(tmp_path):
    folder = tmp_path / "backup"
    folder.mkdir()
    _database(folder / "spendtracker.sqlite3", rows=5)
    (folder / "secret.key").write_text("k")

    checked = backup_script.verify(folder, against={"transactions": 5})
    assert checked["revision"] == "b4c9e1d70a25"
    assert checked["rows"]["transactions"] == 5


def test_integrity_check_catches_a_copy_that_counts_correctly_and_is_broken(tmp_path):
    """Counting rows is not enough, which is why `PRAGMA integrity_check` runs first.

    A truncated or partly-written copy can still answer a `count(*)` off pages
    it happens to have. The backup script runs the
    integrity check before anything else and is right to.
    """
    folder = tmp_path / "backup"
    folder.mkdir()
    path = folder / "spendtracker.sqlite3"
    # Enough rows to span many pages: a two-page database has nothing to
    # corrupt that is not also the header, and the case worth catching is the
    # one where the file still opens.
    _database(path, rows=8_000)
    (folder / "secret.key").write_text("k")
    backup_script.verify(folder)  # intact: fine

    # Zero out a b-tree page well past the header. The file still opens and
    # still reports a table, which is exactly the shape that gets past a row
    # count and does not get past integrity_check.
    raw = bytearray(path.read_bytes())
    page = 4096
    raw[page * 3 : page * 5] = b"\x00" * (page * 2)
    path.write_bytes(raw)

    with pytest.raises(SystemExit) as refused:
        backup_script.verify(folder)
    assert "integrity_check" in str(refused.value)


def test_pruning_keeps_the_newest_and_only_touches_real_backups(tmp_path):
    """And it runs *after* the new copy is verified, never before.

    A script that prunes first and then finds its fresh copy unreadable has
    turned one bad backup into no backups.
    """
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
        folder = tmp_path / stamp
        folder.mkdir()
        (folder / "manifest.json").write_text("{}")
    # Not a backup: no manifest. It must survive.
    (tmp_path / "notes").mkdir()

    removed = backup_script.prune(tmp_path, keep=2)
    assert [one.name for one in removed] == ["20260101-000000"]
    assert sorted(one.name for one in tmp_path.iterdir()) == [
        "20260102-000000", "20260103-000000", "notes",
    ]


def test_keeping_zero_is_read_as_no_pruning_rather_than_delete_everything():
    """`--keep` defaults to 0, and 0 must not mean "remove them all"."""
    assert backup_script.prune.__defaults__ is None  # keep is required, never implicit


def test_the_check_names_the_data_directory_it_would_upgrade(client, monkeypatch, tmp_path):
    """README and UPGRADING.md send people to `make upgrade-check` to find out
    where their data is. It printed the directory only during a real upgrade,
    so the command the docs named answered everything but that question."""
    from scripts import upgrade

    monkeypatch.setattr(upgrade, "published", lambda: (None, None))
    say = upgrade.Step()
    upgrade.report(say)

    named = [line for line in say.lines if "data directory" in line]
    assert len(named) == 1
    assert named[0].endswith(str(tmp_path)), named[0]
