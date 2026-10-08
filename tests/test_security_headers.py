"""The headers every response carries, and the two places they are held back.

These are not a fix for a live bug: the client has no `dangerouslySetInnerHTML`,
no `innerHTML` and no `eval`, so there is nothing to exploit today. They are the
layer that is already there when somebody writes the first one -- and
`frame-ancestors 'none'` is not theoretical at all, because this app has
one-click irreversible actions and clickjacking needs no script bug.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.main import BASE_SECURITY_HEADERS


@pytest.mark.parametrize("name,value", sorted(BASE_SECURITY_HEADERS.items()))
def test_every_response_carries_the_base_headers(client, name, value):
    assert client.get("/api/health").headers[name] == value


def test_a_refusal_carries_them_too(client):
    """The middleware is outermost on purpose.

    Registered last, so it wraps the CSRF check and the setup gate rather than
    sitting inside them. Inside, a 403 from `refuse_cross_origin_writes` would
    go back bare -- and a refusal is exactly the response an attacker sees.
    """
    # `/api/session` on purpose: it is in PUBLIC_PREFIXES, so the setup gate
    # lets it through and the CSRF check is what answers.
    refused = client.post(
        "/api/session",
        json={"email": "jane@example.com", "password": "x"},
        headers={"Origin": "https://evil.example"},
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == "that request did not come from this app"
    assert refused.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in refused.headers["Content-Security-Policy"]

    # And the gate one layer out, which answers before the CSRF check does.
    gated = client.get("/api/me")
    assert gated.status_code == 503
    assert gated.headers["X-Content-Type-Options"] == "nosniff"


def test_the_policy_says_what_it_means(client):
    policy = client.get("/api/health").headers["Content-Security-Policy"]
    directives = dict(
        (part.split(" ", 1) + [""])[:2] for part in (p.strip() for p in policy.split(";")) if part
    )

    assert directives["frame-ancestors"] == "'none'"
    assert directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'none'"
    assert directives["default-src"] == "'self'"
    # No 'unsafe-inline' and no 'unsafe-eval' on scripts. The SPA is a built
    # module from /assets and needs neither.
    assert directives["script-src"] == "'self'"
    assert "unsafe" not in directives["script-src"]

    # style-src is the one concession, and it is deliberate: `theme.ts` paints a
    # household's palette into a <style> element's textContent. The values are
    # validated as hex before storage, so nothing arbitrary reaches it.
    assert "'unsafe-inline'" in directives["style-src"]

    # `blob:` on img-src is the receipt screens', and the only policy change
    # the whole receipt feature makes. It lets a page display bytes it already
    # holds under a URL it minted for itself, which is what shows a photograph
    # back while the server spends 0.6 to 5 seconds encoding it. Without it the
    # frame is a broken image and the only clue is a console violation -- on a
    # phone, at a till, which nobody is reading.
    #
    # And no `data:` (#94): nothing in the client uses a data URI, so allowing
    # one only widens what an injected <img> could carry.
    assert directives["img-src"] == "'self' blob:"
    assert "data:" not in directives["img-src"]


def test_the_whole_policy_string_is_pinned(client):
    """Not directive by directive -- the string.

    The test above proves each directive means what it should. This one fails
    when somebody *adds* one, or relaxes one nobody thought to assert, and it
    is what tells the next person editing the CSP that the receipt screens need
    `blob:` -- rather than their finding out from a broken image months later.
    """
    from app.main import CONTENT_SECURITY_POLICY

    assert "; ".join(
        (
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' blob:",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    ) == CONTENT_SECURITY_POLICY
    assert client.get("/api/health").headers["Content-Security-Policy"] == (
        CONTENT_SECURITY_POLICY
    )


def test_the_database_browser_is_left_out_of_the_policy(client):
    """Datasette renders its own pages with inline scripts.

    Our policy would break them rather than protect them, so `/db` keeps the
    base headers and loses the CSP. What guards it is what always guarded it:
    owner-only, and a redacted snapshot rather than the ledger.
    """
    for path in ("/db", "/db/", "/db/snapshot"):
        answer = client.get(path, follow_redirects=False)
        assert "Content-Security-Policy" not in answer.headers, path
        assert answer.headers["X-Frame-Options"] == "DENY", path

    # And the exemption is exactly those paths. A bare startswith("/db") would
    # have switched the policy off for a client-side route nobody chose.
    from app.main import _is_ours

    assert _is_ours("/dbsomething")
    assert _is_ours("/accounts")
    assert not _is_ours("/db")
    assert not _is_ours("/db/snapshot/users.json")


def test_hsts_is_sent_in_production_and_not_in_development(monkeypatch, tmp_path):
    """Pinning a developer's browser to HTTPS on a plain-HTTP localhost is a
    self-inflicted afternoon, so the one header that is sticky is gated."""

    def boot(environment: str) -> TestClient:
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / environment}.sqlite3")
        monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path / environment))
        monkeypatch.setenv("SPENDTRACKER_ENV", environment)

        import app.config as config

        importlib.reload(config)
        import app.db as db_module

        importlib.reload(db_module)
        from app.models import Base

        Base.metadata.create_all(db_module.engine)
        from tests.conftest import _stamp_head

        _stamp_head(db_module.engine)

        import app.main as main

        importlib.reload(main)
        return TestClient(main.app, base_url="https://testserver")

    with boot("production") as live:
        assert "max-age=" in live.get("/api/health").headers["Strict-Transport-Security"]

    with boot("development") as dev:
        assert "Strict-Transport-Security" not in dev.get("/api/health").headers


#: Two loopback hosts, as the browser writes them, and two that are not: the
#: tailnet name the deployment is reached at, and a LAN address. Every test of
#: the cookie names runs all four, so a rule that only ever looked at one side
#: cannot pass.
LOOPBACK = ("localhost:8848", "[::1]:8848")
ELSEWHERE = ("spend.example.ts.net", "192.168.1.50:8848")


def _request_to(host: str):
    """A bare request naming `host`, for the functions that read the name
    off a request rather than being handed one."""
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"host", host.encode())]})


def _issued(monkeypatch, host: str, *, cookie_secure: bool = True) -> tuple[list[str], list[str]]:
    """Every `Set-Cookie` the module writes for a request to `host`: the
    three it sets, then the two it clears."""
    import dataclasses

    from fastapi import Response

    from app.auth import cookies as cookie_module

    monkeypatch.setattr(
        cookie_module,
        "settings",
        dataclasses.replace(cookie_module.settings, cookie_secure=cookie_secure),
    )
    request = _request_to(host)
    made = Response()
    # All three, deliberately. `st_pending` used to set its own flags inline
    # in the router, so it kept `Secure` after the other two stopped -- and a
    # sign-in that holds no pending cookie fails on the *code* step, which
    # reads as a broken authenticator rather than a dropped cookie.
    cookie_module.set_session(made, request, "a-session-value")
    cookie_module.set_device(made, request, "a-device-value")
    cookie_module.set_pending(made, request, "a-pending-value")
    cleared = Response()
    cookie_module.clear_session(cleared, request)
    cookie_module.clear_pending(cleared, request)
    return made.headers.getlist("set-cookie"), cleared.headers.getlist("set-cookie")


def _names(headers: list[str]) -> list[str]:
    return [header.split("=", 1)[0] for header in headers]


def test_the_cookies_carry_secure_unless_the_lan_flag_turns_it_off(monkeypatch):
    """`Secure` is the flag that makes a LAN instance fail silently.

    A browser will not *store* a `Secure` cookie from `http://192.168.1.50:8848`
    -- localhost is trustworthy, a LAN address is not. So the sign-in answers
    200, the cookie is dropped without a word, and the app bounces back to the
    sign-in screen with nothing in either log. The flag exists so that instance
    can work; this test is what says it is still on for everybody else --
    loopback included, where only the *name* changes (#196).
    """
    for host in LOOPBACK + ELSEWHERE:
        on, _ = _issued(monkeypatch, host, cookie_secure=True)
        assert len(on) == 3, host
        for header in on:
            assert "Secure" in header, (host, header)
            # The two that are not negotiable whatever the transport is.
            # HttpOnly keeps a script off the session; SameSite=Lax is the
            # second half of the CSRF story the middleware tells.
            assert "HttpOnly" in header
            assert "SameSite=lax" in header

        off, _ = _issued(monkeypatch, host, cookie_secure=False)
        assert len(off) == 3, host
        for header in off:
            assert "Secure" not in header, (host, header)
            assert "HttpOnly" in header
            assert "SameSite=lax" in header


def test_the_names_carry_host_except_on_loopback(monkeypatch):
    """#209 and #196. `__Host-` is what stops another service on this host --
    cookies are not isolated by port -- planting its own session for the
    victim to land in. The browser only honours the prefix on a `Secure`
    cookie with `Path=/` and no `Domain`, so the name and the flag are one
    decision. Safari drops a prefixed cookie from `http://localhost` (WebKit
    218980) and loops at the sign-in, so on loopback the names go bare and
    `Secure` stays; everywhere else, the tailnet first, keeps the prefix."""
    prefixed = ["__Host-st_session", "__Host-st_device", "__Host-st_pending"]
    bare = ["st_session", "st_device", "st_pending"]

    for host in ELSEWHERE:
        made, cleared = _issued(monkeypatch, host)
        assert _names(made) == prefixed, host
        for header in made + cleared:
            # The prefix's own conditions, or the browser drops the cookie.
            assert "Secure" in header and "Path=/" in header and "Domain" not in header
        # A clear is an overwrite, which a browser refuses for __Host- unless
        # Secure -- and it has to be the name that was set, or it clears nothing.
        assert _names(cleared) == ["__Host-st_session", "__Host-st_pending"], host

    for host in LOOPBACK:
        made, cleared = _issued(monkeypatch, host)
        assert _names(made) == bare, host
        assert all("Secure" in header for header in made + cleared), host
        assert _names(cleared) == ["st_session", "st_pending"], host

    # With `Secure` off the prefix cannot be had anywhere, loopback or not.
    for host in LOOPBACK + ELSEWHERE:
        made, cleared = _issued(monkeypatch, host, cookie_secure=False)
        assert _names(made) == bare, host
        assert _names(cleared) == ["st_session", "st_pending"], host


def test_the_loopback_rule_reads_the_host_not_the_port():
    """`127.0.0.1` on any port is loopback; `127.0.0.1.example` and a
    `localhost` subdomain are names somebody else can own."""
    from app.auth import cookies
    from app.hosts import request_host

    hosts = {
        "localhost": True,
        "LOCALHOST:8851": True,
        "127.0.0.1:8848": True,
        "[::1]": True,
        "spend.example.ts.net": False,
        "localhost.example.ts.net": False,
        "127.0.0.1.example": False,
        "100.101.102.103:8848": False,
    }
    seen = {host: cookies.is_loopback(request_host(_request_to(host))) for host in hosts}
    assert seen == hosts


def _cookies_from(answer) -> dict[str, str]:
    """`name -> value` out of an answer's `Set-Cookie`s, a clear as `""`."""
    found = {}
    for header in answer.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        found[name] = rest.split(";", 1)[0].strip('"')
    return found


def _sign_in_at(client, base: str, secret: str, clock) -> dict[str, str]:
    """Password, then code, at `base` -- carrying the cookies by hand.

    By hand because the test client's jar will not send a `Secure` cookie
    over `http://`, and plain-HTTP `localhost` is the whole point; the jar is
    emptied first so nothing it held from another host rides along.
    """
    import pyotp

    from tests.conftest import HEADERS, PASSWORD

    client.cookies.clear()
    first = client.post(
        f"{base}/api/session",
        json={"email": "Jane.Doe@gmail.com", "password": PASSWORD},
        headers=HEADERS,
    )
    assert first.status_code == 200, first.text
    pending = _cookies_from(first)
    [(pending_name, pending_value)] = pending.items()

    client.cookies.clear()
    second = client.post(
        f"{base}/api/session/code",
        json={"code": pyotp.TOTP(secret).at(clock()), "trust_device": True},
        headers={**HEADERS, "cookie": f"{pending_name}={pending_value}"},
    )
    assert second.status_code == 200, second.text
    assert second.json()["authenticated"] is True
    return {"pending": pending_name, **_cookies_from(second)}


def test_sign_in_sets_the_name_its_host_reads_and_no_other(client, clock):
    """The server's half of #209 and #196, through the real routes.

    At `http://localhost` the sign-in issues `st_session` and reads only that;
    at the tailnet name it issues `__Host-st_session` and reads only that. A
    session carried under the other host's name is somebody else's cookie --
    on the tailnet that is the one another port planted -- and is not signed
    in. Sign-out clears the name it was signed in under.
    """
    from tests.conftest import HEADERS, _setup_owner

    owner = _setup_owner(client)
    at = {"http://localhost:8848": "", "https://spend.example.ts.net": "__Host-"}

    for base, prefix in at.items():
        issued = _sign_in_at(client, base, owner["secret"], clock)
        session_name, device_name = f"{prefix}st_session", f"{prefix}st_device"
        assert issued["pending"] == f"{prefix}st_pending", base
        # Exactly these: the session, the device, and the pending one cleared.
        assert set(issued) - {"pending"} == {session_name, device_name, issued["pending"]}
        assert issued[issued["pending"]] == "", base
        value = issued[session_name]
        assert value, base

        client.cookies.clear()
        mine = {"cookie": f"{session_name}={value}"}
        assert client.get(f"{base}/api/me", headers=mine).status_code == 200, base

        other = "st_session" if prefix else "__Host-st_session"
        client.cookies.clear()
        theirs = {"cookie": f"{other}={value}"}
        assert client.get(f"{base}/api/me", headers=theirs).status_code == 401, base

        client.cookies.clear()
        out = client.delete(f"{base}/api/session", headers={**HEADERS, **mine})
        assert out.status_code == 204, base
        assert _cookies_from(out) == {session_name: ""}, base
        # And the session is gone server-side, not only from the browser.
        client.cookies.clear()
        assert client.get(f"{base}/api/me", headers=mine).status_code == 401, base


def test_hsts_is_held_back_on_plain_http_loopback_only(client):
    """#196. A browser ignores HSTS over plain HTTP, so sending it to
    `http://localhost` -- the container on somebody's own computer, which is
    production -- is noise. Every other address keeps it, the tailnet first."""
    sent = {
        base: "Strict-Transport-Security" in client.get(f"{base}/api/health").headers
        for base in (
            "http://localhost:8848",
            "http://[::1]:8848",
            "https://spend.example.ts.net",
            "https://localhost:8848",
        )
    }
    assert sent == {
        "http://localhost:8848": False,
        "http://[::1]:8848": False,
        "https://spend.example.ts.net": True,
        "https://localhost:8848": True,
    }


# Reads the client's sources too, so it runs on every pull request.
@pytest.mark.repo_wide
def test_nothing_outside_the_cookie_module_spells_a_cookie_name():
    """Every reader asks `auth/cookies.py` for the name. One that spelled it
    would keep reading the bare name after the prefix arrived, and quietly
    stop recognising anybody -- or keep honouring a planted cookie."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    names = ("st_session", "st_device", "st_pending")
    offenders = [
        path.relative_to(root).as_posix()
        for folder in ("app", "scripts", "client/src", "client/e2e")
        for path in (root / folder).rglob("*")
        if path.suffix in {".py", ".ts", ".tsx"}
        and path.name != "cookies.py"
        and "node_modules" not in path.parts
        and any(name in path.read_text() for name in names)
    ]
    assert offenders == [], f"these spell a cookie name themselves: {offenders}"


def test_the_default_is_secure():
    """Nobody has to type anything to get the safe one."""
    from app.config import Settings

    assert Settings.cookie_secure is True


def test_hsts_is_not_sent_by_an_instance_that_is_deliberately_plain_http(
    monkeypatch, tmp_path
):
    """The two flags have to agree, and this is the pairing that bites.

    An instance with `cookie_secure` off is by definition serving plain HTTP at
    a LAN address. Sending HSTS from there pins every browser that loads it to
    HTTPS for that host and port for a year -- so the app becomes unreachable
    at the address it just handed out, and the cure is a trip into
    chrome://net-internals on each device. Production alone is not enough of a
    test.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'lan'}.sqlite3")
    monkeypatch.setenv("SPENDTRACKER_DATA_DIR", str(tmp_path / "lan"))
    monkeypatch.setenv("SPENDTRACKER_ENV", "production")
    monkeypatch.setenv("SPENDTRACKER_COOKIE_SECURE", "off")

    import app.config as config

    importlib.reload(config)
    import app.db as db_module

    importlib.reload(db_module)
    from app.models import Base

    Base.metadata.create_all(db_module.engine)
    from tests.conftest import _stamp_head

    _stamp_head(db_module.engine)

    import app.main as main

    importlib.reload(main)
    try:
        with TestClient(main.app, base_url="https://testserver") as live:
            answer = live.get("/api/health")
        assert answer.status_code == 200
        assert "Strict-Transport-Security" not in answer.headers
    finally:
        # Reloaded modules outlive monkeypatch's env restore, so put the
        # singletons back the way the rest of the suite expects to find them.
        monkeypatch.undo()
        importlib.reload(config)
        importlib.reload(db_module)
        importlib.reload(main)


def test_no_route_sets_a_cookie_behind_the_cookie_module_s_back():
    """The flags live in one place, and this is what keeps them there.

    `auth/cookies.py` says the flags "cannot drift apart" — which was not true
    while `app/api/routers/auth.py` called `response.set_cookie` itself for
    `st_pending`, with `secure=True` written out by hand. A grep is a blunt
    instrument, and it is the one that would have caught it.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = [
        path.relative_to(root.parent).as_posix()
        for path in root.rglob("*.py")
        if path.name != "cookies.py" and ".set_cookie(" in path.read_text()
    ]
    assert offenders == [], (
        f"these set a cookie outside app/auth/cookies.py: {offenders}. "
        "Add a helper there instead, so every cookie gets the same flags."
    )


def _permissions(response) -> dict[str, str]:
    """`camera=(), geolocation=(self)` as a dict, so a test can name one feature."""
    return dict(
        (part.split("=", 1) + [""])[:2]
        for part in (p.strip() for p in response.headers["Permissions-Policy"].split(","))
        if part
    )


def test_only_snap_may_ask_where_it_is(client):
    """The one page with a location toggle, and nowhere else.

    `geolocation=()` on every response made the toggle on `/snap` inert: the
    browser refuses the call before the user is ever asked, so pressing it
    looked like a refusal the user had caused. The permission is opened for
    that document alone, and `self` rather than `*` so nothing embedded
    inherits it.
    """
    # `follow_redirects=False` matters: signed out, `/snap` answers 303 to the
    # sign-in, and a followed redirect would report the headers of `/` -- which
    # are the ones this test exists to tell apart.
    snap = client.get("/snap", follow_redirects=False)
    assert _permissions(snap)["geolocation"] == "(self)"
    assert _permissions(client.get("/snap/snap.js"))["geolocation"] == "(self)"

    # The SPA, the API and the rest of the origin are unchanged.
    for path in ("/api/health", "/", "/api/openapi.json"):
        assert _permissions(client.get(path))["geolocation"] == "()", path

    # A path that merely starts with the same letters is not the capture page.
    assert _permissions(client.get("/snapshot", follow_redirects=False))["geolocation"] == "()"


def test_snap_keeps_every_other_restriction(client):
    """Only geolocation moved. Camera, microphone and payment stay shut.

    The camera is the surprising one: `/snap` photographs a receipt, and does
    it through a file input with `capture`, which is the OS camera app and not
    `getUserMedia`. Nothing on this origin needs the API.
    """
    answer = client.get("/snap", follow_redirects=False)
    snap = _permissions(answer)
    assert snap["camera"] == "()"
    assert snap["microphone"] == "()"
    assert snap["payment"] == "()"
    assert answer.headers["X-Frame-Options"] == "DENY"


def test_an_api_response_is_not_kept_in_the_browser_cache(client):
    """#194: JSON outlived sign-out in a shared browser's disk cache."""
    assert client.get("/api/health").headers["cache-control"] == "no-store"
    # A refusal too: the middleware is outermost.
    assert client.get("/api/me").headers["cache-control"] == "no-store"


def test_a_route_that_chose_its_own_caching_keeps_it(client):
    """Receipt bytes are content-addressed and cached for a year on purpose."""
    from tests.receipt_fixtures import with_exif
    from tests.test_receipt_routes import _ledger, _upload

    ledger = _ledger(client)
    made = _upload(client, ledger["ours"]["id"], with_exif(), transaction_id=ledger["one"]["id"])
    receipt = made.json()["receipt"]

    for role in ("thumb", "display"):
        got = client.get(f"/api/receipts/{receipt['id']}/{role}")
        assert got.status_code == 200, got.text
        assert got.headers["cache-control"] == "private, max-age=31536000, immutable"

    # Whereas the JSON about the same receipt is not kept.
    listed = client.get(f"/api/households/{ledger['ours']['id']}/receipts")
    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "no-store"
