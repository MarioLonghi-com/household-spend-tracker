"""A ceiling on every request body, applied before anything reads one. Issue #83.

FastAPI parses a body before it resolves a single dependency, so an
unauthenticated 30 MB of broken JSON came back `422 JSON decode error at loc
30000021` -- read and parsed in full before the 401 it deserved. The route caps
all ran after the damage.

These drive the app over raw ASGI rather than through the test client, because
the test client reads the whole request body into memory before the app sees a
byte -- which would make "nothing was read past the limit" untestable.
"""

from __future__ import annotations

import asyncio

from app import body_limit
from app.main import BASE_SECURITY_HEADERS

V1 = "/api/agent/v1"
BOGUS_KEY = {"authorization": "Bearer stk_not_a_real_key"}
MIB = 1024 * 1024


def _drive(app, *, path: str, headers: dict[str, str], chunks, method: str = "POST"):
    """Run one request through the ASGI app; say what came back and what was read."""
    read = {"bytes": 0, "calls": 0}
    sent: list[dict] = []
    source = iter(chunks)

    async def receive():
        read["calls"] += 1
        chunk = next(source, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        read["bytes"] += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver")]
        + [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 443),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    answered = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], answered, body, read


def _stream(total: int = 4 * MIB, chunk_size: int = 64 * 1024):
    """Finite on purpose: with the ceiling removed the app reads to the end and
    the test fails, rather than reading forever and hanging the run."""
    for _ in range(total // chunk_size):
        yield b"x" * chunk_size


def test_a_declared_200_mb_body_is_refused_before_a_byte_is_read(client):
    """The reproduction from the issue, with the length declared.

    No credential that means anything -- a made-up key is what waves a request
    past the CSRF check, and that is exactly the stranger this is for. Before
    the fix this read the lot and answered 422.
    """
    status, headers, body, read = _drive(
        client.app_module.app,
        path=f"{V1}/households/whatever/transactions/lookup",
        headers={**BOGUS_KEY, "content-type": "application/json",
                 "content-length": str(200 * MIB)},
        chunks=_stream(),
    )
    assert status == 413, body
    assert read["bytes"] == 0
    # And it is a refusal like any other: the security headers are on it.
    for name, value in BASE_SECURITY_HEADERS.items():
        assert headers[name.lower()] == value


def test_a_streamed_body_is_cut_off_just_past_the_ceiling(client):
    """No `Content-Length`: chunked, the way a client that wants to hide its size
    would send it. It is counted as it arrives and abandoned one chunk past the
    ceiling, not read to the end and measured.

    The setup wizard, because it is the one JSON route a stranger reaches on a
    fresh instance: public, past the setup gate, and past the CSRF check with
    nothing more than a matching `Origin`. FastAPI reads its body first."""
    status, headers, body, read = _drive(
        client.app_module.app,
        path="/api/setup/begin",
        headers={"origin": "https://testserver", "content-type": "application/json"},
        chunks=_stream(),
    )
    assert status == 413, body
    ceiling = body_limit.DEFAULT_CEILING
    assert ceiling < read["bytes"] <= ceiling + 64 * 1024
    assert "larger than this endpoint accepts" in body.decode()
    assert headers["x-frame-options"] == "DENY"


def test_the_ceiling_is_per_path(client):
    """A 20 MB receipt is a real thing to send; 20 MB of JSON to a lookup is not.

    The receipt upload is let through the ceiling (and then answered by the
    setup gate, which needs no body at all). The same length on a JSON route is
    a 413.
    """
    declared = {"content-type": "multipart/form-data; boundary=x",
                "content-length": str(20 * MIB)}
    status, _, _, _ = _drive(
        client.app_module.app,
        path="/api/households/whatever/receipts",
        headers=declared,
        chunks=iter(()),
    )
    assert status == 503

    status, _, _, read = _drive(
        client.app_module.app,
        path="/api/households/whatever/receipts/bulk-delete",
        headers=declared,
        chunks=iter(()),
    )
    assert status == 413
    assert read["bytes"] == 0


def test_every_ceiling_sits_above_the_cap_it_guards():
    """Derived from the route caps, so raising one cannot leave this refusing the
    files it just allowed. And the batch, whose honest maximum was 140 MB of
    JSON, now has a whole-body cap of its own."""
    from app.api.routers import agent, imports
    from app.services import account_import, receipts

    house = "/api/households/h1"
    assert body_limit.ceiling_for(f"{house}/receipts") > receipts.MAX_RECEIPT_BYTES
    assert body_limit.ceiling_for(f"{house}/imports") > imports.MAX_UPLOAD_BYTES
    assert body_limit.ceiling_for(f"{house}/imports/recognise") > imports.MAX_UPLOAD_BYTES
    # The accounts file's cap sits under the default, so it needs no entry of
    # its own -- until somebody raises it past a megabyte (#146).
    assert body_limit.ceiling_for(f"{house}/accounts/import") > account_import.MAX_ACCOUNT_CSV_BYTES
    base64_body = agent.MAX_BASE64_BYTES * 4 // 3
    assert body_limit.ceiling_for(f"{V1}/households/h1/receipts") > base64_body
    assert body_limit.ceiling_for(f"{V1}/imports/b1/document") > base64_body
    assert body_limit.ceiling_for(f"{V1}/households/h1/receipts/binary") > agent.MAX_BASE64_BYTES
    assert body_limit.ceiling_for(f"{V1}/households/h1/receipts/batch") == (
        agent.MAX_BATCH_BODY_BYTES
    )
    assert body_limit.ceiling_for(f"{V1}/households/h1/imports") == 8 * MIB
    # Everything else, including the paths that share an upload's prefix.
    for other in (f"{house}/receipts/bulk-delete", f"{house}/transactions", "/api/session"):
        assert body_limit.ceiling_for(other) == MIB, other


def test_the_batch_cap_is_in_the_manifest():
    """An agent learns the limit from the document, not from a 413."""
    from app.api.routers import agent

    entry = next(e for e in agent.ENDPOINTS if e.path.endswith("/receipts/batch"))
    assert f"{agent.MAX_BATCH_BODY_BYTES // MIB} MB" in entry.says


def test_a_declared_body_a_little_over_is_read_out_and_then_refused(client):
    """Within the drain, the body is read to its end -- so a client that writes
    before it reads gets to read the 413 (#40) -- and none of it reaches the app.
    """
    declared = 3 * MIB
    status, headers, body, read = _drive(
        client.app_module.app,
        path=f"{V1}/households/whatever/transactions/lookup",
        headers={**BOGUS_KEY, "content-type": "application/json",
                 "content-length": str(declared)},
        chunks=_stream(declared),
    )
    assert status == 413, body
    assert read["bytes"] == declared
    assert "Nothing in it was stored" in body.decode()
    assert headers["x-frame-options"] == "DENY"


def test_a_client_waiting_for_100_continue_is_refused_unread(client):
    status, _, _, read = _drive(
        client.app_module.app,
        path=f"{V1}/households/whatever/transactions/lookup",
        headers={**BOGUS_KEY, "content-type": "application/json",
                 "content-length": str(3 * MIB), "expect": "100-continue"},
        chunks=_stream(3 * MIB),
    )
    assert status == 413
    assert read["bytes"] == 0


def test_past_the_drain_a_declared_body_is_refused_unread(client):
    ceiling = body_limit.ceiling_for(f"{V1}/households/whatever/receipts/batch")
    status, _, _, read = _drive(
        client.app_module.app,
        path=f"{V1}/households/whatever/receipts/batch",
        headers={**BOGUS_KEY, "content-type": "application/json",
                 "content-length": str(body_limit.drain_limit(ceiling) + 1)},
        chunks=_stream(),
    )
    assert status == 413
    assert read["bytes"] == 0
