"""What the server-side code does on an OS it was not written on. Issue #315.

The desktop app runs this code on macOS and Windows, where CI never ran it.
Each test asserts what the code produced there, not that it ran: a guarded
flag's value, a file's mode, the list a startup check hands to the log.
"""

from __future__ import annotations

import ast
import os
import stat
from pathlib import Path

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


# --------------------------------------------------------------------------- #
# A connection the app opened is closed by the app, not by the collector
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parent.parent


def _unclosed_connections(source: str, where: str) -> list[str]:
    """`with sqlite3.connect(...) as c:` sites. Python's context manager on a
    connection commits or rolls back and leaves it **open** until the garbage
    collector gets to it. On macOS a ledger renamed under such a connection
    then fails to open with "disk I/O error" (the reset test did, every time,
    under coverage); on Windows an open handle stops the rename altogether --
    and restore, reset and the upgrade drill all rename the ledger (#315).
    `contextlib.closing(sqlite3.connect(...))` closes it on the way out."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                call = item.context_expr
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "connect"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "sqlite3"
                ):
                    found.append(f"{where}:{node.lineno}")
    return found


def test_every_sqlite_connection_the_app_opens_in_a_with_is_closed_by_it():
    found = []
    for folder in ("app", "statements", "scripts"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            found += _unclosed_connections(path.read_text(), str(path.relative_to(ROOT)))
    assert found == []


def test_the_scan_finds_a_planted_one():
    planted = "import sqlite3\nwith sqlite3.connect('x') as conn:\n    pass\n"
    assert _unclosed_connections(planted, "planted.py") == ["planted.py:2"]
    closed = "import contextlib, sqlite3\nwith contextlib.closing(sqlite3.connect('x')) as conn:\n    pass\n"
    assert _unclosed_connections(closed, "closed.py") == []
