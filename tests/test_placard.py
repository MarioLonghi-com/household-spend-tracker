"""The maintenance page and browser recovery, the page's side (design notes 6.4, Part 11, 9.2).

The page runs here as it runs in its container, a real HTTP server on a
loopback port, against a temporary `update` volume and ledger. The two faces:
to an unauthenticated viewer only "being updated" (A8) -- no version, no
step, no log, whatever the volume holds -- and the owner's recovery once the
updater accepted the code. The end-to-end tests run the updater's real
`Service` against the fake world in a thread beside it, so the page never
judges a code: the updater does, through the files of the handshake.
"""

from __future__ import annotations

import errno
import http.client
import io
import json
import os
import sqlite3
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path

import pytest

from app.services import updates
from scripts import placard
from tests.updater_world import A, B, World
from updater import contract, volume

CODE = "7K2M-9QXR-4HJT-0WVB-PN3D-8FGC-6YZ1"
WRONG = "8K2M-9QXR-4HJT-0WVB-PN3D-8FGC-6YZ1"
HASH = updates.hash_recovery_code(CODE)
SECRET_KEY_TEXT = "placeholder-secret-key-for-tests"


class Page:
    """The page on a free port, and an HTTP client for it."""

    def __init__(self, place: placard.Place) -> None:
        self.place = place
        self.server = placard.Server(("127.0.0.1", 0), place)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie: str | None = None

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def request(self, method: str, path: str, form: dict | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        body = urllib.parse.urlencode(form).encode() if form is not None else None
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if body is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookie:
            h["Cookie"] = f"{placard.COOKIE}={self.cookie}"
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        set_cookie = resp.getheader("Set-Cookie")
        if set_cookie and set_cookie.startswith(placard.COOKIE + "="):
            value = set_cookie.split(";", 1)[0].split("=", 1)[1]
            self.cookie = value or None
        return resp, data

    def get(self, path: str):
        return self.request("GET", path)

    def post(self, path: str, form: dict, headers: dict | None = None):
        return self.request("POST", path, form, headers)

    def csrf(self) -> str:
        _, body = self.get("/recovery")
        text = body.decode()
        marker = 'name=csrf value="'
        return text[text.index(marker) + len(marker) :].split('"', 1)[0]


def make_ledger(data_dir: Path, stamps: list[str]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for i, stamp in enumerate(stamps):
        folder = data_dir / "backups" / stamp
        folder.mkdir(parents=True)
        with sqlite3.connect(folder / "spendtracker.sqlite3") as db:
            db.execute("create table alembic_version (version_num varchar(32))")
            db.execute("insert into alembic_version values (?)", (f"aaaaaaaaaaa{i}",))
        (folder / "manifest.json").write_text(json.dumps({"alembic_revision": f"aaaaaaaaaaa{i}"}))
        (folder / "secret.key").write_text(SECRET_KEY_TEXT + "\n")


def seeded_volume(root: Path, update_id: str, mode: str | None = "recovery") -> volume.Volume:
    """An update volume holding a failed update's records, as the updater leaves them."""
    vol = volume.Volume(root)
    vol.init()
    volume.write_json(
        vol.history(update_id),
        {
            "protocol": 1,
            "id": update_id,
            "kind": "apply",
            "state": "needs_recovery",
            "sentence": "The update failed and putting 0.7.1 back failed too.",
            "failed_step": "R2",
            "from_version": "0.7.1",
            "to_version": "0.8.0",
            "log_tail": ["drill line seven"],
        },
    )
    volume.write_json(
        vol.status,
        {
            "protocol": 1,
            "id": update_id,
            "kind": "apply",
            "state": "needs_recovery",
            "step": "R2",
            "sentences": ["Restoring the backup with the previous version."],
        },
    )
    volume.write_json(
        vol.journal(update_id),
        {
            "protocol": 1,
            "id": update_id,
            "kind": "apply",
            "step": "R2",
            "recovery_hash": HASH,
            "context": {"restore_log": ["restore said this"]},
        },
    )
    if mode:
        volume.write_json(
            vol.recovery_mode,
            contract.recovery_mode(mode, update_id if mode == "recovery" else None, time.time()),
        )
    return vol


SECRETS_OF_THE_UPDATE = (
    "0.7.1",
    "0.8.0",
    "R2",
    "drill line seven",
    "restore said this",
    "Restoring the backup",
)


@pytest.fixture
def ids():
    import uuid

    return str(uuid.uuid4()), str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# The faces
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("recovery", [False, True])
def test_everybody_sees_only_that_it_is_being_updated(tmp_path, ids, recovery):
    seeded_volume(tmp_path / "update", ids[0])
    page = Page(placard.Place(tmp_path / "update", tmp_path / "ledger", recovery=recovery))
    try:
        for path in ("/", "/transactions", "/api/accounts?x=1"):
            resp, body = page.get(path)
            text = body.decode()
            assert resp.status == 503 and placard.SENTENCE in text
            for leak in SECRETS_OF_THE_UPDATE:
                assert leak not in text, f"{leak!r} shown to anybody"
            assert ('href="/recovery"' in text) is recovery
            for header, value in placard.SECURITY_HEADERS.items():
                assert resp.getheader(header) == value
        resp, body = page.get("/api/health")
        assert resp.status == 503 and json.loads(body) == {"status": "unavailable"}
        resp, body = page.get("/recovery")
        if recovery:
            text = body.decode()
            assert resp.status == 200 and "name=code" in text
            for leak in SECRETS_OF_THE_UPDATE:
                assert leak not in text, f"{leak!r} shown before the code"
        else:
            assert resp.status == 404
    finally:
        page.close()


def test_the_ledger_ahead_page_says_only_to_run_the_launcher(tmp_path, ids):
    seeded_volume(tmp_path / "update", ids[0], mode="ledger_ahead")
    page = Page(placard.Place(tmp_path / "update", tmp_path / "ledger", recovery=True))
    try:
        resp, body = page.get("/")
        text = body.decode()
        assert resp.status == 503 and "Run the launcher again" in text
        assert placard.SENTENCE not in text and 'href="/recovery"' not in text
        for leak in SECRETS_OF_THE_UPDATE:
            assert leak not in text
        assert page.get("/recovery")[0].status == 404
        assert page.post("/recovery/open", {"code": CODE})[0].status == 503
        assert not os.path.lexists(tmp_path / "update" / "recovery" / "request.json")
    finally:
        page.close()


def test_a_post_from_another_origin_writes_no_request(tmp_path, ids):
    seeded_volume(tmp_path / "update", ids[0])
    page = Page(placard.Place(tmp_path / "update", tmp_path / "ledger", recovery=True, answer_seconds=0.2))
    try:
        resp, _ = page.post("/recovery/open", {"code": CODE}, {"Origin": "http://evil.example"})
        assert resp.status == 403
        assert not os.path.lexists(tmp_path / "update" / "recovery" / "request.json")
    finally:
        page.close()


def test_the_page_shows_the_updaters_refusal_window_and_sends_nothing_during_it(tmp_path, ids):
    vol = seeded_volume(tmp_path / "update", ids[0])
    volume.write_json(
        vol.recovery_attempts,
        {"protocol": 1, "wrong": 5, "refused_until": contract.iso(time.time() + 600), "per_round": 5},
    )
    page = Page(placard.Place(tmp_path / "update", tmp_path / "ledger", recovery=True))
    try:
        _, body = page.get("/recovery")
        assert "10 more minutes" in body.decode() and "name=code" not in body.decode()
        resp, _ = page.post("/recovery/open", {"code": CODE})
        assert resp.status == 429
        assert not os.path.lexists(vol.recovery_request)
    finally:
        page.close()


def test_without_an_updater_the_request_is_withdrawn_with_its_code(tmp_path, ids):
    vol = seeded_volume(tmp_path / "update", ids[0])
    page = Page(placard.Place(tmp_path / "update", tmp_path / "ledger", recovery=True, answer_seconds=0.5))
    try:
        _, body = page.post("/recovery/open", {"code": CODE})
        assert "did not answer" in body.decode()
        assert not os.path.lexists(vol.recovery_request)
    finally:
        page.close()


# --------------------------------------------------------------------------- #
# Against the updater: the fake world, its service ticking in a thread
# --------------------------------------------------------------------------- #


class Updater:
    def __init__(self, service) -> None:
        self.service = service
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                self.service.tick()
            self.stop.wait(0.02)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(10)


#: The page in `recovering` reads the world's clock, which only moves when
#: something sleeps on it -- and the updater thread sleeps on it on every tick.
#: On a slow runner those ticks can spend the page's 60 s answer deadline (and
#: its 30 min session) in fake time before the page has read an answer that
#: did arrive, which failed CI once with "the updater did not answer". These
#: tests are about what the page and the updater say, not about deadlines,
#: which have tests of their own on a page that has no ticking updater.
LONG = 10.0**7


@pytest.fixture
def recovering(tmp_path, monkeypatch):
    """A world whose update needs recovery, the page in recovery mode, the updater ticking."""
    monkeypatch.setattr(placard, "SESSION_SECONDS", LONG)
    with World(tmp_path / "world") as w:
        service = w.service()
        service.startup()
        w.drill = "migration-fails"
        w.restore_fails = 3
        report = w.prepared(service)
        req = {**w.apply_request(report), "recovery_hash": HASH}
        w.write_request(req)
        service.tick()
        assert w.history(req["id"])["state"] == "needs_recovery"
        stamps = sorted(w.ledger.backups)
        make_ledger(tmp_path / "ledger", stamps)
        place = placard.Place(
            w.volume.root,
            tmp_path / "ledger",
            recovery=True,
            cookie_secure=False,
            clock=w.time,
            sleep=lambda s: time.sleep(0.01),
            answer_seconds=LONG,
        )
        page = Page(place)
        updater = Updater(service)
        try:
            yield w, page, req, stamps
        finally:
            updater.close()
            page.close()


def unlock(page: Page, code: str = CODE):
    return page.post("/recovery/open", {"code": code})


def test_a_wrong_code_is_refused_by_the_updater_and_the_page_stays_shut(recovering):
    w, page, req, _ = recovering
    resp, body = unlock(page, WRONG)
    text = body.decode()
    assert resp.status == 200 and "not the recovery code" in text
    assert page.cookie is None
    assert "4 more wrong codes before a pause" in text
    assert (volume.read_own_json(w.volume.recovery_attempts) or {})["wrong"] == 1
    shut = page.get("/recovery")[1].decode()
    for leak in (B, "line 59", "the copy failed", "Retry the rollback"):
        assert leak not in text and leak not in shut


def test_the_right_code_shows_the_failed_update_and_its_logs(recovering):
    w, page, req, stamps = recovering
    resp, _ = unlock(page, CODE.lower().replace("-", ""))
    assert resp.status == 303 and page.cookie
    _, body = page.get("/recovery")
    text = body.decode()
    assert f"from <b>{A}</b> to <b>{B}</b>" in text
    assert "line 59" in text, "the drill's log tail"
    assert "the copy failed" in text, "the restore's log tail"
    assert "Retry the rollback" in text and "Leave it to me" in text
    for stamp in stamps:
        assert stamp in text
    actions = w.history(req["id"])["recovery"]
    assert [a["kind"] for a in actions] == ["open"]


def zip_names(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n.rsplit("/", 1)[1]: z.read(n) for n in z.namelist()}


def test_the_backup_download_leaves_the_key_out_unless_ticked(recovering):
    w, page, req, stamps = recovering
    unlock(page)
    csrf = page.csrf()
    resp, data = page.post("/recovery/act", {"csrf": csrf, "kind": "download_backup", "backup": stamps[-1]})
    assert resp.status == 200 and resp.getheader("Content-Type") == "application/zip"
    files = zip_names(data)
    assert set(files) == {"README.txt", "manifest.json", "spendtracker.sqlite3"}
    assert SECRET_KEY_TEXT.encode() not in data
    resp, data = page.post(
        "/recovery/act",
        {"csrf": csrf, "kind": "download_backup", "backup": stamps[-1], "include_key": "yes"},
    )
    files = zip_names(data)
    assert set(files) == {"README.txt", "manifest.json", "spendtracker.sqlite3", "secret.key"}
    assert files["secret.key"].strip() == SECRET_KEY_TEXT.encode()
    sentences = [a["sentence"] for a in w.history(req["id"])["recovery"] if a["kind"] == "download_backup"]
    assert sentences[0].endswith("without secret.key.") and sentences[1].endswith("with secret.key.")


def test_the_diagnostics_hold_no_ledger_no_key_and_no_hash(recovering):
    w, page, req, _ = recovering
    unlock(page)
    resp, data = page.post("/recovery/act", {"csrf": page.csrf(), "kind": "download_diagnostics"})
    assert resp.status == 200
    files = zip_names(data)
    assert {"journal.json", "history.json", "drill.json"} <= set(files)
    assert "spendtracker.sqlite3" not in files and "secret.key" not in files
    assert b"SQLite format" not in data and SECRET_KEY_TEXT.encode() not in data
    journal_doc = json.loads(files["journal.json"])
    assert "recovery_hash" not in journal_doc and journal_doc["id"] == req["id"]
    assert HASH.encode() not in b"".join(files.values())


def test_retry_from_the_page_brings_the_previous_version_back(recovering):
    w, page, req, _ = recovering
    unlock(page)
    w.restore_fails = 0
    resp, body = page.post("/recovery/act", {"csrf": page.csrf(), "kind": "retry_rollback"})
    assert resp.status == 202 and "Accepted" in body.decode()
    deadline = time.time() + 20
    while time.time() < deadline and (w.history(req["id"]) or {}).get("state") != "rolled_back":
        time.sleep(0.05)
    assert w.history(req["id"])["state"] == "rolled_back"
    assert w.ledger.stamp == A
    assert [w.version_of(c) for c in w.running_apps()] == [A]


def test_an_action_without_the_forms_token_does_nothing(recovering):
    w, page, req, _ = recovering
    unlock(page)
    before = list(w.history(req["id"])["recovery"])
    resp, _ = page.post("/recovery/act", {"csrf": "forged", "kind": "leave_for_operator"})
    assert resp.status == 303
    time.sleep(0.2)
    assert w.history(req["id"])["state"] == "needs_recovery"
    assert w.history(req["id"])["recovery"] == before


def test_leaving_it_to_me_shows_the_server_commands(recovering):
    w, page, req, _ = recovering
    unlock(page)
    resp, _ = page.post("/recovery/act", {"csrf": page.csrf(), "kind": "leave_for_operator"})
    assert resp.status == 303
    _, body = page.get("/recovery")
    text = body.decode()
    assert "scripts.restore" in text and "Retry the rollback" not in text
    assert w.history(req["id"])["state"] == "left_for_operator"


def test_the_code_reaches_no_file_the_page_and_updater_wrote(recovering, tmp_path):
    w, page, req, stamps = recovering
    unlock(page, WRONG)
    unlock(page, CODE.lower())
    page.post("/recovery/act", {"csrf": page.csrf(), "kind": "download_diagnostics"})
    canonical = updates.canonical_code(CODE)
    for path in [Path(d) / f for d, _, fs in os.walk(tmp_path) for f in fs]:
        data = path.read_bytes()
        for v in (CODE, CODE.lower(), canonical, canonical.lower(), WRONG, updates.canonical_code(WRONG)):
            assert v.encode() not in data, f"{v} reached {path}"


def test_two_requests_on_a_clock_that_has_not_moved_carry_different_times(tmp_path, monkeypatch):
    """The page tells an answer from the last one's by the request's `created_at`.

    With the clock standing still between two requests, the old `last +
    0.000001` formatted to the same microsecond about one time in twenty, and
    the page took the wrong code's refusal for the right code's answer (a CI
    flake that looked like a timing problem). The reading below is between two
    microseconds, as a real clock's is, and collided before the fix.
    """
    frozen = next(
        r
        for r in (1_790_000_000.0 + k * 0.000000123 for k in range(100_000))
        if placard.now_iso(r) == placard.now_iso(r + 0.000001)
    )
    sent: list[dict] = []

    def capture(directory, request):
        sent.append(dict(request))
        raise OSError(errno.EROFS, "captured")

    monkeypatch.setattr(placard, "write_request", capture)
    place = placard.Place(
        tmp_path / "update", tmp_path / "ledger", recovery=True, clock=lambda: frozen, sleep=lambda s: None, answer_seconds=0
    )
    update_id = "4b1d9a3e-5a2f-4c3e-9d1b-0a1b2c3d4e5f"
    first = place.ask(update_id, "open", "WRONG-CODE")
    second = place.ask(update_id, "open", "RIGHT-CODE")
    assert first["state"] == second["state"] == "refused"
    assert [r["code"] for r in sent] == ["WRONG-CODE", "RIGHT-CODE"]
    assert sent[0]["created_at"] != sent[1]["created_at"]
    # One microsecond apart, and both read back as times.
    gap = placard.parse_iso(sent[1]["created_at"]) - placard.parse_iso(sent[0]["created_at"])
    assert gap == pytest.approx(0.000001, abs=1e-7)
