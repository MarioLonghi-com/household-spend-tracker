"""What the server-side code does on an OS it was not written on. Issue #315.

The desktop app runs this code on macOS and Windows, where CI never ran it.
Each test asserts what the code produced there, not that it ran: a guarded
flag's value, a file's mode, the list a startup check hands to the log.
"""

from __future__ import annotations

import os
import stat

import pytest

from app import permissions


def test_an_open_flag_windows_lacks_is_zero_there_and_the_real_one_elsewhere(monkeypatch):
    assert getattr(os, "O_NOFOLLOW", 0) == permissions.O_NOFOLLOW
    assert getattr(os, "O_NONBLOCK", 0) == permissions.O_NONBLOCK
    if os.name != "nt":
        assert permissions.O_NOFOLLOW != 0
        assert permissions.O_NONBLOCK != 0


@pytest.mark.skipif(os.name == "nt", reason="needs a mode fchmod can change")
def test_fchmod_changes_the_mode_where_there_is_one(tmp_path):
    target = tmp_path / "report.json"
    target.write_text("{}")
    target.chmod(0o600)
    fd = os.open(target, os.O_RDONLY)
    try:
        permissions.fchmod(fd, 0o660)
    finally:
        os.close(fd)
    assert stat.S_IMODE(target.stat().st_mode) == 0o660


def test_fchmod_is_a_quiet_no_op_without_one(tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    target.write_text("{}")
    before = stat.S_IMODE(target.stat().st_mode)
    monkeypatch.delattr(os, "fchmod", raising=False)
    fd = os.open(target, os.O_RDONLY)
    try:
        permissions.fchmod(fd, 0o000)
    finally:
        os.close(fd)
    assert stat.S_IMODE(target.stat().st_mode) == before


def _loose_tree(root):
    (root / "logs").mkdir()
    for path in (root / "spendtracker.sqlite3", root / "logs" / "app.log"):
        path.write_text("x")
    if os.name != "nt":
        root.chmod(0o755)
        (root / "logs").chmod(0o755)
        (root / "spendtracker.sqlite3").chmod(0o644)
        (root / "logs" / "app.log").chmod(0o664)


@pytest.mark.skipif(os.name == "nt", reason="the bits are only set, and only mean anything, on POSIX")
def test_where_the_bits_mean_access_every_loose_path_is_named(tmp_path):
    _loose_tree(tmp_path)
    found = {path.relative_to(tmp_path).as_posix(): mode for path, mode in permissions.not_private(tmp_path)}
    assert found == {".": 0o755, "logs": 0o755, "logs/app.log": 0o664, "spendtracker.sqlite3": 0o644}


def test_where_the_bits_mean_nothing_no_path_is_named(tmp_path, monkeypatch):
    """Windows: `stat` says 0o666 for every file whoever may read it, so the
    startup check would name the whole data directory on every boot."""
    _loose_tree(tmp_path)
    monkeypatch.setattr(permissions, "MODE_BITS_MEAN_ACCESS", False)
    assert permissions.not_private(tmp_path) == []


def test_the_bits_are_trusted_exactly_off_windows():
    assert permissions.MODE_BITS_MEAN_ACCESS is (os.name != "nt")
