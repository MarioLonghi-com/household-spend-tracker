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


def test_the_cookies_carry_secure_unless_the_lan_flag_turns_it_off(monkeypatch):
    """`Secure` is the flag that makes a LAN instance fail silently.

    A browser will not *store* a `Secure` cookie from `http://192.168.1.50:8848`
    -- localhost is trustworthy, a LAN address is not. So the sign-in answers
    200, the cookie is dropped without a word, and the app bounces back to the
    sign-in screen with nothing in either log. The flag exists so that instance
    can work; this test is what says it is still on for everybody else.
    """
    import dataclasses

    from fastapi import Response

    from app.auth import cookies as cookie_module

    def cookies_set_with(*, cookie_secure: bool) -> list[str]:
        monkeypatch.setattr(
            cookie_module,
            "settings",
            dataclasses.replace(cookie_module.settings, cookie_secure=cookie_secure),
        )
        response = Response()
        # All three, deliberately. `st_pending` used to set its own flags inline
        # in the router, so it kept `Secure` after the other two stopped -- and
        # a sign-in that holds no pending cookie fails on the *code* step,
        # which reads as a broken authenticator rather than a dropped cookie.
        cookie_module.set_session(response, "a-session-value")
        cookie_module.set_device(response, "a-device-value")
        cookie_module.set_pending(response, "a-pending-value")
        return response.headers.getlist("set-cookie")

    on = cookies_set_with(cookie_secure=True)
    assert len(on) == 3
    for header in on:
        assert "Secure" in header
        # The two that are not negotiable whatever the transport is. HttpOnly
        # keeps a script off the session; SameSite=Lax is the second half of
        # the CSRF story the middleware tells.
        assert "HttpOnly" in header
        assert "SameSite=lax" in header

    off = cookies_set_with(cookie_secure=False)
    assert len(off) == 3
    for header in off:
        assert "Secure" not in header
        assert "HttpOnly" in header
        assert "SameSite=lax" in header


def test_the_cookie_names_carry_host_exactly_when_the_cookies_are_secure(monkeypatch):
    """#209. `__Host-` is what stops another service on this host -- cookies
    are not isolated by port -- planting its own session for the victim to land
    in. The browser only honours the prefix on a `Secure` cookie with `Path=/`
    and no `Domain`, so the name and the flag are one decision."""
    import dataclasses

    from fastapi import Response

    from app.auth import cookies as cookie_module

    def issued_with(*, cookie_secure: bool) -> tuple[list[str], list[str]]:
        monkeypatch.setattr(
            cookie_module,
            "settings",
            dataclasses.replace(cookie_module.settings, cookie_secure=cookie_secure),
        )
        made = Response()
        cookie_module.set_session(made, "a-session-value")
        cookie_module.set_device(made, "a-device-value")
        cookie_module.set_pending(made, "a-pending-value")
        cleared = Response()
        cookie_module.clear_session(cleared)
        cookie_module.clear_pending(cleared)
        return made.headers.getlist("set-cookie"), cleared.headers.getlist("set-cookie")

    made, cleared = issued_with(cookie_secure=True)
    assert [h.split("=", 1)[0] for h in made] == [
        "__Host-st_session",
        "__Host-st_device",
        "__Host-st_pending",
    ]
    for header in made + cleared:
        assert header.startswith("__Host-")
        # The prefix's own conditions, or the browser drops the cookie.
        assert "Secure" in header and "Path=/" in header and "Domain" not in header
    # A clear is an overwrite, which a browser refuses for __Host- unless Secure.
    assert [h.split("=", 1)[0] for h in cleared] == ["__Host-st_session", "__Host-st_pending"]

    made, cleared = issued_with(cookie_secure=False)
    assert [h.split("=", 1)[0] for h in made] == ["st_session", "st_device", "st_pending"]
    assert [h.split("=", 1)[0] for h in cleared] == ["st_session", "st_pending"]


def test_a_session_planted_under_the_bare_name_is_not_honoured(client):
    """The server's half of #209: with `Secure` on it reads only the prefixed
    name, so a cookie some other port set as plain `st_session` is ignored."""
    from app.auth import cookies
    from tests.conftest import _setup_owner

    _setup_owner(client)
    value = client.cookies.get(cookies.session_name())
    assert cookies.session_name() == "__Host-st_session"
    assert value and client.get("/api/me").status_code == 200

    client.cookies.clear()
    client.cookies.set("st_session", value, domain="testserver.local")
    assert client.get("/api/me").status_code == 401


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
