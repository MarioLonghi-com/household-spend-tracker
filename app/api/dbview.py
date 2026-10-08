"""The database browser, mounted inside the app and behind its session check.

Datasette has no authentication of its own and will run arbitrary read-only SQL
for whoever reaches it, so on its own port it is only ever safe on loopback.
Mounted here it inherits the real thing: the same session cookie, which means
the same password, the same authenticator and the same 30-day device trust --
so it can go on the tailnet with everything else.

Three deliberate limits on top of that:

**Owner only.** The rest of the API is scoped per household and answers 404 for
one you are not in. Raw tables have no such notion: `transactions` is every
household at once. So this gives an owner more than the admin screen does --
that shows every household's name and id, this shows every household's whole
ledger, including ones they are not a member of. The owner runs the instance
and holds its database file on the host, so it is the same reach by another
door; a member it would give everybody's ledger, which is why it is the
owner's alone.

**The snapshot, never the live database.** `scripts/db_view.py` blanks every
password hash, TOTP secret, recovery-code hash and session token before writing
it. An owner reading their own ledger is expected; an owner pulling argon2
hashes out of a web console is not, and neither is a stolen session doing it.
It also keeps a reader off the file the server is writing to.

**Only if the snapshot exists.** Nothing is mounted until somebody has
deliberately run `make snapshot`. A deployment that never builds one never
serves this at all, and `SPENDTRACKER_DB_VIEW=off` turns it off outright.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from starlette.requests import HTTPConnection

from ..auth import cookies as cookie_names
from ..auth import sessions as session_service
from ..config import settings
from ..db import session_scope
from ..models import Role, User

MOUNT = "/db"


class _ViewerNotInstalled(RuntimeError):
    """Datasette is not in this install. Not an error in the app."""


def snapshot_path() -> Path:
    """Where `scripts/db_view.py` writes, and the only file this will ever open.

    Resolved per call rather than captured at import. As a module constant it
    took whichever data directory happened to be configured when this module was
    first imported -- so in the test suite it pointed at a real one or a
    temporary one depending on import order, and the test that checks the
    unmounted case could delete the developer's own snapshot. It also means a
    snapshot built while the server is running is found without a restart.
    """
    return Path(settings.data_dir) / "snapshot.sqlite3"


def enabled() -> bool:
    return str(getattr(settings, "db_view", "on")).lower() not in {"off", "0", "false", "no"}


def _refusal(status: int, detail: str) -> tuple[int, bytes]:
    return status, json.dumps({"detail": detail}).encode()


def _who(cookie: str | None) -> tuple[int, bytes] | None:
    """None when the caller may pass; otherwise the refusal to send back.

    Deliberately the same lookup `deps.current_user` uses, rather than a second
    reading of the cookie -- a copy would be one more place for the two to
    drift apart on what counts as a valid session.
    """
    with session_scope() as session:
        row = session_service.lookup(session, cookie)
        if row is None:
            return _refusal(401, "sign in first")
        user = session.get(User, row.user_id)
        if user is None or user.disabled_at is not None:
            return _refusal(401, "sign in first")
        if user.role is not Role.owner:
            return _refusal(403, "only the owner can browse the database")
        session_service.touch(session, row)
    return None


class GuardedDatasette:
    """An ASGI app that checks the session, then hands over to Datasette.

    Built on first use rather than at import: the snapshot is usually made after
    the server is already running, and Datasette wants the file to exist when it
    is constructed.
    """

    def __init__(self) -> None:
        self._inner: Any = None
        self._lock = asyncio.Lock()

    async def _datasette(self) -> Any:
        if self._inner is not None:
            return self._inner
        async with self._lock:
            if self._inner is None:
                try:
                    from datasette.app import Datasette
                except ImportError as exc:
                    # `datasette` is in requirements-dev.txt, not
                    # requirements.txt, and deliberately so: it drags in a dozen
                    # transitive packages for a read-only table browser. The
                    # import is reached only after the session check passes and
                    # the snapshot is found, so a normal production install
                    # never touches it -- but an operator who runs
                    # `make snapshot` and then opens /db used to get a bare 500
                    # with an ImportError behind it and no way to read that as
                    # "install one package".
                    raise _ViewerNotInstalled() from exc

                ds = Datasette(
                    [str(snapshot_path())],
                    settings={
                        # The file is a copy, but say so anyway: nothing here
                        # should ever be able to write.
                        "allow_download": False,
                        "suggest_facets": False,
                        "base_url": f"{MOUNT}/",
                    },
                )
                await ds.invoke_startup()
                self._inner = ds.app()
        return self._inner

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # pragma: no cover - no websockets here
            return

        # Starlette's own parsing, and the request's host: the session cookie's
        # name depends on it (#196), so this reads it the way every route does.
        refusal = await asyncio.to_thread(
            _who, cookie_names.session_value(HTTPConnection(scope))
        )
        if refusal is not None:
            await _send_json(send, *refusal)
            return

        if not snapshot_path().exists():
            await _send_json(
                send,
                404,
                json.dumps(
                    {"detail": "no snapshot yet -- run `make snapshot` to build one"}
                ).encode(),
            )
            return

        try:
            inner = await self._datasette()
        except _ViewerNotInstalled:
            await _send_json(
                send,
                *_refusal(
                    501,
                    "the /db viewer needs `pip install datasette`; the snapshot itself is fine",
                ),
            )
            return
        await inner(scope, receive, send)


async def _send_json(send: Any, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                # A browser that wandered here should not cache the refusal.
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
