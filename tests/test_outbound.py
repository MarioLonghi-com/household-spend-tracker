"""The two outbound GETs follow no redirect and read nothing unbounded (#219, #225).

Real sockets on 127.0.0.1, not a patched ``urlopen``: the bug was in what
urllib's own redirect handler does, so replacing urllib would test nothing.
"""

from __future__ import annotations

import http.server
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.services import platform as platform_service

Handler = Callable[[http.server.BaseHTTPRequestHandler], None]


@contextmanager
def local_server(answer: Handler) -> Iterator[tuple[str, list[dict]]]:
    """An http server on a free port: its base URL, and every request it saw."""
    seen: list[dict] = []

    class One(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the stdlib's name
            seen.append({"path": self.path, "headers": dict(self.headers)})
            answer(self)

        def log_message(self, *args):  # quiet
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), One)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


def redirect_to(target: str) -> Handler:
    def answer(handler):
        handler.send_response(302)
        handler.send_header("Location", f"{target}/stolen")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return answer


def body(payload: bytes) -> Handler:
    def answer(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

    return answer


def drip(every: float, total: int) -> Handler:
    """A byte at a time: each recv is quick, the answer never finishes."""

    def answer(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(total))
        handler.end_headers()
        try:
            for _ in range(total):
                handler.wfile.write(b" ")
                handler.wfile.flush()
                time.sleep(every)
        except OSError:
            pass

    return answer


def test_check_upstream_follows_no_redirect(monkeypatch):
    with (
        local_server(body(b'[{"name": "v99.0.0"}]')) as (elsewhere, reached),
        local_server(redirect_to(elsewhere)) as (redirector, asked),
    ):
        monkeypatch.setattr(platform_service, "UPSTREAM_TAGS", f"{redirector}/tags")
        answer = platform_service.check_upstream()
    assert len(asked) == 1
    assert reached == []
    assert answer.latest is None
    assert answer.problem is not None and "302" in answer.problem


def test_check_upstream_reads_no_more_than_a_tag_list(monkeypatch):
    """2 MiB is not a tag list; before #225 it was read whole, whatever its size."""
    with local_server(body(b"[" + b" " * (2 << 20) + b"]")) as (server, asked):
        monkeypatch.setattr(platform_service, "UPSTREAM_TAGS", f"{server}/tags")
        answer = platform_service.check_upstream()
    assert len(asked) == 1
    assert answer.latest is None
    assert answer.problem == "the repository's answer was larger than a tag list"


def test_check_upstream_still_reads_a_real_tag_list(monkeypatch):
    tags = b'[{"name": "v0.3.1"}, {"name": "v99.1.0"}, {"name": "demo"}]'
    with local_server(body(tags)) as (server, _):
        monkeypatch.setattr(platform_service, "UPSTREAM_TAGS", f"{server}/tags")
        answer = platform_service.check_upstream()
    assert (answer.problem, answer.latest, answer.newer) == (None, "v99.1.0", True)
