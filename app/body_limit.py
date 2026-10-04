"""A ceiling on every request body, applied before anything reads one.

Every route that takes a file already had a cap -- `refuse_declared_size`,
`read_capped`, the agent's `_decoded` -- and every one of them ran too late.
FastAPI parses a JSON or multipart body *before* it resolves a single
dependency, and Starlette spools file parts to disk with no limit of its own.
So an unauthenticated POST of 30 MB of broken JSON to an agent route came back
`422 JSON decode error at loc 30000021`: the whole body had been read and parsed
before anything asked who was sending it, and the 401 it deserved never got a
turn. The route-level caps stay; they are what give a *member* a sentence about
their file. This is what stops a stranger spending the memory first.

Pure ASGI rather than `@app.middleware("http")`, because the second half of the
job cannot be done from there: a body with no `Content-Length` (chunked) can
only be measured as it arrives, which means wrapping `receive`, and
`BaseHTTPMiddleware` gives no way to substitute it.

The ceilings are **derived from the route caps they sit above**, not typed in a
second time, so raising a cap in its own module cannot leave this refusing the
files it just allowed. Each carries a megabyte of slack for multipart framing,
form fields and JSON keys; the route's own check remains the precise one.
"""

from __future__ import annotations

import json
import re

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api.routers import agent as agent_router
from .api.routers import imports as imports_router
from .services import receipts as receipt_service
from .services.one_time_import import ynab_source

MIB = 1024 * 1024

#: Framing, form fields and JSON keys around the payload a route actually caps.
SLACK = MIB

#: Everything that is not listed below. The largest ordinary JSON body in the
#: API is an agent's list of up to 1,000 ids or category assignments, which is
#: tens of kilobytes; a megabyte is room for twenty times that.
DEFAULT_CEILING = MIB

#: The base64 form of the agent's per-receipt cap, as `_decoded` measures it.
_BASE64_BODY = agent_router.MAX_BASE64_BYTES * 4 // 3 + 16

_HOUSE = r"/api/households/[^/]+"
_AGENT = rf"/api/agent/v{agent_router.API_VERSION}"

#: (pattern, ceiling), matched against the path in order; first match wins.
#: Anchored at both ends so `/receipts/bulk-delete` is not mistaken for the
#: upload that shares its prefix.
CEILINGS: tuple[tuple[re.Pattern[str], int], ...] = tuple(
    (re.compile(f"^{pattern}$"), ceiling)
    for pattern, ceiling in (
        # The browser's multipart doors.
        (rf"{_HOUSE}/receipts", receipt_service.MAX_RECEIPT_BYTES + SLACK),
        (rf"{_HOUSE}/imports", imports_router.MAX_UPLOAD_BYTES + SLACK),
        (rf"{_HOUSE}/imports/recognise", imports_router.MAX_UPLOAD_BYTES + SLACK),
        # A YNAB export, resent on every step of the one-time import (#183).
        (
            rf"{_HOUSE}/one-time-import/ynab/(analyse|preview|commit)",
            ynab_source.MAX_FILE_BYTES + SLACK,
        ),
        # The agent's receipt doors: base64 JSON, raw bytes, and the array.
        (rf"{_AGENT}/households/[^/]+/receipts", _BASE64_BODY + SLACK),
        (rf"{_AGENT}/imports/[^/]+/document", _BASE64_BODY + SLACK),
        (rf"{_AGENT}/households/[^/]+/receipts/binary", agent_router.MAX_BASE64_BYTES + SLACK),
        (rf"{_AGENT}/households/[^/]+/receipts/batch", agent_router.MAX_BATCH_BODY_BYTES),
        # Up to 1,000 rows, each with a 500-character memo, a 200-character
        # payee and external id, and a free-form `details` object. A full batch
        # of long rows is a few megabytes of JSON.
        (rf"{_AGENT}/households/[^/]+/imports", 8 * MIB),
    )
)

def ceiling_for(path: str) -> int:
    for pattern, ceiling in CEILINGS:
        if pattern.match(path):
            return ceiling
    return DEFAULT_CEILING


def _sentence(ceiling: int) -> str:
    return (
        f"that request body is larger than this endpoint accepts "
        f"({ceiling // MIB} MB). Nothing was read past the limit."
    )


class BodyTooLarge(HTTPException):
    """Raised from inside `receive` when a streamed body crosses its ceiling.

    FastAPI's `HTTPException` -- not Starlette's, which it does not catch there --
    rather than a private type, because FastAPI turns any *other* exception raised while it reads a body into `400 There was an error
    parsing the body` -- which would tell a client its JSON was wrong when the
    truth is that it was too long.
    """

    def __init__(self, ceiling: int) -> None:
        super().__init__(status_code=413, detail=_sentence(ceiling))


class BodyLimit:
    """413 on a declared length over the path's ceiling; count it otherwise."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Every method, GET included: a route that declares a body gets it
        # parsed whatever the verb, and counting costs one addition per chunk.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        ceiling = ceiling_for(scope.get("path", ""))

        declared = None
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                declared = value
                break
        if declared is not None:
            if not declared.isdigit():
                await self._refuse(send, 400, "Content-Length is not a number")
                return
            if int(declared) > ceiling:
                await self._refuse(send, 413, _sentence(ceiling))
                return

        # Counted even when a length was declared: a client that declares 10
        # bytes and sends a gigabyte is exactly the one to distrust, and
        # uvicorn's own check on that is a detail of one server.
        received = 0
        exceeded = False
        started = False

        async def counted() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > ceiling:
                    exceeded = True
                    raise BodyTooLarge(ceiling)
            return message

        async def watched(message: Message) -> None:
            nonlocal started
            if exceeded:
                # Whatever the app made of the exception is replaced, not
                # trusted to have been a 413. Raised from inside `receive`, it
                # crosses the task group each `BaseHTTPMiddleware` runs its
                # `receive` in, arrives at FastAPI as an `ExceptionGroup`, and
                # FastAPI calls that "400 There was an error parsing the body".
                if message["type"] == "http.response.start" and not started:
                    started = True
                    await self._refuse(send, 413, _sentence(ceiling))
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except Exception:
            # A reader that let the refusal escape rather than answering. If a
            # response has already gone out there is nothing honest left to
            # send.
            if not exceeded or started:
                raise
            await self._refuse(send, 413, _sentence(ceiling))

    @staticmethod
    async def _refuse(send: Send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
