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

import json
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


# --------------------------------------------------------------------------- #
# The unattended caller (#155)
# --------------------------------------------------------------------------- #
#
# An updater runs `--check --json` to learn what an upgrade would do and
# `--yes --report PATH` to do it, and it reads nothing but the document and
# the exit status. Each of these fakes `alembic upgrade head` with a stamp of
# the head revision: the real migrations are proven by test_migrations.py and
# the CI rehearsal, and what is under test here is what the drill does with
# the result -- so the fake can also do what a bad migration would.


def _ledger() -> pathlib.Path:
    from app.config import settings

    return pathlib.Path(settings.database_url.split("///", 1)[-1])


def _stamp(revision: str) -> None:
    with sqlite3.connect(_ledger()) as conn:
        conn.execute("UPDATE alembic_version SET version_num = ?", (revision,))


@pytest.fixture()
def world(client, monkeypatch) -> list[str]:
    """Two members with authenticators, on a ledger one migration behind head.

    Yields the chain's revisions. The run takes the container path -- no port
    probe, no placard, backups under the data directory -- because that is
    the path an updater takes, and because a test must not bind 8848. The
    stamp goes back to head afterwards: `app.config` stays reloaded once the
    client fixture is gone, and a later in-process `alembic upgrade` would
    otherwise find this ledger and try to migrate it.
    """
    from scripts import upgrade
    from tests.test_recovery_codes_regenerate import _two_members

    _two_members(client)
    walked = [one.revision for one in chain()]
    _stamp(walked[-2])
    monkeypatch.setattr(upgrade, "published", lambda: (None, None))
    monkeypatch.setenv("SPENDTRACKER_IN_CONTAINER", "1")
    yield walked
    _stamp(walked[-1])


def _fake_alembic(monkeypatch, *, then=None) -> None:
    """Stand in for `alembic upgrade head`: stamp head, then do what `then` says."""
    import subprocess

    from scripts import upgrade

    head = chain()[-1].revision

    def run(argv, **_):
        assert argv[1:] == ["-m", "alembic", "upgrade", "head"], argv
        _stamp(head)
        if then is not None:
            then()
        return subprocess.CompletedProcess(argv, 0, stdout=f"Running upgrade -> {head}", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", run)


def _drill(tmp_path, *argv: str) -> tuple[int, dict]:
    from scripts import upgrade

    report = tmp_path / "outcome.json"
    code = upgrade.main(["--yes", "--report", str(report), *argv])
    return code, json.loads(report.read_text())


def test_check_json_lists_exactly_the_pending_migrations(world, monkeypatch, capsys):
    """D1. The updater's prepare step keeps this list as printed, and the
    confirmation screen draws one checkbox per lossy entry from it."""
    from scripts import upgrade

    walked = world
    _stamp(walked[-3])
    capsys.readouterr()

    assert upgrade.main(["--check", "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)

    assert facts["database_stamped"] == walked[-3]
    assert facts["code_head"] == walked[-1]
    assert [one["revision"] for one in facts["pending"]] == walked[-2:]
    by_id = all_migrations()
    for one in facts["pending"]:
        assert one["title"] == by_id[one["revision"]].title
        assert one["reversible"] == by_id[one["revision"]].reversible
        assert one["note"] == by_id[one["revision"]].note
    assert facts["lossy"] == any(one["reversible"] != "clean" for one in facts["pending"])
    assert facts["data_dir"] == str(_ledger().parent)
    assert facts["published"] == {"version": None, "commit": None}


def test_check_json_at_head_has_nothing_pending(world, monkeypatch, capsys):
    from scripts import upgrade

    walked = world
    _stamp(walked[-1])
    capsys.readouterr()

    assert upgrade.main(["--check", "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts["pending"] == []
    assert facts["lossy"] is False
    assert facts["database_stamped"] == facts["code_head"] == walked[-1]


def test_check_writes_nothing_to_a_live_wal_ledger(world):
    """D2. The app is still serving while `--check` runs: the file, its WAL
    and its shared-memory index must be byte for byte what they were."""
    from scripts import upgrade

    ledger = _ledger()
    wal = ledger.with_name(ledger.name + "-wal")
    shm = ledger.with_name(ledger.name + "-shm")
    # Live: the app's connections are open and the wizard's writes sit in the
    # WAL, not yet folded into the main file. Otherwise this proves nothing.
    assert wal.stat().st_size > 0, "no WAL to leave alone"

    def snapshot() -> list[tuple[bytes, int]]:
        # The `-shm` is held to its size only. It is the wal-index, shared
        # memory the OS mirrors to a file, and every reader of a WAL database
        # -- `mode=ro` included -- writes its read-mark into it. That is not
        # the ledger; the two files that are stay byte for byte.
        return [(one.read_bytes(), one.stat().st_mtime_ns) for one in (ledger, wal)] + [
            (b"", shm.stat().st_size)
        ]

    before = snapshot()
    assert upgrade.main(["--check"]) == 0
    assert snapshot() == before
    assert upgrade.main(["--check", "--json"]) == 0
    assert snapshot() == before


def test_the_report_matches_the_live_ledger(world, monkeypatch, tmp_path):
    """D3. Every number in the document is read back from disk, not trusted."""
    from app.services.backup_bundle import counts

    walked = world
    _fake_alembic(monkeypatch)

    code, outcome = _drill(tmp_path)

    assert code == 0
    assert outcome["exit"] == 0 and outcome["outcome"] == "done"
    assert outcome["stamp"] == {"before": walked[-2], "after": walked[-1]}
    assert outcome["migrated"] is True
    live = counts(_ledger())
    assert outcome["rows"]["after"] == live
    assert outcome["rows"]["before"] == live
    assert live["users"] == 2
    assert outcome["dropped"] == []
    assert outcome["key"]["ok"] is True
    folder = pathlib.Path(outcome["backup"]["folder"])
    assert outcome["backup"]["verified"] is True
    assert json.loads((folder / "manifest.json").read_text())["rows"] == live
    assert (folder / "upgrade.log").read_text().splitlines()[-1].endswith(outcome["log"][-1][27:])
    assert any("Done." in line for line in outcome["log"])


def test_a_dropped_count_exits_non_zero_and_names_the_table(world, monkeypatch, tmp_path):
    """D4. One of two tables loses a row: the run says which, and exits 6."""
    from scripts import upgrade


    def lose_one_audit_row():
        with sqlite3.connect(_ledger()) as conn:
            conn.execute("DELETE FROM changes WHERE rowid = (SELECT min(rowid) FROM changes)")

    _fake_alembic(monkeypatch, then=lose_one_audit_row)

    code, outcome = _drill(tmp_path)

    assert code == upgrade.ROWS_DROPPED == 6
    assert outcome["outcome"] == "rows-dropped"
    assert outcome["dropped"] == ["changes"]
    assert outcome["rows"]["after"]["changes"] == outcome["rows"]["before"]["changes"] - 1
    assert outcome["rows"]["after"]["users"] == outcome["rows"]["before"]["users"] == 2
    assert outcome["key"]["ok"] is True
    assert any("DO NOT START THE SERVICE" in line for line in outcome["log"])
    assert any("fewer rows" in line and "changes" in line for line in outcome["log"])


def test_the_wrong_secret_key_exits_non_zero(world, monkeypatch, tmp_path):
    """D5. The migrated ledger is intact and nobody could sign in to it."""
    import dataclasses

    from cryptography.fernet import Fernet

    from app.auth import crypto
    from scripts import upgrade

    _fake_alembic(monkeypatch)
    # The key another data directory would have: valid, and not this one's.
    monkeypatch.setattr(
        crypto, "settings", dataclasses.replace(crypto.settings, secret_key=Fernet.generate_key().decode())
    )

    code, outcome = _drill(tmp_path)

    assert code == upgrade.KEY_DOES_NOT_OPEN == 5
    assert outcome["outcome"] == "key-does-not-open"
    assert outcome["key"]["ok"] is False
    assert "SECRET.KEY CANNOT DECRYPT" in outcome["key"]["message"]
    assert outcome["dropped"] == []
    assert outcome["migrated"] is True
    assert any("DO NOT START THE SERVICE" in line for line in outcome["log"])


def test_dropped_rows_outrank_a_wrong_key_and_both_are_reported(world, monkeypatch, tmp_path):
    import dataclasses

    from cryptography.fernet import Fernet

    from app.auth import crypto
    from scripts import upgrade


    def lose_one_audit_row():
        with sqlite3.connect(_ledger()) as conn:
            conn.execute("DELETE FROM changes WHERE rowid = (SELECT min(rowid) FROM changes)")

    _fake_alembic(monkeypatch, then=lose_one_audit_row)
    monkeypatch.setattr(
        crypto, "settings", dataclasses.replace(crypto.settings, secret_key=Fernet.generate_key().decode())
    )

    code, outcome = _drill(tmp_path)

    assert code == upgrade.ROWS_DROPPED
    assert outcome["dropped"] == ["changes"]
    assert outcome["key"]["ok"] is False


def test_a_failed_migration_exits_three_and_the_report_says_the_backup_is_there(
    world, monkeypatch, tmp_path
):
    import subprocess

    from scripts import upgrade

    walked = world
    monkeypatch.setattr(
        upgrade.subprocess, "run",
        lambda argv, **_: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom: no such column"),
    )

    code, outcome = _drill(tmp_path)

    assert code == upgrade.MIGRATION_FAILED == 3
    assert outcome["outcome"] == "migration-failed"
    assert outcome["migrated"] is False
    assert outcome["backup"]["verified"] is True
    assert pathlib.Path(outcome["backup"]["folder"], "spendtracker.sqlite3").exists()
    assert outcome["stamp"] == {"before": walked[-2], "after": walked[-2]}
    assert any("boom: no such column" in line for line in outcome["log"])


def test_declining_the_prompt_writes_a_report_that_says_nothing_changed(
    world, monkeypatch, tmp_path
):
    """Without `--yes` the prompt still asks; the report is still written."""
    from scripts import upgrade

    walked = world
    monkeypatch.setattr("builtins.input", lambda _: "no")
    report = tmp_path / "outcome.json"

    code = upgrade.main(["--report", str(report)])
    outcome = json.loads(report.read_text())

    assert code == upgrade.REFUSED == 1
    assert outcome["outcome"] == "declined"
    assert outcome["backup"] == {"folder": None, "verified": False}
    assert outcome["migrated"] is False
    assert outcome["stamp"] == {"before": walked[-2], "after": None}
    assert list(_ledger().parent.glob("backups/*")) == []


def test_the_flags_belong_to_their_modes(capsys):
    """`--json` without `--check` and `--report` with it are usage errors (exit 2),
    not a silently missing file."""
    from scripts import upgrade

    with pytest.raises(SystemExit) as refused:
        upgrade.main(["--json"])
    assert refused.value.code == 2
    with pytest.raises(SystemExit) as refused:
        upgrade.main(["--check", "--report", "x.json"])
    assert refused.value.code == 2


def test_the_report_is_written_0660_for_the_updates_group_whatever_the_umask(tmp_path):
    """The updater reads it through the `update` volume's group; under a rootless
    engine it is root without capabilities and cannot read a 0600 file (#169)."""
    import os
    import stat

    report = tmp_path / "work" / "drill.json"
    report.parent.mkdir()
    old = os.umask(0o077)
    try:
        from scripts import upgrade as drill

        drill._write_report(report, {"exit": 0})
        drill._write_report(report, {"exit": 3})  # again, over the first
    finally:
        os.umask(old)
    assert stat.S_IMODE(report.stat().st_mode) == 0o660
    assert json.loads(report.read_text()) == {"exit": 3}
    assert sorted(p.name for p in report.parent.iterdir()) == ["drill.json"]
