"""A backup you can carry away (#133): download, delete, and restore from the zip.

What is asserted is what each operation did to the files and to the ledger,
never that a route answered:

* the zip's database **is** the ledger -- opened with sqlite3, its rows counted
  against the live database and against its own manifest, its SHA-256 checked;
* `secret.key` is in the zip exactly when it was asked for, and is the key;
* the README tells a stranger what the file is and how to restore it, in
  commands that exist;
* deleting one of two backups removes that file and leaves the other one
  byte-for-byte as it was;
* a name that is not in the listing reaches nothing, whatever it spells;
* the zip made here restores -- with its key, without one onto the instance it
  came from, and not at all with the wrong key unless told to.

Two of everything: two households with rows in both, two backups.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import secrets
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pyotp
import pytest

from tests.conftest import HEADERS, PASSWORD, _setup_owner

BACKUPS = "/api/admin/application/backups"


@pytest.fixture(autouse=True)
def _one_key(monkeypatch, client):
    """Seal with the key on disk, as a real instance does.

    `app.auth.crypto` binds `settings` at import, and the suite reloads
    `app.config` per test -- so without this, the owner's authenticator is
    sealed with whichever test's key came first while `secret.key` on disk is
    this test's. A restore test checks exactly that the two agree, so here
    they have to.
    """
    from app import config
    from app.auth import crypto

    monkeypatch.setattr(crypto, "settings", config.settings)


def _two_households_with_rows(client) -> None:
    for name, currency, amounts in (
        ("Ours", "EUR", (-1250, 90000)),
        ("Theirs", "GBP", (-4400, 1500, -99)),
    ):
        house = client.post("/api/households", json={"name": name}, headers=HEADERS).json()
        account = client.post(
            f"/api/households/{house['id']}/accounts",
            json={"name": f"{name} checking", "type": "checking", "currency": currency},
            headers=HEADERS,
        )
        assert account.status_code == 201, account.text
        for day, amount in enumerate(amounts, start=4):
            made = client.post(
                f"/api/households/{house['id']}/transactions",
                json={
                    "account_id": account.json()["id"],
                    "date": f"2026-01-{day:02d}",
                    "amount": amount,
                },
                headers=HEADERS,
            )
            assert made.status_code == 201, made.text


def _backup(client) -> dict:
    made = client.post(BACKUPS, headers=HEADERS)
    assert made.status_code == 201, made.text
    return made.json()


def _two_backups(client) -> tuple[dict, dict]:
    first = _backup(client)
    # Named to the second: two in one second would collide, which is the
    # platform's rule and not what is under test here. So the second one is
    # named a second later rather than waited for -- three tests used to spend
    # a real second each on this sleep.
    from app.services import platform as platform_service

    class _OneSecondLater(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=1)

    client.post("/api/households", json={"name": "Between"}, headers=HEADERS)
    with mock.patch.object(platform_service, "datetime", _OneSecondLater):
        second = _backup(client)
    assert first["name"] != second["name"]
    return first, second


def _zip_from(answer, name: str) -> zipfile.ZipFile:
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"] == "application/zip"
    assert f'{name.removesuffix(".sqlite3")}.zip' in answer.headers["content-disposition"]
    assert answer.headers["cache-control"] == "no-store"
    return zipfile.ZipFile(io.BytesIO(answer.content))


def _download(client, name: str) -> zipfile.ZipFile:
    """The plain link: a GET, which can never carry the key."""
    return _zip_from(client.get(f"{BACKUPS}/{name}/download", headers=HEADERS), name)


def _grant(client, world: dict, *, steps_ahead: int = 1) -> str:
    """Both factors, now. The setup wizard spent the current step's code, so
    this uses the next one, which the one-step drift window accepts."""
    code = pyotp.TOTP(world["secret"]).at(int(time.time()) + 30 * steps_ahead)
    answer = client.post(
        "/api/me/step-up", json={"password": PASSWORD, "code": code}, headers=HEADERS
    )
    assert answer.status_code == 200, answer.text
    return answer.json()["token"]


def _post_download(client, name: str, **body):
    return client.post(f"{BACKUPS}/{name}/download", json=body, headers=HEADERS)


def _download_with_key(client, name: str, world: dict) -> zipfile.ZipFile:
    token = _grant(client, world)
    return _zip_from(
        _post_download(client, name, include_key=True, step_up_token=token), name
    )


def _rows(path: Path, table: str) -> int:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _data_dir(client) -> Path:
    from app import config

    return config.settings.data_dir


# --------------------------------------------------------------------------- #
# The zip
# --------------------------------------------------------------------------- #


def test_the_zip_holds_the_ledger_its_manifest_and_a_readme_and_no_key(client, tmp_path):
    _setup_owner(client)
    _two_households_with_rows(client)
    first, second = _two_backups(client)

    archive = _download(client, first["name"])
    folder = first["name"].removesuffix(".sqlite3")
    assert sorted(archive.namelist()) == [
        f"{folder}/README.txt",
        f"{folder}/manifest.json",
        f"{folder}/spendtracker.sqlite3",
    ], "no secret.key unless it was asked for"

    archive.extractall(tmp_path / "out")
    database = tmp_path / "out" / folder / "spendtracker.sqlite3"
    manifest = json.loads((tmp_path / "out" / folder / "manifest.json").read_text())

    # The file in the zip is the backup, byte for byte, and says so.
    assert _sha(database) == manifest["database_sha256"] == _sha(Path(first["path"]))
    # And the backup is the ledger as it was: both households' rows, all five.
    assert _rows(database, "transactions") == manifest["rows"]["transactions"] == 5
    assert _rows(database, "households") == manifest["rows"]["households"] == 2
    assert manifest["secret_key"] is False
    assert manifest["alembic_revision"]

    # The other backup is its own file, with the household made in between.
    other = _download(client, second["name"])
    other_manifest = json.loads(
        other.read(f"{second['name'].removesuffix('.sqlite3')}/manifest.json")
    )
    assert other_manifest["rows"]["households"] == 3
    assert other_manifest["database_sha256"] != manifest["database_sha256"]


def test_the_readme_says_what_it_is_how_to_open_it_and_how_to_restore_it(client):
    from app.services import platform
    from scripts import restore

    world = _setup_owner(client)
    made = _backup(client)
    folder = made["name"].removesuffix(".sqlite3")

    without = _download(client, made["name"]).read(f"{folder}/README.txt").decode()
    assert platform.REPOSITORY in without
    assert "SQLite" in without and "sqlitebrowser.org" in without
    assert "smallest unit" in without, "the future reader has to know 1250 is 12.50"
    assert "make restore FROM=" in without
    assert "KEY=" in without and "is NOT in this folder" in without
    assert "make migrate" in without

    with_key = _download_with_key(client, made["name"], world)
    readme = with_key.read(f"{folder}/README.txt").decode()
    assert "secret.key IS in this folder" in readme
    assert "KEY=" not in readme

    # The command it gives is one the script takes. `KEY=` is the Makefile's
    # spelling of --key; a README naming a flag nobody wrote is a runbook
    # found wrong at 23:00.
    helped = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(helped), pytest.raises(SystemExit):
        restore.main(["--help"])
    assert "--key" in helped.getvalue()
    makefile = (Path(__file__).resolve().parent.parent / "Makefile").read_text()
    assert '$(if $(KEY),--key "$(KEY)")' in makefile


def test_the_key_is_in_the_zip_only_when_asked_and_it_is_this_instances_key(client):
    from app import config

    world = _setup_owner(client)
    made = _backup(client)
    folder = made["name"].removesuffix(".sqlite3")

    archive = _download_with_key(client, made["name"], world)
    assert f"{folder}/secret.key" in archive.namelist()
    assert archive.read(f"{folder}/secret.key").decode().strip() == config.settings.secret_key
    manifest = json.loads(archive.read(f"{folder}/manifest.json"))
    assert manifest["secret_key"] is True

    plain = _zip_from(_post_download(client, made["name"], include_key=False), made["name"])
    assert f"{folder}/secret.key" not in plain.namelist()


def _grants_left(client) -> int:
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.models import StepUpGrant

    with Session(client.app_module.db_engine) as own:
        return own.execute(select(func.count()).select_from(StepUpGrant)).scalar_one()


def test_the_key_needs_both_factors_and_not_just_the_cookie(client):
    """#204: a lifted owner cookie used to be worth every member's second factor."""
    world = _setup_owner(client)
    made = _backup(client)
    folder = made["name"].removesuffix(".sqlite3")
    backups = Path(made["path"]).parent
    before = sorted(path.name for path in backups.iterdir())

    # Nothing but the session: refused, and no zip was even started.
    bare = _post_download(client, made["name"], include_key=True)
    assert bare.status_code == 401, bare.text
    made_up = _post_download(client, made["name"], include_key=True, step_up_token="made-up")
    assert made_up.status_code == 401, made_up.text
    # A GET cannot carry the key at all, and says so rather than quietly
    # handing over a zip without the key it asked for.
    old_link = client.get(
        f"{BACKUPS}/{made['name']}/download", params={"include_key": "true"}, headers=HEADERS
    )
    assert old_link.status_code == 422, old_link.text
    assert sorted(path.name for path in backups.iterdir()) == before

    # With a fresh grant: the key is in the zip, and the grant is gone.
    token = _grant(client, world)
    assert _grants_left(client) == 1
    archive = _zip_from(
        _post_download(client, made["name"], include_key=True, step_up_token=token), made["name"]
    )
    assert f"{folder}/secret.key" in archive.namelist()
    assert _grants_left(client) == 0

    # The same grant twice: one grant, one download.
    again = _post_download(client, made["name"], include_key=True, step_up_token=token)
    assert again.status_code == 401, again.text


def test_a_download_without_the_key_needs_no_grant_either_way(client):
    _setup_owner(client)
    made = _backup(client)
    folder = made["name"].removesuffix(".sqlite3")
    posted = _zip_from(_post_download(client, made["name"]), made["name"])
    assert f"{folder}/secret.key" not in posted.namelist()
    assert f"{folder}/secret.key" not in _download(client, made["name"]).namelist()


def test_the_zip_is_built_privately_and_removed_once_it_has_been_sent(client):
    """A full copy of the ledger, perhaps with its key, must not stay behind."""
    world = _setup_owner(client)
    made = _backup(client)
    backups = Path(made["path"]).parent
    before = sorted(path.name for path in backups.iterdir())

    _download_with_key(client, made["name"], world)

    assert sorted(path.name for path in backups.iterdir()) == before
    assert not list(backups.glob(".download-*"))


def test_a_backup_that_is_not_a_database_is_refused_rather_than_handed_out(client):
    _setup_owner(client)
    made = _backup(client)
    broken = Path(made["path"]).with_name("spendtracker-20200101T000000Z.sqlite3")
    broken.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)

    answer = client.get(f"{BACKUPS}/{broken.name}/download", headers=HEADERS)
    assert answer.status_code == 422, answer.text
    assert broken.name in answer.json()["detail"]
    assert not list(broken.parent.glob(".download-*")), "the refused zip is not left behind"

    # And the good one beside it still downloads.
    _download(client, made["name"])


@pytest.mark.parametrize(
    "name",
    [
        "../secret.key",
        "..%2Fsecret.key",
        "api.sqlite3",  # the live ledger's own name, which is not a backup
        "spendtracker-20990101T000000Z.sqlite3",
        "%2Fetc%2Fpasswd",
    ],
)
def test_a_name_not_in_the_listing_reaches_nothing(client, name):
    _setup_owner(client)
    made = _backup(client)
    live = _data_dir(client) / "api.sqlite3"
    live_before = live.stat().st_size

    # 404 from the route when the name reaches it. A name with `..` in it does
    # not: the client normalises the path before sending, so it lands on some
    # other path entirely -- the SPA's catch-all, when the client is built,
    # which answers a DELETE with 405. Either way it is refused, and what is
    # asserted below is the part that matters: nothing was served or touched.
    fetched = client.get(f"{BACKUPS}/{name}/download", headers=HEADERS)
    assert fetched.status_code in (404, 405), fetched.text
    assert fetched.headers.get("content-type") != "application/zip"
    removed = client.delete(f"{BACKUPS}/{name}", headers=HEADERS)
    assert removed.status_code in (404, 405), removed.text

    assert live.exists() and live.stat().st_size == live_before
    assert (_data_dir(client) / "secret.key").exists()
    assert Path(made["path"]).exists()


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


def test_deleting_one_of_two_backups_removes_it_and_leaves_the_other_untouched(client):
    _setup_owner(client)
    _two_households_with_rows(client)
    first, second = _two_backups(client)
    kept_sha = _sha(Path(second["path"]))

    gone = client.delete(f"{BACKUPS}/{first['name']}", headers=HEADERS)
    assert gone.status_code == 204, gone.text

    assert not Path(first["path"]).exists()
    assert _sha(Path(second["path"])) == kept_sha
    listed = client.get(BACKUPS, headers=HEADERS).json()
    assert [one["name"] for one in listed] == [second["name"]]

    # Twice is not twice: the second one finds nothing.
    assert client.delete(f"{BACKUPS}/{first['name']}", headers=HEADERS).status_code == 404


def test_the_last_backup_left_can_be_deleted(client):
    """Decided 2026-09-25 (#133, question C): the screen warns, the server allows."""
    _setup_owner(client)
    only = _backup(client)

    assert client.delete(f"{BACKUPS}/{only['name']}", headers=HEADERS).status_code == 204
    assert client.get(BACKUPS, headers=HEADERS).json() == []
    assert client.get("/api/admin/application", headers=HEADERS).json()["latest_backup"] is None


# --------------------------------------------------------------------------- #
# Restore, from what the screen hands out
# --------------------------------------------------------------------------- #


def _save(archive: zipfile.ZipFile, path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as out:
        for info in archive.infolist():
            out.writestr(info, archive.read(info))
    return path


def _change_the_live_ledger(client) -> None:
    client.post("/api/households", json={"name": "After the backup"}, headers=HEADERS)


def test_the_zip_with_its_key_restores_the_ledger_it_was_taken_from(client, tmp_path):
    from scripts import restore

    world = _setup_owner(client)
    _two_households_with_rows(client)
    made = _backup(client)
    zipped = _save(_download_with_key(client, made["name"], world), tmp_path / "b.zip")
    _change_the_live_ledger(client)

    live = _data_dir(client) / "api.sqlite3"
    assert _rows(live, "households") == 3

    assert restore.restore(zipped, port=1, yes=True) == 0

    assert _rows(live, "households") == 2, "the household made after the backup is gone"
    assert _rows(live, "transactions") == 5
    assert any(p.name.startswith("api.sqlite3.before-restore-") for p in live.parent.iterdir())
    assert not list(live.parent.glob(".restore-*")), "the unpacked copy is cleaned up"


def test_without_a_key_onto_the_same_instance_the_key_there_is_kept(client, tmp_path):
    from scripts import restore

    _setup_owner(client)
    made = _backup(client)
    zipped = _save(_download(client, made["name"]), tmp_path / "b.zip")
    _change_the_live_ledger(client)

    key = _data_dir(client) / "secret.key"
    key_before = key.read_bytes()

    assert restore.restore(zipped, port=1, yes=True) == 0

    assert key.read_bytes() == key_before
    assert not list(key.parent.glob("secret.key.before-restore-*")), "not moved aside"
    assert _rows(_data_dir(client) / "api.sqlite3", "households") == 0


def test_the_wrong_key_is_refused_before_anything_moves(client, tmp_path):
    """The failure this guards: every row readable, every authenticator refused."""
    from scripts import restore

    _setup_owner(client)
    made = _backup(client)
    zipped = _save(_download(client, made["name"]), tmp_path / "b.zip")
    wrong = tmp_path / "other.key"
    wrong.write_text(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())

    live = _data_dir(client) / "api.sqlite3"
    live_sha = _sha(live)

    with pytest.raises(SystemExit, match="every authenticator would be refused"):
        restore.restore(zipped, port=1, yes=True, key_file=wrong)
    assert _sha(live) == live_sha
    assert not any(".before-restore-" in p.name for p in live.parent.iterdir())

    # Told it knows, it goes ahead and puts the key it was given in place.
    assert restore.restore(zipped, port=1, yes=True, key_file=wrong, without_key=True) == 0
    assert (_data_dir(client) / "secret.key").read_text().strip() == wrong.read_text().strip()


def test_a_bare_backup_file_restores_with_the_key_named_for_it(client, tmp_path):
    from scripts import restore

    _setup_owner(client)
    made = _backup(client)
    elsewhere = tmp_path / "carried.sqlite3"
    elsewhere.write_bytes(Path(made["path"]).read_bytes())
    right = tmp_path / "carried.key"
    right.write_bytes((_data_dir(client) / "secret.key").read_bytes())
    _change_the_live_ledger(client)

    assert restore.restore(elsewhere, port=1, yes=True, key_file=right) == 0
    assert _rows(_data_dir(client) / "api.sqlite3", "households") == 0


def test_unpacking_takes_only_the_four_known_names_and_nothing_by_its_path(tmp_path):
    from app.services import backup_bundle

    good = tmp_path / "real.sqlite3"
    with sqlite3.connect(good) as conn:
        conn.execute("CREATE TABLE t (x)")
    hostile = tmp_path / "hostile.zip"
    with zipfile.ZipFile(hostile, "w") as archive:
        archive.write(good, "a/b/spendtracker.sqlite3")
        archive.writestr("../../escaped.txt", "out")
        archive.writestr("/abs/secret.key", "key")
        archive.writestr("x/notes.txt", "ignored")

    into = tmp_path / "into"
    into.mkdir()
    backup_bundle.unpack(hostile, into)

    assert sorted(p.name for p in into.iterdir()) == ["secret.key", "spendtracker.sqlite3"]
    assert not (tmp_path / "escaped.txt").exists() and not (tmp_path.parent / "escaped.txt").exists()
    assert (into / "secret.key").stat().st_mode & 0o077 == 0

    twice = tmp_path / "twice.zip"
    with zipfile.ZipFile(twice, "w") as archive:
        archive.write(good, "a/spendtracker.sqlite3")
        archive.write(good, "b/spendtracker.sqlite3")
    again = tmp_path / "again"
    again.mkdir()
    with pytest.raises(backup_bundle.BundleRefused, match="two files"):
        backup_bundle.unpack(twice, again)


# --------------------------------------------------------------------------- #
# Leftovers
# --------------------------------------------------------------------------- #


def test_a_download_zip_a_crash_left_behind_is_swept_after_an_hour(client):
    import os
    import time

    from app.auth import housekeeping
    from app.services import backup_bundle

    _setup_owner(client)
    made = _backup(client)
    backups = Path(made["path"]).parent
    stale = backups / ".download-spendtracker-x-abc.zip"
    fresh = backups / ".download-spendtracker-y-def.zip"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    hour_and_a_bit = time.time() - backup_bundle.LEFTOVER_SECONDS - 60
    os.utime(stale, (hour_and_a_bit, hour_and_a_bit))

    removed = housekeeping.sweep(client.app_module.db_engine)

    assert removed["backup_downloads"] == 1
    assert not stale.exists()
    assert fresh.exists(), "one still being sent is not touched"
    assert Path(made["path"]).exists(), "and a backup is never a leftover"


# --------------------------------------------------------------------------- #
# #225 -- what a member may inflate to
# --------------------------------------------------------------------------- #


def _bundle_with(tmp_path, name: str, **members: bytes):
    good = tmp_path / "real.sqlite3"
    with sqlite3.connect(good) as conn:
        conn.execute("CREATE TABLE t (x)")
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(good, "b/spendtracker.sqlite3")
        for member, body in members.items():
            archive.writestr(f"b/{member}", body)
    into = tmp_path / f"{name}.into"
    into.mkdir()
    return path, into


def test_a_key_that_inflates_to_a_megabyte_is_refused_and_leaves_nothing(tmp_path):
    from app.services import backup_bundle

    bomb, into = _bundle_with(tmp_path, "bomb.zip", **{"secret.key": b"\0" * (1 << 20)})
    assert bomb.stat().st_size < 64 * 1024  # small on disk, large inflated
    with pytest.raises(backup_bundle.BundleRefused, match="secret.key is 1,048,576 bytes"):
        backup_bundle.unpack(bomb, into)
    assert not (into / "secret.key").exists()


def test_a_member_that_inflates_past_its_claim_is_stopped_and_removed(tmp_path, monkeypatch):
    """The zip's directory says small; the bytes say otherwise. Whichever of the
    ceiling or zipfile's own CRC check fires first, the partial file goes."""
    from app.services import backup_bundle

    liar, into = _bundle_with(tmp_path, "liar.zip", **{"manifest.json": b"{" + b" " * (200 * 1024) + b"}"})
    real_infolist = zipfile.ZipFile.infolist

    def claiming_small(self):
        infos = real_infolist(self)
        for info in infos:
            if info.filename.endswith("manifest.json"):
                info.file_size = 100
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", claiming_small)
    with pytest.raises(backup_bundle.BundleRefused, match="manifest.json"):
        backup_bundle.unpack(liar, into)
    assert not (into / "manifest.json").exists()


def test_a_real_bundle_still_unpacks_under_the_ceilings(tmp_path):
    from app.services import backup_bundle

    fine, into = _bundle_with(
        tmp_path, "fine.zip", **{"manifest.json": b"{}", "README.txt": b"read me", "secret.key": b"k" * 44}
    )
    backup_bundle.unpack(fine, into)
    assert sorted(p.name for p in into.iterdir()) == [
        "README.txt", "manifest.json", "secret.key", "spendtracker.sqlite3"
    ]
    assert (into / "secret.key").read_bytes() == b"k" * 44
