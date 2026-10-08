"""The `update` volume on a real filesystem: group-shared layout (C11) and atomic writes (5.6).

Changing a file's group to 65532 needs privilege or membership of it, which a
developer's laptop and a CI runner do not have. Those parts are skipped by the
code, not the test: the test asserts the group where it could be applied and
the mode always.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from updater import volume
from updater.volume import DIR_MODE, FILE_MODE, SUBDIRS, UPDATE_GID, UnsafeFile, Volume


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_the_layout_is_group_shared_setgid_2770(tmp_path):
    before = os.lstat(tmp_path).st_gid
    vol = Volume(tmp_path / "update")
    vol.init()
    made = [vol.root, *(vol.root / name for name in SUBDIRS)]
    assert sorted(p.name for p in vol.root.iterdir()) == sorted(SUBDIRS)
    for path in made:
        assert mode(path) == DIR_MODE == 0o2770, (path, oct(mode(path)))
        assert os.lstat(path).st_mode & stat.S_ISGID
        assert os.lstat(path).st_gid in (UPDATE_GID, before)


def test_a_loosened_directory_is_tightened_again(tmp_path):
    first, second = tmp_path / "history", tmp_path / "journal"
    for d in (first, second):
        d.mkdir(mode=0o777)
        os.chmod(d, 0o777)
    grouped = [volume.make_shared_dir(d) for d in (first, second)]
    assert [mode(d) for d in (first, second)] == [0o2770, 0o2770]
    if os.geteuid() == 0:
        assert grouped == [True, True]
        assert {os.lstat(d).st_gid for d in (first, second)} == {UPDATE_GID}


def test_a_directory_already_2770_is_not_chmodded_again(tmp_path, monkeypatch):
    """Rootless engines: the updater is root with `cap_drop: ALL` and does not
    own the volume's root, so a chmod there is EPERM (#169). One that is
    already right is left alone; one that is not is still tightened."""
    right, loose = tmp_path / "update", tmp_path / "loose"
    for d in (right, loose):
        d.mkdir()
    os.chmod(right, 0o2770)
    os.chmod(loose, 0o755)
    chmodded = []
    real = os.chmod

    def refuse_unowned(path, mode_, *a, **kw):
        chmodded.append(os.fspath(path))
        if os.fspath(path) == os.fspath(right):
            raise PermissionError(1, "Operation not permitted", os.fspath(path))
        return real(path, mode_, *a, **kw)

    monkeypatch.setattr(volume.os, "chmod", refuse_unowned)
    volume.make_shared_dir(right)
    volume.make_shared_dir(loose)
    assert chmodded == [os.fspath(loose)]
    assert (mode(right), mode(loose)) == (0o2770, 0o2770)


def test_a_symlink_where_a_directory_belongs_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.chmod(target, 0o755)  # explicitly: another test in this process may have left a umask
    (tmp_path / "update").mkdir()
    os.symlink(target, tmp_path / "update" / "journal")
    with pytest.raises(UnsafeFile):
        Volume(tmp_path / "update").init()
    assert mode(target) == 0o755


def test_writes_are_0660_whatever_the_umask_and_leave_no_temporary_file(tmp_path):
    vol = Volume(tmp_path / "update")
    vol.init()
    old = os.umask(0o077)
    try:
        volume.write_json(vol.status, {"state": "accepted"})
        volume.write_json(vol.heartbeat, {"busy": False})
    finally:
        os.umask(old)
    assert mode(vol.status) == mode(vol.heartbeat) == FILE_MODE == 0o660
    assert json.loads(vol.status.read_text()) == {"state": "accepted"}
    assert sorted(p.name for p in vol.root.iterdir() if p.is_file()) == ["status.json", "updater.json"]


def test_a_failed_write_leaves_the_previous_file_whole(tmp_path, monkeypatch):
    vol = Volume(tmp_path / "update")
    vol.init()
    volume.write_json(vol.journal("a"), {"step": "4"})
    volume.write_json(vol.journal("b"), {"step": "7"})

    def power_cut(fd):
        raise OSError("power cut")

    monkeypatch.setattr(volume.os, "fsync", power_cut)
    with pytest.raises(OSError):
        volume.write_json(vol.journal("a"), {"step": "5"})
    monkeypatch.undo()
    volume.write_json(vol.journal("b"), {"step": "8"})
    assert json.loads(vol.journal("a").read_text()) == {"step": "4"}
    assert json.loads(vol.journal("b").read_text()) == {"step": "8"}
    assert sorted(p.name for p in (vol.root / "journal").iterdir()) == ["a.json", "b.json"]


def test_the_directory_is_fsynced_after_the_rename(tmp_path, monkeypatch):
    vol = Volume(tmp_path / "update")
    vol.init()
    synced = []
    real = volume.fsync_dir
    monkeypatch.setattr(volume, "fsync_dir", lambda p: (synced.append(p), real(p)))
    volume.write_json(vol.history("x"), {"state": "refused"})
    volume.write_json(vol.prepared("y"), {"to_version": "0.9.0"})
    assert synced == [vol.root / "history", vol.root / "prepared"]


def test_reading_the_updaters_own_file_never_follows_a_link(tmp_path):
    vol = Volume(tmp_path / "update")
    vol.init()
    secret = tmp_path / "secret.json"
    secret.write_text('{"key": "the ledger key"}')
    os.symlink(secret, vol.journal("planted"))
    volume.write_json(vol.journal("real"), {"step": "3"})
    with pytest.raises(UnsafeFile):
        volume.read_own_json(vol.journal("planted"))
    assert volume.read_own_json(vol.journal("real")) == {"step": "3"}
    assert volume.read_own_json(vol.journal("absent")) is None
