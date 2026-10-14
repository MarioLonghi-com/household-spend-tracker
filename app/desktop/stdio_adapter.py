"""The desktop app's backend: this ASGI app, served over stdin and stdout (#319).

The desktop shell owns a private origin, `app://localhost`, and hands every
request made on it -- the SPA's files included, so FastAPI's CSP and headers
apply as they do on a server -- to this process. Nothing listens on a port, so
no other program on the machine, and no web page, can reach the ledger.

**Wire format, both directions:** frames of a 4-byte big-endian length and that
many bytes. A request is two frames, a JSON head and the body; so is an
answer. Heads carry an `id`, so answers may come back in any order.

    shell -> {"id": 7, "method": "GET", "path": "/api/me", "query": "", "headers": [[k, v], ...]}
    shell -> <body>
    here  -> {"id": 7, "status": 200, "headers": [[k, v], ...]}
    here  -> <body>

**Before any request,** one frame with id 0 says how the start went, from
`app.desktop.launch.prepare`:

    here  -> {"id": 0, "event": "ready",   "launch": {...}}    then requests are served
    here  -> {"id": 0, "event": "stopped", "launch": {...}}    and the process exits

When the ledger needs migrating, one more comes first, so the shell can say
"Upgrading your ledger…" for as long as that takes:

    here  -> {"id": 0, "event": "upgrading", "steps": 2}

`stopped` carries why: `newer` (a newer version wrote this ledger), `declined`
(a step that cannot be undone needs the person's yes -- the shell asks, and
starts this again with `SPENDTRACKER_ACCEPT_LOSSY=1`), `migration-failed` and
`rows-dropped` (the backup it names is where the ledger is), or `busy` (another
copy of the app holds this ledger).

**The shell's own requests** carry `control` where a request has a method --
`{"id": 9, "control": "backup"}` -- and are answered like any other, with a
JSON body (`app.desktop.control`). Only the shell writes frames, so no page
can send one.

**Restore** is a start of its own: with `SPENDTRACKER_RESTORE_FROM=<file>` the
process restores that backup (`app.desktop.restore`), says so in one frame,
and exits without serving anything:

    here  -> {"id": 0, "event": "restored", "restore": {...}}             exit 0
    here  -> {"id": 0, "event": "stopped",  "restore": {...}}             exit 3

**Standard output is the frames and nothing else.** File descriptor 1 is kept
for them and then pointed at standard error, so a stray write -- this app's, a
library's, a C extension's -- lands in the log rather than in a frame. stdin
closing is the signal to stop: the app's shutdown runs and the process exits,
so it never outlives the shell.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import struct
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote

_LENGTH = struct.Struct(">I")

#: What the desktop shell sends as `Host`, and the scheme its origin uses.
HOST = "localhost"

ACCEPT_LOSSY = "SPENDTRACKER_ACCEPT_LOSSY"
RESTORE_FROM = "SPENDTRACKER_RESTORE_FROM"


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #


def encode(head: dict, body: bytes = b"") -> bytes:
    raw = json.dumps(head, ensure_ascii=False).encode("utf-8")
    return _LENGTH.pack(len(raw)) + raw + _LENGTH.pack(len(body)) + body


def _read_exact(stream: BinaryIO, size: int) -> bytes | None:
    chunks, left = [], size
    while left:
        chunk = stream.read(left)
        if not chunk:
            return None
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO) -> bytes | None:
    """One frame, or None at the end of the stream."""
    length = _read_exact(stream, _LENGTH.size)
    if length is None:
        return None
    return _read_exact(stream, _LENGTH.unpack(length)[0])


def read_message(stream: BinaryIO) -> tuple[dict, bytes] | None:
    """A head and its body, or None at the end of the stream."""
    head = read_frame(stream)
    if head is None:
        return None
    body = read_frame(stream)
    if body is None:
        return None
    return json.loads(head), body


# --------------------------------------------------------------------------- #
# One request
# --------------------------------------------------------------------------- #


def scope_for(head: dict) -> dict:
    """The ASGI scope for a request the shell forwarded.

    `Host` is always `localhost` -- the shell's origin -- whatever the head
    said, and there is no client address: nothing reached this over a network,
    and the rate limiter counts by account when there is no address. The path
    arrives percent-encoded, as on the wire; the scope wants it decoded, with
    the original kept as `raw_path`, as uvicorn does.
    """
    raw_path = head["path"]
    headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in head.get("headers", [])
        if key.lower() != "host"
    ]
    headers.append((b"host", HOST.encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": head["method"].upper(),
        "scheme": "https",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("utf-8"),
        "query_string": head.get("query", "").encode("latin-1"),
        "root_path": "",
        "headers": headers,
        "client": None,
        "server": (HOST, 443),
        "state": {},
    }


async def handle(app, head: dict, body: bytes) -> tuple[dict, bytes]:
    """Run one request through the app. Always answers, if only with a 500."""
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if delivered:
            # Nothing more will come: wait as a client that is still connected.
            await asyncio.Event().wait()
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    status, out_headers, chunks = 500, [], []

    async def send(message: dict) -> None:
        nonlocal status, out_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            out_headers = [[k.decode("latin-1"), v.decode("latin-1")] for k, v in message.get("headers", [])]
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    try:
        await app(scope_for(head), receive, send)
    except Exception as problem:  # noqa: BLE001 - the shell must always get an answer
        sys.stderr.write(f"stdio_adapter: {head.get('method')} {head.get('path')} raised {problem!r}\n")
        status, out_headers, chunks = 500, [["content-type", "text/plain; charset=utf-8"]], [b"internal error"]
    return {"id": head["id"], "status": status, "headers": out_headers}, b"".join(chunks)


def serve_control(head: dict) -> tuple[dict, bytes]:
    """One of the shell's own requests (`app.desktop.control`). Blocking:
    run it off the event loop."""
    from app.desktop import control

    command = control.COMMANDS.get(head.get("control"))
    if command is None:
        status, said = 404, {"detail": f"no control called {head.get('control')!r}"}
    else:
        try:
            status, said = 200, command()
        except control.ControlRefused as refused:
            status, said = 422, {"detail": str(refused)}
        except Exception as problem:  # noqa: BLE001 - the shell must always get an answer
            sys.stderr.write(f"stdio_adapter: control {head.get('control')} raised {problem!r}\n")
            status, said = 500, {"detail": "internal error"}
    body = json.dumps(said, ensure_ascii=False).encode("utf-8")
    return {"id": head["id"], "status": status, "headers": [["content-type", "application/json"]]}, body


# --------------------------------------------------------------------------- #
# The app's lifespan
# --------------------------------------------------------------------------- #


class Lifespan:
    """Startup and shutdown, driven by hand: there is no server to do it."""

    def __init__(self, app) -> None:
        self._app = app
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._said: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None

    async def _receive(self) -> dict:
        return await self._inbox.get()

    async def _send(self, message: dict) -> None:
        await self._said.put(message)

    async def startup(self) -> None:
        scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        await self._inbox.put({"type": "lifespan.startup"})
        said = await self._said.get()
        if said["type"] != "lifespan.startup.complete":
            raise RuntimeError(said.get("message") or "the app did not start")

    async def shutdown(self) -> None:
        await self._inbox.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(self._said.get(), timeout=30)
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=30)


# --------------------------------------------------------------------------- #
# The process
# --------------------------------------------------------------------------- #


def _take_stdout() -> BinaryIO:
    """The frame channel: a private copy of fd 1, then fd 1 sent to stderr."""
    frames = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return frames


#: What the app serves, pinned: Python's table already says so, but on Windows
#: `mimetypes` also reads the registry, where another program can have mapped
#: `.js` to `text/plain` -- and a module script served as that is refused.
SERVED_TYPES = (
    ("text/javascript", ".js"),
    ("text/javascript", ".mjs"),
    ("text/css", ".css"),
    ("text/html", ".html"),
    ("application/json", ".json"),
    ("application/manifest+json", ".webmanifest"),
    ("image/svg+xml", ".svg"),
    ("image/png", ".png"),
    ("image/avif", ".avif"),
    ("font/woff2", ".woff2"),
)


def _own_mime_types() -> None:
    """Python's own MIME table, never the system's files.

    The first guess otherwise reads `/etc/apache2/mime.types` and its
    siblings, which the Mac App Store sandbox refuses with EPERM -- so every
    file response, the SPA itself, failed with a 500 while JSON worked (found
    in the sandbox, #59). `init(files=[])` is not enough: it *adds* to the
    system list. The list itself is emptied first.
    """
    mimetypes.knownfiles = []
    mimetypes.init()
    for kind, extension in SERVED_TYPES:
        mimetypes.add_type(kind, extension)


def _refuse(sentence: str) -> int:
    sys.stderr.write(f"stdio_adapter: {sentence}\n")
    return 2


def main() -> int:
    if not sys.flags.utf8_mode:
        # Windows pipes speak the ANSI code page otherwise, and an accented
        # payee is exactly what must survive them (PEP 686 makes this the
        # default only in Python 3.15).
        return _refuse("start with PYTHONUTF8=1")
    frames = _take_stdout()
    lock = threading.Lock()

    def answer(head: dict, body: bytes = b"") -> None:
        with lock:
            frames.write(encode(head, body))

    from app import config, instance_lock

    if config.settings.profile != "desktop":
        return _refuse("this serves the desktop profile only (SPENDTRACKER_PROFILE=desktop)")

    restoring = os.environ.get(RESTORE_FROM, "").strip()
    if restoring:
        return _restore(restoring, answer)

    from app.desktop import launch

    accept = os.environ.get(ACCEPT_LOSSY, "").strip().lower() in {"1", "true", "yes"}
    try:
        prepared = launch.prepare(
            confirm_lossy=lambda _titles: accept,
            starting=lambda steps: answer({"id": 0, "event": "upgrading", "steps": steps}),
        )
    except instance_lock.LedgerBusy as busy:
        answer({"id": 0, "event": "stopped", "launch": {"outcome": "busy", "log": [str(busy)]}})
        return 3
    said = {**asdict(prepared), "backup": str(prepared.backup) if prepared.backup else None}
    if not prepared.ok:
        answer({"id": 0, "event": "stopped", "launch": said})
        return 3

    _own_mime_types()
    from app.main import app

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    lifespan = Lifespan(app)
    loop.run_until_complete(lifespan.startup())
    answer({"id": 0, "event": "ready", "launch": said})

    stdin = sys.stdin.buffer

    async def serve(head: dict, body: bytes) -> None:
        if "control" in head:
            answer(*await loop.run_in_executor(None, serve_control, head))
        else:
            answer(*await handle(app, head, body))

    def reader() -> None:
        while (message := read_message(stdin)) is not None:
            asyncio.run_coroutine_threadsafe(serve(*message), loop)
        loop.call_soon_threadsafe(loop.stop)

    threading.Thread(target=reader, name="stdin", daemon=True).start()
    loop.run_forever()
    loop.run_until_complete(lifespan.shutdown())
    return 0


def _restore(source: str, answer) -> int:
    from app import instance_lock
    from app.desktop import restore

    try:
        done = restore.restore(Path(source))
    except instance_lock.LedgerBusy as busy:
        done = restore.Restored(restore.REFUSED, str(busy))
    answer({"id": 0, "event": "restored" if done.ok else "stopped", "restore": asdict(done)})
    return 0 if done.ok else 3


if __name__ == "__main__":
    sys.exit(main())
