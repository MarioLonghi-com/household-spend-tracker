"""A 413 a client can actually read, over a real socket (#40).

The batch route's ceiling answered at once, from the declared length, with
`Connection: close` -- and closed the socket while the client was still
writing its 38 MB. A client that sends its whole body before it reads
(Python's `http.client`, `requests`, most of them) saw `BrokenPipeError` or a
reset, never the 413, and could not tell whether anything was stored. These
run uvicorn on a loopback port, because the failure lives in the socket and
no in-process client has one.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest
import uvicorn

from app import body_limit

V1 = "/api/agent/v1"
MIB = 1024 * 1024


@pytest.fixture()
def served(client):
    """The app on a loopback port, for the length of one test."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            client.app_module.app, host="127.0.0.1", port=port, lifespan="off", log_level="error"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    yield port
    server.should_exit = True
    thread.join(timeout=10)


def _post(port: int, path: str, size: int) -> tuple[int, dict]:
    """Send the whole body, then read the answer: what a plain client does."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    body = b'{"receipts": [' + b" " * (size - 16) + b"]}"
    conn.request(
        "POST",
        path,
        body=body,
        headers={
            "Authorization": "Bearer stk_not_a_real_key",
            "Content-Type": "application/json",
            "Host": "testserver",
        },
    )
    answer = conn.getresponse()
    status, payload = answer.status, json.loads(answer.read() or b"{}")
    conn.close()
    return status, payload


def test_an_oversized_batch_is_answered_413_not_dropped(served):
    """The issue's shape: about 38 MB of JSON to the batch, length declared."""
    status, payload = _post(served, f"{V1}/households/h1/receipts/batch", 38 * MIB)
    assert status == 413
    assert "larger than this endpoint accepts (32 MB)" in payload["detail"]


def test_an_oversized_json_body_elsewhere_is_answered_too(served):
    status, payload = _post(served, f"{V1}/households/h1/transactions/lookup", 3 * MIB)
    assert status == 413
    assert "(1 MB)" in payload["detail"]


def test_past_the_drain_the_socket_is_closed_without_reading(served):
    """A declared length far past anything honest is not read to be refused.

    Draining is for a client that overshot; a stranger declaring a gigabyte
    gets the old answer -- refused before a byte is read, and the socket
    closed -- because reading it would be doing their bidding.
    """
    ceiling = body_limit.ceiling_for(f"{V1}/households/h1/receipts/batch")
    assert body_limit.drain_limit(ceiling) < 1024 * MIB
    with socket.create_connection(("127.0.0.1", served), timeout=10) as raw:
        raw.sendall(
            b"POST " + f"{V1}/households/h1/receipts/batch".encode() + b" HTTP/1.1\r\n"
            b"Host: testserver\r\nAuthorization: Bearer stk_not_a_real_key\r\n"
            b"Content-Type: application/json\r\nContent-Length: "
            + str(1024 * MIB).encode()
            + b"\r\n\r\n"
        )
        answer = raw.recv(65536)
    assert answer.startswith(b"HTTP/1.1 413")
