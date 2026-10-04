"""The maintenance page `make upgrade` serves, and what `make lan` exposes. Issue #95.

It puts alembic's stdout and stderr in front of whoever loads the address, and
it formatted them into `<pre>{log}</pre>` as they came. A migration that prints
anything shaped like markup -- a column default, a quoted value, an error
message echoing input -- was HTML on that page, with no CSP behind it.
"""

from __future__ import annotations

import http.client
import http.server
import threading

from scripts import upgrade

HOSTILE = '<script>alert("from a migration")</script><img src=x onerror=alert(1)>'


def test_the_log_is_escaped_on_the_page():
    page = upgrade.render_placard(["running upgrade abc -> def", HOSTILE]).decode()
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;alert(&quot;from a migration&quot;)&lt;/script&gt;" in page
    # And the line is still there to read.
    assert "running upgrade abc -&gt; def" in page


def test_the_placard_is_served_with_a_policy_that_runs_nothing():
    say = upgrade.Step()
    say.lines.append(HOSTILE)
    handler = type("TestPlacard", (upgrade.Placard,), {"say": say})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request("GET", "/")
        answer = connection.getresponse()
        body = answer.read().decode()
    finally:
        server.shutdown()
        server.server_close()

    assert answer.status == 503
    policy = answer.getheader("Content-Security-Policy")
    assert policy is not None
    directives = dict(part.strip().split(" ", 1) for part in policy.split(";"))
    assert directives["default-src"] == "'none'"
    assert "script-src" not in directives, "default-src 'none' is what keeps scripts out"
    assert directives["style-src"] == "'unsafe-inline'"
    assert "<script>" not in body


def test_make_lan_keeps_the_api_docs_off_unless_asked():
    """`make lan` binds 0.0.0.0, and it used to set SPENDTRACKER_ENV=development,
    which mounts /api/docs, /api/redoc and /api/openapi.json for anyone on the
    network. Read from `make -n`, which prints the recipe without running it."""
    import pathlib
    import shutil
    import subprocess

    import pytest

    if shutil.which("make") is None:  # pragma: no cover - CI and dev machines have it
        pytest.skip("no make")
    root = pathlib.Path(__file__).resolve().parent.parent

    def recipe(*extra: str) -> str:
        return subprocess.run(
            ["make", "-n", "lan", *extra], cwd=root, capture_output=True, text=True, check=True
        ).stdout

    plain = recipe()
    assert "--host 0.0.0.0" in plain
    assert "SPENDTRACKER_ENV=production" in plain
    assert "development" not in plain
    assert "SPENDTRACKER_ENV=development" in recipe("DOCS=1")
