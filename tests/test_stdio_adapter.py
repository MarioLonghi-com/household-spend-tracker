"""The desktop backend served over stdin and stdout. Issue #319.

Two ways in. Through the frames in this process: every request is encoded as
the shell sends it, decoded by the adapter's own reader, run by its `handle`,
and the answer goes back the same way -- so what is asserted has crossed the
wire format, with the app's real startup around it. And the real thing, as a
child process: the start handshake, requests, a clean exit when stdin closes,
and nothing on standard output but frames.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import PASSWORD, _stamp_head

ROOT = Path(__file__).resolve().parent.parent
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}


class FrameTransport(httpx.BaseTransport):
    """httpx, speaking the adapter's frames to the adapter's own handler."""

    def __init__(self, test_client: TestClient, app) -> None:
        self._client = test_client
        self._app = app
        self._next = 1

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        from app.desktop import stdio_adapter as adapter

        raw_path, _, _ = request.url.raw_path.partition(b"?")
        head = {
            "id": self._next,
            "method": request.method,
            "path": raw_path.decode("ascii"),
            "query": request.url.query.decode("ascii"),
            "headers": [[k.decode("latin-1"), v.decode("latin-1")] for k, v in request.headers.raw],
        }
        self._next += 1
        sent = adapter.read_message(io.BytesIO(adapter.encode(head, request.read())))
        answer = self._client.portal.call(adapter.handle, self._app, *sent)
        back_head, back_body = adapter.read_message(io.BytesIO(adapter.encode(*answer)))
        assert back_head["id"] == head["id"]
        return httpx.Response(back_head["status"], headers=back_head["headers"], content=back_body)


@pytest.fixture()
def desktop(monkeypatch, tmp_path):
    """The desktop app, started for real, and an httpx client over its frames."""
    for name in ("SPENDTRACKER_PUBLIC_URL", "SPENDTRACKER_RP_ID", "SPENDTRACKER_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SPENDTRACKER_PROFILE", "desktop")
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ledger' / 'spendtracker.sqlite3'}")
    import app.config as config

    importlib.reload(config)
    import app.db as db

    importlib.reload(db)
    from app.models import Base

    Base.metadata.create_all(db.engine)
    _stamp_head(db.engine)
    import app.main as main

    importlib.reload(main)
    with (
        TestClient(main.app, base_url="https://localhost") as started,
        httpx.Client(transport=FrameTransport(started, main.app), base_url="https://localhost") as client,
    ):
        yield client
    monkeypatch.undo()
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(main)


def _set_up(client: httpx.Client) -> None:
    started = client.post(
        "/api/setup/begin",
        json={"token": "", "email": "jane@example.test", "display_name": "Jane Ñandú", "password": PASSWORD},
        headers=SAME_ORIGIN,
    )
    assert started.status_code == 200, started.text
    done = client.post("/api/setup/complete", json={"blob": started.json()["blob"], "codes_saved": True}, headers=SAME_ORIGIN)
    assert done.status_code == 200, done.text


def test_the_scope_says_localhost_and_no_client_and_decodes_the_path():
    from app.desktop.stdio_adapter import scope_for

    scope = scope_for({
        "id": 1, "method": "get", "path": "/api/caf%C3%A9", "query": "a=1",
        "headers": [["Host", "evil.example"], ["Accept", "text/html"]],
    })
    assert (scope["method"], scope["path"], scope["raw_path"]) == ("GET", "/api/café", b"/api/caf%C3%A9")
    assert scope["client"] is None
    assert (scope["scheme"], scope["server"]) == ("https", ("localhost", 443))
    assert scope["headers"] == [(b"accept", b"text/html"), (b"host", b"localhost")]


def test_a_session_and_accented_names_cross_the_frames(desktop):
    _set_up(desktop)
    assert desktop.get("/api/me").json()["display_name"] == "Jane Ñandú"
    made = desktop.post("/api/households", json={"name": "Café Ñandú — Göteborg"}, headers=SAME_ORIGIN)
    assert made.status_code == 201, made.text
    names = [h["name"] for h in desktop.get("/api/households").json()]
    assert names == ["Café Ñandú — Göteborg"]
    assert made.content.decode("utf-8").count("Café Ñandú — Göteborg") == 1


def test_refusals_keep_their_status_and_headers(desktop):
    _set_up(desktop)
    invalid = desktop.post("/api/households", json={}, headers=SAME_ORIGIN)
    assert (invalid.status_code, invalid.json()["code"]) == (422, "request.invalid")
    refused = desktop.post("/api/me/step-up", json={"password": "not the password at all"}, headers=SAME_ORIGIN)
    assert (refused.status_code, refused.headers["x-refused"]) == (401, "proof")
    unclaimed = desktop.get("/api/agent/v1/households")
    assert unclaimed.status_code == 404
    assert "content-security-policy" in desktop.get("/api/health").headers


def test_a_receipt_goes_in_as_multipart_and_comes_back_as_bytes(desktop):
    from PIL import Image

    _set_up(desktop)
    house = desktop.post("/api/households", json={"name": "Ours"}, headers=SAME_ORIGIN).json()
    picture = io.BytesIO()
    Image.new("RGB", (640, 480), "white").save(picture, "JPEG")
    uploaded = desktop.post(
        f"/api/households/{house['id']}/receipts",
        files={"file": ("recibo-café.jpg", picture.getvalue(), "image/jpeg")},
        headers=SAME_ORIGIN,
    )
    assert uploaded.status_code == 201, uploaded.text
    receipt = uploaded.json()["receipt"]["id"]
    thumb = desktop.get(f"/api/receipts/{receipt}/thumb")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/avif"
    assert thumb.content[4:8] == b"ftyp", "an AVIF file, byte for byte"


def test_a_backup_downloads_as_a_zip(desktop):
    _set_up(desktop)
    made = desktop.post("/api/admin/application/backups", headers=SAME_ORIGIN)
    assert made.status_code == 201, made.text
    zipped = desktop.get(f"/api/admin/application/backups/{made.json()['name']}/download")
    assert zipped.status_code == 200
    assert zipped.content[:2] == b"PK"
    granted = desktop.post("/api/me/step-up", json={"password": PASSWORD}, headers=SAME_ORIGIN)
    assert granted.status_code == 200


# --------------------------------------------------------------------------- #
# The real process
# --------------------------------------------------------------------------- #


def _spawn(data: Path, **env: str) -> subprocess.Popen:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("SPENDTRACKER_") and k != "DATABASE_URL"}
    return subprocess.Popen(
        [sys.executable, "-m", "app.desktop.stdio_adapter"],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**clean, "SPENDTRACKER_PROFILE": "desktop", "SPENDTRACKER_DATA_DIR": str(data), "PYTHONUTF8": "1", **env},
    )


def test_the_process_starts_answers_and_stops_when_stdin_closes(tmp_path):
    from app.desktop.stdio_adapter import encode, read_message

    child = _spawn(tmp_path / "ledger")
    try:
        head, _ = read_message(child.stdout)
        assert head["event"] == "ready", head
        assert head["launch"]["outcome"] == "ready"
        child.stdin.write(encode({"id": 1, "method": "GET", "path": "/api/health", "query": "", "headers": []}))
        child.stdin.write(encode({"id": 2, "method": "GET", "path": "/api/capabilities", "query": "", "headers": []}))
        child.stdin.flush()
        answers = {}
        for _ in range(2):
            head, body = read_message(child.stdout)
            answers[head["id"]] = (head["status"], json.loads(body))
        assert answers[1] == (200, answers[1][1]) and answers[1][1]["setup_required"] is True
        assert answers[2][1]["second_factor"] is False
        child.stdin.close()
        assert child.wait(timeout=60) == 0
        # Every byte left on standard output is a frame: here, none.
        assert child.stdout.read() == b""
    finally:
        if child.poll() is None:
            child.kill()
    assert (tmp_path / "ledger" / "spendtracker.sqlite3").exists()


def test_it_refuses_to_start_without_utf8_mode_or_outside_the_desktop_profile(tmp_path):
    for env, says in (
        ({"PYTHONUTF8": "0"}, "start with PYTHONUTF8=1"),
        ({"SPENDTRACKER_PROFILE": "server"}, "serves the desktop profile only"),
    ):
        child = _spawn(tmp_path / "ledger", **env)
        out, err = child.communicate(timeout=60)
        assert (child.returncode, out) == (2, b""), err
        assert says in err.decode()


def test_a_ledger_from_a_newer_version_stops_the_start_and_says_why(tmp_path):
    from app.desktop.stdio_adapter import read_message

    data = tmp_path / "ledger"
    first = _spawn(data)
    assert read_message(first.stdout)[0]["event"] == "ready"
    first.stdin.close()
    assert first.wait(timeout=60) == 0
    with sqlite3.connect(data / "spendtracker.sqlite3") as conn:
        conn.execute("UPDATE alembic_version SET version_num = 'f00dfeedbeef'")

    second = _spawn(data)
    head, _ = read_message(second.stdout)
    assert (head["event"], head["launch"]["outcome"], head["launch"]["stamp_after"]) == (
        "stopped", "newer", "f00dfeedbeef",
    )
    assert second.wait(timeout=60) == 3


def test_a_frozen_build_reads_its_stamp_and_never_starts_git(monkeypatch, tmp_path):
    from app import build

    stamp = tmp_path / "build.json"
    stamp.write_text(json.dumps({"commit": "0123456789abcdef", "branch": None, "committed_at": None, "dirty": False}))
    monkeypatch.setattr(build, "STAMP", stamp)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def no_git(*_args):
        raise AssertionError("git was started from a frozen build")

    monkeypatch.setattr(build, "_git", no_git)
    build.current.cache_clear()
    try:
        found = build.current()
    finally:
        build.current.cache_clear()
    assert (found.commit, found.source) == ("0123456789abcdef", "stamp")
