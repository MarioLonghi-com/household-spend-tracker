"""The maintenance page, and browser recovery behind the one-time code (design notes 6.4, Part 11, 9.2).

    python -m scripts.placard [--recovery]

The updater starts this from the **old** app image, in its own container,
where the app was listening: the app's network and port binding, the
`update` volume read-write (it writes under `recovery/` only) and the ledger
volume **read-only**. It binds what the app binds -- `SPENDTRACKER_HOST`,
else 0.0.0.0, and `PORT`, else 8848, as `deploy/entrypoint.py` chooses -- so
the sidecar's healthcheck and the owner's open tab reach it at the same
address.

**Standard library only.** No FastAPI, no `app` import -- importing the app's
settings can write a key -- and nothing is ever written to the ledger.

## Two faces

To everybody: **"Spend Tracker is being updated. It will be back in a few
minutes."** (A8) and nothing else -- no version, no step, no log -- with
`/api/health` answering 503. In recovery mode (`--recovery`, and
`recovery/mode.json` saying what for), a small *Owner: open recovery* link
as well; in the 9.2 case, where no code exists, the page says only that the
ledger is ahead and to run the launcher again.

`/recovery` asks for the recovery code. **The page never decides whether it
is right**: it forwards the code inside a fixed request (`recovery/request.json`,
R9's shapes) and reads the updater's verdict from `recovery/answer.json`,
matched by the request's `id`, `kind` and `created_at` (to the microsecond).
Only after an `open` the updater accepted does the page show the failed
update -- its steps, sentences, the drill's and the restore's log tails -- and
offer 11.3's actions, each one again a request carrying the code, which the
page holds in memory for the session and never writes anywhere else.

The downloads are streamed by the page, after the updater accepted the code:
a backup folder from the read-only ledger mount as a zip `make restore`
takes, `secret.key` only when its box was ticked; and the diagnostics, which
hold the update's records and logs with the code's hash taken out, and no
ledger data and no key.

The wrong-code counter is the updater's (`recovery/attempts.json`); the page
only shows it.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import errno
import html
import http.server
import io
import json
import os
import re
import secrets
import socketserver
import sqlite3
import stat
import threading
import time
import urllib.parse
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SENTENCE = "Spend Tracker is being updated. It will be back in a few minutes."
AHEAD_SENTENCE = (
    "Spend Tracker was started on an older version than the one its data was last "
    "used with, so it will not open. Run the launcher again: it starts the right version."
)
TITLE = "Spend Tracker"

DEFAULT_UPDATE_DIR = "/var/lib/spend-tracker-update"
DEFAULT_DATA_DIR = "/var/lib/spend-tracker"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8848
#: The group every file in the `update` volume belongs to (C11).
UPDATE_GID = 65532
FILE_MODE = 0o660

#: How long an unlocked page stays unlocked without use.
SESSION_SECONDS = 30 * 60
MAX_SESSIONS = 16
#: How long the page waits for the updater to take a request and answer it.
ANSWER_SECONDS = 60.0
POLL_SECONDS = 0.25
#: The most of any update-volume file the page reads.
MAX_READ = 1024 * 1024
#: A form body is a code and a few fields.
MAX_FORM = 4096
COOKIE = "placard_recovery"

BACKUP_STAMP = re.compile(r"[0-9]{8}-[0-9]{6}")
REVISION = re.compile(r"[0-9a-f]{12}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
CODE = re.compile(r"[A-Za-z0-9-]{16,64}")
#: The newest update backups offered (11.3).
OFFERED_BACKUPS = 5

#: The app's own headers (`app/main.py`), and a CSP tighter than the app's:
#: this page has no script, no image and no other origin at all.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), "
        "publickey-credentials-get=(), publickey-credentials-create=()"
    ),
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Cache-Control": "no-store",
}

#: The warning the Backups screen shows beside its own tick-box (#204).
KEY_WARNING = (
    "The zip will hold the key that decrypts every authenticator. With it and a member's "
    "password, anyone holding the zip can sign in as that member once it is restored. "
    "Keep it somewhere only you can open."
)

#: What the owner runs from a terminal once they took it over (deploy/UPGRADING.md).
SERVER_COMMANDS = """\
# 1. The maintenance page itself is a one-off container; remove it first.
docker ps -a --filter label=com.docker.compose.oneoff=True
docker rm -f <the placard container listed above>

# 2. Put an update backup back with the version that took it, then start it.
docker compose run --rm -T --entrypoint python app -m scripts.restore \\
    /var/lib/spend-tracker/backups/<stamp> --yes
docker compose run --rm -T --entrypoint python app -m scripts.upgrade --check
docker compose up -d

# The full procedure: deploy/UPGRADING.md, "Rolling back" and "In a container".
"""


def truthy(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def bind_host() -> str:
    """As `deploy/entrypoint.py`'s `bind_host`: `SPENDTRACKER_HOST`, else 0.0.0.0."""
    return (os.environ.get("SPENDTRACKER_HOST") or DEFAULT_HOST).strip() or DEFAULT_HOST


def bind_port() -> int:
    raw = (os.environ.get("PORT") or str(DEFAULT_PORT)).strip()
    return int(raw) if raw.isdigit() else DEFAULT_PORT


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def read_json(path: Path) -> dict | None:
    """A JSON object from the update volume: never through a symlink, a regular file, bounded."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_READ:
            return None
        data = os.read(fd, MAX_READ + 1)
    finally:
        os.close(fd)
    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def write_request(directory: Path, doc: dict) -> None:
    """`recovery/request.json`, atomically, group-shared, never through a symlink (C11)."""
    st = os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(errno.ENOTDIR, "recovery/ in the update volume is not a directory")
    target = directory / "request.json"
    tmp = directory / f".request.{secrets.token_hex(6)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with contextlib.suppress(PermissionError):
            if os.fstat(fd).st_gid != UPDATE_GID:
                os.fchown(fd, -1, UPDATE_GID)
        os.write(fd, json.dumps(doc).encode())
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, target)


def now_iso(now: float) -> str:
    return dt.datetime.fromtimestamp(now, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        with contextlib.suppress(ValueError):
            return dt.datetime.strptime(value, fmt).replace(tzinfo=dt.UTC).timestamp()
    return None


# --------------------------------------------------------------------------- #
# The place: where things are, and what the updater says
# --------------------------------------------------------------------------- #


@dataclass
class Session:
    code: str
    update_id: str
    csrf: str
    expires: float


@dataclass
class Place:
    update_dir: Path
    data_dir: Path
    recovery: bool = False
    cookie_secure: bool = True
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    answer_seconds: float = ANSWER_SECONDS
    sessions: dict[str, Session] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _last_sent: float = 0.0

    @property
    def recovery_dir(self) -> Path:
        return self.update_dir / "recovery"

    def mode(self) -> dict | None:
        """None for the plain page; else `{"mode": "recovery"|"ledger_ahead", "id": ...}`."""
        if not self.recovery:
            return None
        doc = read_json(self.recovery_dir / "mode.json") or {}
        mode = doc.get("mode") if doc.get("mode") in ("recovery", "ledger_ahead") else "recovery"
        update_id = doc.get("id") if isinstance(doc.get("id"), str) and UUID.fullmatch(doc["id"]) else None
        if mode == "recovery" and update_id is None:
            status = read_json(self.update_dir / "status.json") or {}
            if isinstance(status.get("id"), str) and UUID.fullmatch(status["id"]):
                update_id = status["id"]
        return {"mode": mode, "id": update_id}

    def attempts(self) -> dict:
        doc = read_json(self.recovery_dir / "attempts.json") or {}
        wrong = doc.get("wrong") if isinstance(doc.get("wrong"), int) else 0
        until = parse_iso(doc.get("refused_until"))
        per = doc.get("per_round") if isinstance(doc.get("per_round"), int) and doc["per_round"] > 0 else 5
        return {"wrong": wrong, "refused_until": until, "per_round": per}

    def refused_for(self) -> int:
        """Seconds the updater still refuses every code for; 0 when it does not."""
        until = self.attempts()["refused_until"]
        return max(0, int(until - self.clock())) if until else 0

    # ------------------------------------------------------------------ #

    def ask(self, update_id: str, kind: str, code: str, **fields: object) -> dict:
        """Write one recovery request and wait for the updater's answer to it.

        Returns the answer, or a made-up `refused` one when the updater did
        not answer. Serialised: one request in the volume at a time.
        """
        with self.lock:
            # Each request's time is its own, to the microsecond: it is how
            # its answer is told from the last one's.
            stamp = max(self.clock(), self._last_sent + 0.000001)
            self._last_sent = stamp
            request = {
                "protocol": 1,
                "id": update_id,
                "kind": kind,
                "created_at": now_iso(stamp),
                "code": code,
                **fields,
            }
            deadline = self.clock() + self.answer_seconds
            while os.path.lexists(self.recovery_dir / "request.json"):
                if self.clock() >= deadline:
                    return {"state": "refused", "sentence": "The updater has not taken the last request yet."}
                self.sleep(POLL_SECONDS)
            try:
                write_request(self.recovery_dir, request)
            except OSError as e:
                return {"state": "refused", "sentence": f"The request could not be written ({e.strerror})."}
            while self.clock() < deadline:
                answer = read_json(self.recovery_dir / "answer.json")
                if (
                    answer is not None
                    and answer.get("id") == update_id
                    and answer.get("kind") == kind
                    and answer.get("created_at") == request["created_at"]
                ):
                    return answer
                self.sleep(POLL_SECONDS)
            # Nobody took it: do not leave the code lying in the volume.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.recovery_dir / "request.json")
            return {"state": "refused", "sentence": "The updater did not answer. It may not be running."}

    def new_session(self, code: str, update_id: str) -> str:
        now = self.clock()
        for token in [t for t, s in self.sessions.items() if s.expires <= now]:
            del self.sessions[token]
        while len(self.sessions) >= MAX_SESSIONS:
            del self.sessions[min(self.sessions, key=lambda t: self.sessions[t].expires)]
        token = secrets.token_urlsafe(32)
        self.sessions[token] = Session(code, update_id, secrets.token_urlsafe(16), now + SESSION_SECONDS)
        return token

    def session(self, token: str | None) -> Session | None:
        found = self.sessions.get(token or "")
        if found is None or found.expires <= self.clock():
            self.sessions.pop(token or "", None)
            return None
        found.expires = self.clock() + SESSION_SECONDS
        return found

    # ------------------------------------------------------------------ #
    # What the owner is shown after the code
    # ------------------------------------------------------------------ #

    def journal(self, update_id: str) -> dict:
        return read_json(self.update_dir / "journal" / f"{update_id}.json") or {}

    def history(self, update_id: str) -> dict:
        return read_json(self.update_dir / "history" / f"{update_id}.json") or {}

    def backups(self) -> list[dict]:
        """The newest update backups: those an update's journal names, with the version that took them."""
        found: dict[str, dict] = {}
        directory = self.update_dir / "journal"
        with contextlib.suppress(OSError):
            for path in directory.iterdir():
                if not (path.name.endswith(".json") and UUID.fullmatch(path.name[:-5])):
                    continue
                ctx = (read_json(path) or {}).get("context") or {}
                folder, version = ctx.get("backup"), ctx.get("old_version")
                if not (isinstance(folder, str) and isinstance(version, str) and ctx.get("old_ref")):
                    continue
                stamp = os.path.basename(folder)
                if BACKUP_STAMP.fullmatch(stamp):
                    found[stamp] = {"stamp": stamp, "version": version}
        out = []
        for stamp in sorted(found, reverse=True)[:OFFERED_BACKUPS]:
            one = found[stamp]
            folder = self.data_dir / "backups" / stamp
            manifest = read_json(folder / "manifest.json") or {}
            one["revision"] = manifest.get("alembic_revision") if isinstance(manifest.get("alembic_revision"), str) else None
            one["present"] = folder.is_dir() and not folder.is_symlink() and (folder / "spendtracker.sqlite3").is_file()
            one["has_key"] = (folder / "secret.key").is_file()
            out.append(one)
        return out

    def ledger_revision(self) -> str | None:
        """The ledger's stamp, read without writing: `immutable=1` on the read-only mount."""
        path = self.data_dir / "spendtracker.sqlite3"
        if not path.is_file():
            return None
        try:
            with contextlib.closing(
                sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?immutable=1", uri=True, timeout=1)
            ) as db:
                rows = db.execute("select version_num from alembic_version").fetchall()
        except sqlite3.Error:
            return None
        return rows[0][0] if len(rows) == 1 and isinstance(rows[0][0], str) else None

    def known_heads(self, update_id: str) -> dict[str, str]:
        """Revision -> version, for the two versions this update knows (as the updater reads them)."""
        ctx = self.journal(update_id).get("context") or {}
        prepared = ctx.get("prepared_id")
        report = read_json(self.update_dir / "prepared" / f"{prepared}.json") if isinstance(prepared, str) and UUID.fullmatch(prepared) else None
        drill = read_json(self.update_dir / "work" / update_id / "drill.json") or {}
        stamps = drill.get("stamp") if isinstance(drill.get("stamp"), dict) else {}
        heads = {}
        old = (report or {}).get("database_stamp") or stamps.get("before")
        new = (report or {}).get("code_head") or stamps.get("after")
        if isinstance(old, str) and isinstance(ctx.get("old_version"), str):
            heads[old] = ctx["old_version"]
        if isinstance(new, str) and isinstance(ctx.get("to_version"), str):
            heads[new] = ctx["to_version"]
        return heads


# --------------------------------------------------------------------------- #
# Zips, streamed
# --------------------------------------------------------------------------- #


class _Sink(io.RawIOBase):
    """A write-only, unseekable file over the response, so `zipfile` streams."""

    def __init__(self, out) -> None:
        self.out = out

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.out.write(data)
        return len(data)


BACKUP_README = """\
A Spend Tracker backup, downloaded from the recovery page during a failed update.

Put it back with `make restore FROM=<this zip>`, or inside the container with
`python -m scripts.restore <folder> --yes`. The manifest says which database
revision and which app version it was taken at; restore it with that version.
{key}
"""


def stream_backup(out, folder: Path, stamp: str, include_key: bool) -> None:
    root = f"spendtracker-update-{stamp}"
    key_line = (
        "secret.key is in this zip. " + KEY_WARNING
        if include_key
        else "secret.key was left out: on another instance, every authenticator is refused unless the key is supplied."
    )
    with zipfile.ZipFile(_Sink(out), "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.writestr(f"{root}/README.txt", BACKUP_README.format(key=key_line))
        for name in ("manifest.json", "spendtracker.sqlite3", *(("secret.key",) if include_key else ())):
            path = folder / name
            if path.is_file() and not path.is_symlink():
                archive.write(path, f"{root}/{name}")


def stream_diagnostics(out, place: Place, update_id: str) -> None:
    """The update's records and logs. No ledger data, no key, and the code's hash taken out."""
    root = f"spendtracker-diagnostics-{update_id[:8]}"
    u = place.update_dir
    files = {
        "history.json": u / "history" / f"{update_id}.json",
        "heartbeat.json": u / "updater.json",
        "status.json": u / "status.json",
        "drill.json": u / "work" / update_id / "drill.json",
        "attempts.json": u / "recovery" / "attempts.json",
        "mode.json": u / "recovery" / "mode.json",
    }
    with zipfile.ZipFile(_Sink(out), "w", compression=zipfile.ZIP_DEFLATED) as archive:
        doc = place.journal(update_id)
        if doc:
            doc = {k: v for k, v in doc.items() if k != "recovery_hash"}
            archive.writestr(f"{root}/journal.json", json.dumps(doc, indent=2, sort_keys=True))
        for name, path in files.items():
            found = read_json(path)
            if found is not None:
                archive.writestr(f"{root}/{name}", json.dumps(found, indent=2, sort_keys=True))


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

STYLE = """
body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f6f5f2;color:#1d1d1b}
main{max-width:46rem;margin:0 auto;padding:3rem 1rem}
h1{font-size:1.4rem;margin:0 0 1rem} h2{font-size:1.1rem;margin:2rem 0 .5rem}
.small{font-size:.85rem;color:#555} .danger{color:#a12a1a} .box{background:#fff;border:1px solid #ddd;border-radius:6px;padding:1rem;margin:.75rem 0}
pre{background:#fff;border:1px solid #ddd;padding:.5rem;overflow-x:auto;font-size:.8rem;white-space:pre-wrap}
input[type=text]{font:inherit;padding:.4rem;width:100%;max-width:24rem;box-sizing:border-box}
button{font:inherit;padding:.35rem .9rem;margin-top:.5rem}
@media (prefers-color-scheme:dark){body{background:#1b1b1a;color:#eee}.box,pre{background:#262624;border-color:#444}.small{color:#aaa}}
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title><style>{STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    ).encode()


def maintenance_body(mode: dict | None) -> str:
    if mode and mode["mode"] == "ledger_ahead":
        return f"<h1>{esc(TITLE)}</h1><p>{esc(AHEAD_SENTENCE)}</p>"
    link = ""
    if mode and mode["mode"] == "recovery":
        link = '<p class=small><a href="/recovery">Owner: open recovery</a></p>'
    return f"<h1>{esc(TITLE)}</h1><p>{esc(SENTENCE)}</p>{link}"


# --------------------------------------------------------------------------- #
# The server
# --------------------------------------------------------------------------- #


class Handler(http.server.BaseHTTPRequestHandler):
    place: Place
    server_version = "placard"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # A line per request, without the query: nothing typed is ever in a URL, but be sure.
        path = self.path.split("?", 1)[0] if isinstance(self.path, str) else ""
        print(f"{self.command} {path} {args[1] if len(args) > 1 else ''}", flush=True)

    # ------------------------------------------------------------------ #

    def send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8", headers: dict | None = None) -> None:
        self.send_response(status)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def maintenance(self) -> None:
        self.send(503, page(TITLE, maintenance_body(self.place.mode())), headers={"Retry-After": "60"})

    def not_found(self) -> None:
        self.send(404, page(TITLE, f"<h1>{esc(TITLE)}</h1><p>Not here.</p>"))

    def route(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def cookie(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return None

    def set_cookie(self, token: str, max_age: int) -> str:
        secure = "; Secure" if self.place.cookie_secure else ""
        return f"{COOKIE}={token}; Path=/recovery; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def form(self) -> dict[str, str] | None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return None
        if length < 0 or length > MAX_FORM:
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v for k, v in urllib.parse.parse_qsl(raw, keep_blank_values=True)}

    def same_origin(self) -> bool:
        """A POST from another origin is refused; SameSite=Strict covers the cookie, this the form."""
        origin = self.headers.get("Origin")
        if origin is None or origin == "null":
            return origin is None
        host = self.headers.get("Host") or ""
        return urllib.parse.urlsplit(origin).netloc == host

    def redirect(self, where: str, cookie: str | None = None) -> None:
        headers = {"Location": where}
        if cookie:
            headers["Set-Cookie"] = cookie
        self.send(303, b"", "text/plain; charset=utf-8", headers)

    # ------------------------------------------------------------------ #

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = self.route()
        if path == "/api/health":
            self.send(503, b'{"status":"unavailable"}', "application/json", {"Retry-After": "60"})
            return
        if path == "/recovery":
            self.recovery_page()
            return
        self.maintenance()

    def do_POST(self) -> None:  # noqa: N802
        path = self.route()
        mode = self.place.mode()
        if not path.startswith("/recovery/") or not mode or mode["mode"] != "recovery":
            self.maintenance()
            return
        if not self.same_origin():
            self.send(403, page(TITLE, "<p>That came from another site.</p>"))
            return
        form = self.form()
        if form is None:
            self.send(413, page(TITLE, "<p>That form is too large.</p>"))
            return
        if path == "/recovery/open":
            self.open_recovery(mode, form)
        elif path == "/recovery/act":
            self.act(form)
        elif path == "/recovery/close":
            self.place.sessions.pop(self.cookie() or "", None)
            self.redirect("/recovery", self.set_cookie("", 0))
        else:
            self.not_found()

    # ------------------------------------------------------------------ #
    # /recovery
    # ------------------------------------------------------------------ #

    def code_form(self, notice: str = "") -> bytes:
        place = self.place
        wait = place.refused_for()
        attempts = place.attempts()
        parts = [f"<h1>{esc(TITLE)}: recovery</h1>"]
        parts.append(
            "<p>The update could not finish, and putting the previous version back did not work "
            "either. Enter the recovery code you saved when you pressed <em>Update</em>.</p>"
        )
        if notice:
            parts.append(f'<p class="danger">{esc(notice)}</p>')
        if wait:
            minutes = max(1, (wait + 59) // 60)
            parts.append(
                f'<p class="danger">Too many wrong codes. Recovery refuses every code for {minutes} more '
                f"minute{'s' if minutes != 1 else ''}.</p>"
            )
        else:
            left = attempts["per_round"] - attempts["wrong"] % attempts["per_round"]
            if attempts["wrong"]:
                parts.append(f"<p class=small>{left} more wrong code{'s' if left != 1 else ''} before a pause.</p>")
            parts.append(
                '<form method=post action="/recovery/open">'
                '<label>Recovery code<br><input type=text name=code autocomplete=off autocapitalize=characters '
                'spellcheck=false required></label><br><button>Open recovery</button></form>'
            )
        parts.append('<p class=small><a href="/">Back</a></p>')
        return page(f"{TITLE}: recovery", "".join(parts))

    def recovery_page(self) -> None:
        mode = self.place.mode()
        if not mode or mode["mode"] != "recovery":
            self.not_found()
            return
        session = self.place.session(self.cookie())
        if session is None or session.update_id != mode["id"]:
            self.send(200, self.code_form())
            return
        self.send(200, self.dashboard(session))

    def open_recovery(self, mode: dict, form: dict[str, str]) -> None:
        code = re.sub(r"\s+", "", form.get("code", ""))
        if not mode["id"]:
            self.send(200, self.code_form("There is no update to recover here."))
            return
        if self.place.refused_for():
            self.send(429, self.code_form())
            return
        if not CODE.fullmatch(code):
            self.send(200, self.code_form("That is not a recovery code."))
            return
        answer = self.place.ask(mode["id"], "open", code)
        if answer.get("state") != "done":
            self.send(200, self.code_form(str(answer.get("sentence") or "Refused.")))
            return
        token = self.place.new_session(code, mode["id"])
        self.redirect("/recovery", self.set_cookie(token, SESSION_SECONDS))

    def act(self, form: dict[str, str]) -> None:
        session = self.place.session(self.cookie())
        if session is None or not secrets.compare_digest(form.get("csrf", ""), session.csrf):
            self.redirect("/recovery")
            return
        kind = form.get("kind", "")
        fields: dict[str, object] = {}
        if kind in ("restore_backup", "download_backup"):
            stamp = form.get("backup", "")
            if not BACKUP_STAMP.fullmatch(stamp):
                self.send(200, self.dashboard(session, "Choose one of the backups."))
                return
            fields["backup"] = stamp
        if kind == "download_backup":
            fields["include_key"] = form.get("include_key") == "yes"
        if kind == "start_matching":
            revision = form.get("revision", "")
            if not REVISION.fullmatch(revision):
                self.send(200, self.dashboard(session, "The ledger's revision could not be read."))
                return
            fields["revision"] = revision
        if kind not in (
            "retry_rollback",
            "restore_backup",
            "start_matching",
            "download_backup",
            "download_diagnostics",
            "leave_for_operator",
        ):
            self.redirect("/recovery")
            return
        answer = self.place.ask(session.update_id, kind, session.code, **fields)
        state = answer.get("state")
        sentence = str(answer.get("sentence") or "")
        if state in ("refused", "failed"):
            if answer.get("code") in ("wrong_code", "settled", "refused_for_now"):
                self.place.sessions.pop(self.cookie() or "", None)
            self.send(200, self.dashboard(session, sentence or "Refused."))
            return
        if kind == "download_backup":
            folder = self.place.data_dir / "backups" / str(fields["backup"])
            if not folder.is_dir() or folder.is_symlink():
                self.send(200, self.dashboard(session, "That backup is not in the ledger's volume any more."))
                return
            self.stream(f"spendtracker-update-{fields['backup']}.zip", lambda out: stream_backup(out, folder, str(fields["backup"]), bool(fields["include_key"])))
            return
        if kind == "download_diagnostics":
            self.stream(
                f"spendtracker-diagnostics-{session.update_id[:8]}.zip",
                lambda out: stream_diagnostics(out, self.place, session.update_id),
            )
            return
        if kind == "leave_for_operator":
            self.redirect("/recovery")
            return
        # An engine action was accepted: this page stops while the updater works.
        self.send(
            202,
            page(
                f"{TITLE}: recovery",
                f"<h1>{esc(TITLE)}: recovery</h1><p>{esc(sentence)}</p>"
                "<p>This page stops while the updater works. Reload in a minute or two: you will see "
                "Spend Tracker, or this page again with what happened.</p>",
            ),
        )

    def stream(self, filename: str, write: Callable[[object], None]) -> None:
        self.send_response(200)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        write(self.wfile)

    # ------------------------------------------------------------------ #

    def dashboard(self, session: Session, notice: str = "") -> bytes:
        place = self.place
        uid = session.update_id
        record = place.history(uid)
        ctx = place.journal(uid).get("context") or {}
        status = read_json(place.update_dir / "status.json") or {}
        state = record.get("state") or "running"
        left = state == "left_for_operator"
        out = [f"<h1>{esc(TITLE)}: recovery</h1>"]
        if notice:
            out.append(f'<p class="danger">{esc(notice)}</p>')
        out.append('<div class=box>')
        frm, to = record.get("from_version") or ctx.get("from_version"), record.get("to_version") or ctx.get("to_version")
        if frm and to:
            out.append(f"<p>The update from <b>{esc(frm)}</b> to <b>{esc(to)}</b>.</p>")
        if record.get("sentence"):
            out.append(f"<p>{esc(record['sentence'])}</p>")
        if record.get("failed_step"):
            out.append(f"<p class=small>It failed at step {esc(record['failed_step'])}.</p>")
        out.append("</div>")
        sentences = status.get("sentences") if status.get("id") == uid and isinstance(status.get("sentences"), list) else []
        if sentences:
            out.append("<h2>What the updater said</h2><pre>" + esc("\n".join(str(s) for s in sentences)) + "</pre>")
        tail = record.get("log_tail") if isinstance(record.get("log_tail"), list) else []
        if tail:
            out.append("<h2>The drill's log, last lines</h2><pre>" + esc("\n".join(str(s) for s in tail)) + "</pre>")
        restore_log = ctx.get("restore_log") if isinstance(ctx.get("restore_log"), list) else []
        if restore_log:
            out.append("<h2>The restore's log, last lines</h2><pre>" + esc("\n".join(str(s) for s in restore_log)) + "</pre>")
        actions = record.get("recovery") if isinstance(record.get("recovery"), list) else []
        if actions:
            lines = "\n".join(f"{a.get('at', '')}  {a.get('kind', '')}: {a.get('result', '')}. {a.get('sentence', '')}" for a in actions if isinstance(a, dict))
            out.append("<h2>Recovery so far</h2><pre>" + esc(lines) + "</pre>")

        hidden = f'<input type=hidden name=csrf value="{esc(session.csrf)}">'
        backups = place.backups()
        present = [b for b in backups if b["present"]]

        def choices(name: str) -> str:
            return "".join(
                f'<label><input type=radio name={name} value="{esc(b["stamp"])}"{" checked" if i == 0 else ""}> '
                f'{esc(b["stamp"])}, taken at {esc(b["version"])}'
                f'{", revision " + esc(b["revision"]) if b["revision"] else ""}</label><br>'
                for i, b in enumerate(present)
            )

        if left:
            out.append("<h2>Left to a terminal</h2><p>Recovery will not act on this update any more. From a terminal on the server:</p>")
            out.append("<pre>" + esc(SERVER_COMMANDS) + "</pre>")
        else:
            out.append("<h2>What you can do</h2>")
            out.append(
                '<div class=box><form method=post action="/recovery/act">' + hidden +
                "<input type=hidden name=kind value=retry_rollback><b>Retry the rollback.</b> "
                "The updater puts the previous version back again: the new one removed, the backup restored, "
                "the previous app started.<br><button>Retry the rollback</button></form></div>"
            )
            if present:
                out.append(
                    '<div class=box><form method=post action="/recovery/act">' + hidden +
                    "<input type=hidden name=kind value=restore_backup><b>Restore a different backup</b>, "
                    "with the version that took it, if that version is still on this machine.<br>"
                    + choices("backup") + "<button>Restore this backup</button></form></div>"
                )
            revision = place.ledger_revision()
            heads = place.known_heads(uid)
            if revision and revision in heads:
                out.append(
                    '<div class=box><form method=post action="/recovery/act">' + hidden +
                    f'<input type=hidden name=kind value=start_matching><input type=hidden name=revision value="{esc(revision)}">'
                    f"<b>Start the version that matches the ledger.</b> The ledger is at {esc(revision)}, which is "
                    f"{esc(heads[revision])}'s. Nothing is migrated.<br><button>Start {esc(heads[revision])}</button></form></div>"
                )
            elif revision:
                out.append(f"<p class=small>The ledger is at {esc(revision)}, which neither version of this update runs as it is.</p>")
        if present:
            out.append(
                '<div class=box><form method=post action="/recovery/act">' + hidden +
                "<input type=hidden name=kind value=download_backup><b>Download a backup</b> as a zip.<br>"
                + choices("backup") +
                '<label><input type=checkbox name=include_key value=yes> Include <code>secret.key</code></label>'
                f'<p class="small danger">{esc(KEY_WARNING)}</p><button>Download</button></form></div>'
            )
        out.append(
            '<div class=box><form method=post action="/recovery/act">' + hidden +
            "<input type=hidden name=kind value=download_diagnostics><b>Download the diagnostics</b>: "
            "the update's records and logs, no ledger data and no key.<br><button>Download</button></form></div>"
        )
        if not left:
            out.append(
                '<div class=box><form method=post action="/recovery/act">' + hidden +
                "<input type=hidden name=kind value=leave_for_operator><b>Stop and leave it to me.</b> "
                "The page stops acting on this update and shows the commands to finish from a terminal.<br>"
                "<button>Leave it to me</button></form></div>"
            )
        out.append('<form method=post action="/recovery/close">' + hidden + "<button>Close recovery</button></form>")
        return page(f"{TITLE}: recovery", "".join(out))


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], place: Place) -> None:
        if ":" in address[0]:
            import socket

            self.address_family = socket.AF_INET6
        handler = type("BoundHandler", (Handler,), {"place": place})
        super().__init__(address, handler)


def make_place(args: argparse.Namespace) -> Place:
    return Place(
        update_dir=Path(args.update_dir),
        data_dir=Path(args.data_dir),
        recovery=args.recovery,
        cookie_secure=truthy(os.environ.get("SPENDTRACKER_COOKIE_SECURE"), True),
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scripts.placard", description=__doc__.splitlines()[0])
    p.add_argument("--recovery", action="store_true", help="recovery mode: recovery/mode.json says for what")
    p.add_argument("--update-dir", default=os.environ.get("SPENDTRACKER_UPDATE_DIR") or DEFAULT_UPDATE_DIR)
    p.add_argument("--data-dir", default=os.environ.get("SPENDTRACKER_DATA_DIR") or DEFAULT_DATA_DIR)
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    host = args.host or bind_host()
    port = args.port if args.port is not None else bind_port()
    server = Server((host, port), make_place(args))
    print(f"The maintenance page{' (recovery mode)' if args.recovery else ''} on {host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
