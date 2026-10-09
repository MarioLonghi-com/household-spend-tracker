"""The application.

Seven things happen here that are not routing: the security headers, the
`Host` check, the request-body ceiling, the setup gate, the cross-origin check,
the SPA fallback's containment guard, and the housekeeping sweep that deletes
what has expired.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import OperationalError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

import app.audit.guard  # noqa: F401  -- installs the bulk-statement guard
import app.audit.hook  # noqa: F401  -- installs the audit flush hook
from statements import UnreadableStatement

#: Re-exported so `from app.main import __version__` keeps working; the
#: definition is in `app/__init__.py`, where a router can read it without
#: importing this module and creating a cycle.
from . import __version__, build, logging_setup, permissions, schema_check
from .api import dbview, deps, discovery
from .api.routers import (
    account_resets,
    admin,
    agent,
    agent_links,
    agent_matching,
    agent_review,
    auth,
    categories,
    households,
    identifiers,
    imports,
    invites,
    one_time_import,
    profile,
    receipts,
    reporting,
    setup,
    stats,
    transactions,
    transfers,
    translations,
    updates,
)
from .auth import cookies, housekeeping, keycheck
from .auth import setup as setup_service
from .body_limit import BodyLimit
from .config import SECRET_KEY_ENV, settings
from .db import SessionLocal, session_scope
from .db import engine as db_engine
from .errors import DomainError, TooManyAttempts
from .hosts import AllowedHosts, request_host
from .services import agent_keys as agent_key_service
from .services import agent_requests, backup_bundle

log = logging.getLogger("spendtracker")

#: Reachable before there is anyone to sign in as.
PUBLIC_PREFIXES = ("/api/health", "/api/setup", "/api/session", "/api/invite", "/api/reset/")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Where every router is mounted. Named once so the mount and anything that
#: has to reconstruct a full path cannot drift apart.
API_PREFIX = "/api"

#: How often expired auth rows are swept. Six hours, on a thirty-day retention
#: window and a fourteen-day idle window -- the sweep only has to be frequent
#: relative to what it is enforcing, and a deployment that restarts weekly
#: should not be the only thing that ever runs it.
HOUSEKEEPING_INTERVAL_SECONDS = 6 * 60 * 60


async def _housekeeping_loop() -> None:
    """Sweep at startup and then on a timer, for the life of the process.

    On a threadpool worker: `sweep` is ordinary blocking SQLAlchemy, and running
    it on the event loop would stall every request for the length of a delete.

    It is wrapped because a failed sweep must not take the instance with it.
    Nothing a user does depends on this having run, and an instance that refuses
    to serve a ledger because it could not delete a fortnight-old session row
    has its priorities backwards.
    """
    while True:
        try:
            removed = await asyncio.to_thread(housekeeping.sweep, db_engine)
            if any(removed.values()):
                log.info(
                    "housekeeping: %s",
                    ", ".join(f"{table} {count}" for table, count in removed.items() if count),
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive; a sweep is never load-bearing
            log.exception("housekeeping sweep failed; will try again later")
        await asyncio.sleep(HOUSEKEEPING_INTERVAL_SECONDS)


def announce_ledger() -> None:
    """Say which ledger this is, and whether its key still opens it.

    Which ledger: a fresh clone, a rebuilt image or a new directory all open
    whatever ledger the data directory already holds -- by design, and
    `deploy/TROUBLESHOOTING.md` says why -- and the first anyone heard of it
    was the old households on the screen. One line naming the file and what is
    in it makes that the first thing in the log instead.

    Whether its key opens it: `app/config.py` creates `secret.key` when there
    is none, so a ledger whose key went missing boots beside a brand-new one,
    every row readable and every authenticator refused, and nothing said so
    until somebody tried to sign in. Such a ledger runs in **recovery mode**
    (#287), and this line is where the operator hears it: computed here as on
    every sign-in, never stored, and over when the original key is back.

    It names the key where it came from (`Settings.secret_key_source`). An
    instance keyed by `SPENDTRACKER_SECRET_KEY` never reads `secret.key`, so
    a line blaming the file sent the operator to copy the right key into a
    file the app ignores, and recovery mode carried on.
    """
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        return
    path = Path(url.split("///", 1)[-1]).resolve()
    rows = backup_bundle.counts(path)
    log.info(
        "ledger %s: %d household(s), %d member(s)",
        path,
        rows.get("households", 0),
        rows.get("users", 0),
    )
    keyed = keycheck.check(path, settings.secret_key)
    if keyed.refused:
        if settings.secret_key_from_env:
            back = f"setting {SECRET_KEY_ENV} back to the original key"
            cause = (
                f"{SECRET_KEY_ENV} is set, so secret.key in {settings.data_dir} is not "
                "read: the variable is what has to hold the original key"
            )
        else:
            back = "putting the original key back"
            cause = (
                "A restore without its own key, or a key recreated because the old "
                "one was missing, does this"
            )
        log.warning(
            "recovery mode: %s does not open %d of the %d authenticator(s) in this "
            "ledger, so %s will be asked for a recovery code at sign-in and then to "
            "set up a new authenticator. The old sealed secrets are kept: %s ends "
            "recovery mode for everybody who has not re-enrolled. Find it before "
            "anybody signs in if you can: the recovery code also signs that member "
            "out everywhere and revokes their trusted browsers and agent keys, and "
            "the key coming back does not restore them. %s; see "
            "deploy/TROUBLESHOOTING.md.",
            settings.secret_key_source,
            len(keyed.refused),
            keyed.enrolled,
            "those members" if len(keyed.refused) > 1 else "that member",
            back,
            cause,
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Schema changes are Alembic's job, not the app's.

    The previous build called ``create_all`` here, which creates what is missing
    and alters nothing that exists -- so a new column never reached a database
    that already had real data in it.
    """
    permissions.private_dir(settings.data_dir)

    # First, so that everything below this line reaches the file. Until this
    # existed the app configured no handler at all: every `log.warning` here
    # went wherever uvicorn's own handlers pointed, which on a self-hosted
    # instance is a terminal that has been closed by the time anyone asks what
    # happened. `app/logging_setup.py` has the rest of the reasoning.
    style = logging_setup.configure()
    log.info("logging to %s at style %s", logging_setup.log_file(), style.key)

    # The umask makes everything created from now on private; it cannot reach
    # back. Files an older version wrote keep `-rw-r--r--` until somebody
    # changes them, and that somebody has to be told. Warned, not chmodded:
    # quietly changing the modes of files on someone's disk at boot is not
    # this app's decision to make.
    exposed = permissions.not_private(settings.data_dir)
    if exposed:
        log.warning(
            "%d path(s) in the data directory can be read by other users on this "
            "machine -- the ledger, its key and the logs belong to this account "
            "alone:\n%s\nFix with:  chmod -R go-rwx %s",
            len(exposed),
            "\n".join(f"    {mode:04o}  {path}" for path, mode in exposed),
            settings.data_dir,
        )

    # Said once, loudly, at the only moment somebody is reading the log. The
    # flag is off because a human typed it, but the instance that gets left
    # running for a fortnight is the one nobody remembers typing it on.
    if not settings.cookie_secure:
        log.warning(
            "SPENDTRACKER_COOKIE_SECURE is off: the session and device cookies "
            "are being sent without `Secure`, so they will travel in clear over "
            "plain HTTP. This is for a trusted local network only -- never for a "
            "deployment reachable from anywhere else."
        )

    # Before anything touches a table. A schema that is behind, ahead, or was
    # never migrated used to start happily and fail at whatever request first
    # reached the missing piece -- a 500 on sign-in is a bad way to learn you
    # forgot `make migrate`.
    revision = schema_check.verify(db_engine)
    log.info("schema at %s", revision)
    announce_ledger()
    # Fixed here, at boot: the checkout can move under a running server, and
    # what matters is the code it imported, not what is on disk by now.
    running = build.current()
    log.info(
        "code at %s%s (%s, from %s)",
        build.short(running) or "an unknown commit",
        ", with uncommitted changes" if running.dirty else "",
        running.branch or "no branch",
        running.source,
    )

    with session_scope() as session:
        if not setup_service.is_configured(session):
            token = setup_service.rotate_setup_token()
            # To the console as well as the 0600 file, on purpose: in a container
            # `docker compose logs` is the only place the person can read it.
            # Which makes the console log a credential until setup is done --
            # anything that ships or keeps container logs (a collector, a
            # persistent log driver) holds a token that claims the instance for
            # whoever reads it first. Bounded: it is rotated on every boot and
            # gone once the first owner exists (#211).
            log.warning(
                "This instance has no accounts yet. Finish setup at /setup with this "
                "one-time token (also written to %s):\n\n    %s\n",
                setup_service.setup_token_path(),
                token,
            )

    sweeper = asyncio.create_task(_housekeeping_loop())
    try:
        yield
    finally:
        # Cancelled and awaited, not abandoned. A task still holding a session
        # when the loop closes logs "Task was destroyed but it is pending" and,
        # on SQLite, can leave the sweep's write transaction open behind it.
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper


# The schema and the two doc viewers are off unless this instance is explicitly
# in development. They leak no data, but they hand an unauthenticated visitor
# the complete route and schema inventory, and `/redoc` was mounted by default
# although it was never asked for. That should be a decision, not a default.
_DEV = settings.environment == "development"

app = FastAPI(
    title="Spend Tracker",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs" if _DEV else None,
    redoc_url="/api/redoc" if _DEV else None,
    openapi_url="/api/openapi.json" if _DEV else None,
)


def _carries_a_key(request: Request) -> bool:
    """Whether this request presents an agent key in the header.

    The prefix is checked whole, through `deps.BEARER`, so the middleware and
    the dependency cannot drift apart about what "carries a key" means -- and
    so that a request waved past this check is one `current_agent` will
    actually demand a valid key for.
    """
    return (request.headers.get("authorization") or "").startswith(deps.BEARER)


@app.middleware("http")
async def refuse_cross_origin_writes(request: Request, call_next):
    """CSRF, without a token dance.

    The client is our own app on the same origin, so a state-changing request
    must either say it came from here or say it is same-site. Requiring one of
    the two to be present and correct is stricter than checking Origin alone,
    which has to decide what to do when the header is absent.
    """
    if request.method not in SAFE_METHODS:
        # A bearer token is not an ambient credential. CSRF is the attack where
        # a browser attaches a cookie the user did not intend to send; a header
        # a program had to set deliberately cannot be attached by a cross-site
        # form, image or fetch. So a request authenticated *only* by a key is
        # out of scope for this check.
        #
        # "Only" is the load-bearing word. If a session cookie is also present
        # this is a browser, and a browser is exactly what the check is for --
        # so the cookie wins and the origin check runs. Otherwise a page on
        # another origin could add a constant Authorization header it does not
        # know the value of, ride the victim's cookie, and be waved through.
        if _carries_a_key(request) and not deps.carries_session_cookie(request):
            return await call_next(request)

        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site")
        # Hosts, not whole URLs. Behind a proxy that terminates TLS the app
        # sees http:// while the browser sends https://, and comparing the
        # scheme would refuse every write the real client makes -- whose
        # obvious field fix is to delete the check altogether.
        same_origin = origin is not None and urlsplit(origin).netloc == request.url.netloc
        declared_same_site = origin is None and site in {"same-origin", "none"}
        if not (same_origin or declared_same_site):
            return JSONResponse(
                status_code=403, content={"detail": "that request did not come from this app"}
            )
    return await call_next(request)


def _key_behind(request: Request) -> str | None:
    """The id of the key a refused request presented, if it presented a real one.

    On its own short-lived session: the request's own session is long gone by
    the time a refusal has been turned into a response, and this must not
    resurrect it.
    """
    presented = deps._presented(request)
    if presented is None:
        return None
    with SessionLocal() as own:
        key = agent_key_service.lookup(own, presented)
        return key.id if key else None


@app.middleware("http")
async def record_agent_requests(request: Request, call_next):
    """One line per request a key made, written after the fact.

    After `call_next`, because `status` and `rows` are only knowable once the
    handler has run -- and because `request.scope["route"]` is not populated
    until routing has happened, which is where the route TEMPLATE comes from.
    The template is the point: the real path carries ids and this table is
    swept on a window precisely so it does not accumulate them.

    Registered *after* `refuse_cross_origin_writes` and therefore running
    outside it, so a request the CSRF check refuses is still recorded. A key
    that is being used wrongly is exactly the one worth having a line for.

    The write is on its own session, like `ratelimit.record`: a request that
    raised has had its session rolled back, and a limit you can escape by
    failing is not a limit.
    """
    response = await call_next(request)

    key_id = getattr(request.state, "agent_key_id", None)
    if key_id is None:
        # Either not an agent request at all, or one that never reached the
        # dependency that sets this -- a CSRF refusal returns before the
        # handler, so nothing downstream ever identified the key.
        #
        # That second case is the one worth a line, and this used to drop it:
        # the middleware sits outside the CSRF check, as the docstring says,
        # but recording was gated on state only `current_agent` assigns. A key
        # being driven from a browser context it has no business being in is
        # the most interesting thing this table could report.
        #
        # So resolve it here, and only here: the lookup costs one indexed read
        # on the token hash, and only for a request that carried a token and
        # never reached the handler. A token that resolves to nothing still
        # writes nothing -- there is genuinely no key to attribute it to.
        key_id = await asyncio.to_thread(_key_behind, request)
        if key_id is None:
            return response

    route = request.scope.get("route")
    template = getattr(route, "path", None) or request.url.path
    # A router carries the paths it was WRITTEN with, not the ones it is
    # mounted at: `include_router(..., prefix="/api")` is not reflected here,
    # so the template alone reads `/agent/v1/manifest` -- a path nobody can
    # call. Every route a key can reach is under this prefix, so prepending it
    # makes the logged route the URL that was actually requested.
    if template.startswith("/") and not template.startswith(API_PREFIX):
        template = API_PREFIX + template

    # As a BACKGROUND TASK, not inline, and this is not a performance choice.
    #
    # `call_next` returns a streaming response as soon as the response STARTS.
    # The downstream task is still running at that point and the dependency
    # exit stack has not unwound -- so `get_session` has not committed, and on
    # SQLite the request still holds its write lock. Writing the line here on
    # its own connection therefore blocks against the very request it is
    # describing, waits out `busy_timeout`, and fails with "database is
    # locked".
    #
    # It never showed while agents could only read: a GET takes no write lock.
    # The first agent WRITE found it immediately.
    #
    # A background task runs after the response is finished, by which point the
    # exit stack has unwound and the lock is gone. It also cannot fail the
    # request it is recording, which is the right relationship between a log
    # and the thing it logs.
    response.background = BackgroundTask(
        _record_agent_request,
        key_id=key_id,
        method=request.method,
        route=template,
        status=response.status_code,
        rows=getattr(request.state, "agent_rows", None),
        batch_id=getattr(request.state, "agent_batch_id", None),
    )
    return response


def _record_agent_request(**line) -> None:
    """Write one request line, and never let it break the response.

    A log that can fail the request it describes is worse than no log: this
    runs after the response has been sent, so raising here would only produce
    an unexplained traceback in the server output and could not tell the
    caller anything useful anyway.
    """
    try:
        agent_requests.record(db_engine, agent_key_id=line.pop("key_id"), **line)
    except Exception:  # noqa: BLE001 - see the docstring
        log.exception("could not record an agent request")


#: Set once the instance answers that it is configured, and never unset: a
#: configured instance does not become a fresh one while it is running.
_configured = False


def _is_configured() -> bool:
    with session_scope() as session:
        return setup_service.is_configured(session)


@app.middleware("http")
async def gate_until_configured(request: Request, call_next):
    """A fresh instance does one thing: let itself be set up.

    Everything else answers 503 -- there is no API to probe and no ledger to
    read, because there is nobody to own one yet.
    """
    global _configured
    path = request.url.path
    if not _configured and path.startswith("/api") and not path.startswith(PUBLIC_PREFIXES):
        # Asked once per process after setup, not once per request (#240):
        # the answer only ever goes from no to yes. And asked on a worker
        # thread, since a pool checkout and a SELECT on the loop make every
        # request wait out a slow disk or a WAL checkpoint.
        _configured = await run_in_threadpool(_is_configured)
        if not _configured:
            return JSONResponse(
                status_code=503,
                content={"detail": "this instance has not been set up yet; open /setup"},
            )
    return await call_next(request)


# Pure ASGI, both of them, and registered here -- after the three above and
# before `add_security_headers` -- so each runs outside everything that reads a
# body or touches the database, and inside the headers, so a 400 or a 413 from
# either carries the same security headers as any other refusal. Starlette runs
# the most recently registered first; the order on the way in is therefore
# headers, host, body limit, setup gate, agent log, CSRF.
#
# The host check is outside the body limit so a request naming a host nobody
# listed is refused before its length is even looked at. The body limit is
# outside the setup gate because the gate opens a database session, and a
# stranger's 200 MB should not buy one.
app.add_middleware(BodyLimit)
app.add_middleware(AllowedHosts, allowed_hosts=settings.allowed_hosts)


#: Sent on every response. None of these is a fix for a bug that exists today --
#: the client has no ``dangerouslySetInnerHTML``, no ``innerHTML`` and no
#: ``eval``, and React escapes what it renders. They are the layer that is
#: already in place when somebody writes the first one.
#:
#: ``frame-ancestors 'none'`` is the one that is not theoretical. This app has
#: one-click irreversible actions -- undo a batch, delete a transaction, revoke
#: every device -- and clickjacking is the attack on those that needs no script
#: bug at all.
BASE_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # An invitation link carries its token in the path, so no referrer may
    # leave this origin carrying it.
    "Referrer-Policy": "no-referrer",
    # Passkeys (#47 §1.4): `self` is already the default for both, written
    # out so the policy says in one place what this origin may use.
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), "
        "publickey-credentials-get=(self), publickey-credentials-create=(self)"
    ),
}

#: `/snap` is the one document allowed to ask the browser where it is, and only
#: because somebody pressed a button on that page saying so.
#:
#: This reverses a line in the receipt-storage design notes (§11.7,
#: "geolocation stays off, and that is a real trade"). The trade was
#: reconsidered on 2026-09-21, when the location toggle was asked for and built.
#: The reasoning that changed: a photograph's own EXIF fix is already read and
#: stored, so the route from a receipt to a coordinate was never actually
#: closed -- what `geolocation=()` closed was the *honest* one, where the page
#: says what it is about to record and the browser asks first. A toggle that
#: cannot work is worse than no toggle, because it reads as a refusal the user
#: caused.
#:
#: It is `self`, not `*`: nothing embedded inherits it, and `/snap` embeds
#: nothing. Every other path on this origin -- the whole SPA -- still sends
#: `geolocation=()`, so the permission cannot be reached from any page but the
#: one carrying the toggle.
SNAP_SECURITY_HEADERS = {
    **BASE_SECURITY_HEADERS,
    # `/snap` signs nobody in, so it is the one page that may not use a passkey.
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(self), payment=(), "
        "publickey-credentials-get=(), publickey-credentials-create=()"
    ),
}

#: Everything is same-origin and self-hosted: the SPA is built into
#: ``app/static/dist`` and served from here, the API is ``/api`` on this origin,
#: and nothing loads a font, a script or an image from anywhere else. So the
#: policy can be as tight as it reads.
#:
#: ``img-src`` carries ``blob:`` for one reason: the receipt screens show a
#: local ``URL.createObjectURL`` preview while the server is encoding, which
#: takes 0.6 to 5 seconds on a phone. Without it the frame is a broken image
#: and the only clue is a console violation, which on a phone at a till nobody
#: is reading. It permits the page to display bytes it already holds, under
#: URLs it minted for itself; a ``blob:`` URL cannot be pointed at another
#: origin and cannot outlive the document that created it.
#:
#: No ``data:`` (#94). Nothing uses one -- the QR code is SVG, and the client
#: imports no images -- and a ``data:`` image is the one kind of image source
#: whose bytes are whatever the markup that names it says.
#:
#: ``style-src`` is the one concession. ``client/src/lib/theme.ts`` paints a
#: household's palette by setting a ``<style>`` element's ``textContent``, which
#: is an inline stylesheet however it got there. A nonce would mean the server
#: rendering the shell, which it does not -- ``index.html`` is a static build
#: artefact. The values interpolated into it are validated as hex by
#: ``theming._valid_hex`` before they are ever stored (`services/households.py`),
#: so the injection this would otherwise allow has no way in.
CONTENT_SECURITY_POLICY = "; ".join(
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
)

#: Paths whose HTML this app did not write. Datasette renders its own pages with
#: inline scripts, and the dev-only Swagger and ReDoc viewers load from a CDN --
#: so the app's policy would break all three rather than protect them. They keep
#: the other headers, and `/db` keeps its own guard: owner-only, and a redacted
#: snapshot rather than the ledger.
#: `/db/` with the slash, plus `/db` exactly. A bare `startswith("/db")` would
#: also exempt a client-side route called `/dbsomething` -- there is none today,
#: and a security header that switches itself off on a path nobody chose is not
#: a thing to leave lying around.
_NOT_OUR_HTML = (f"{dbview.MOUNT}/", "/api/docs", "/api/redoc", "/api/openapi.json")


def _is_ours(path: str) -> bool:
    return path != dbview.MOUNT and not path.startswith(_NOT_OUR_HTML)


#: The capture page and the two files it loads. Listed exactly rather than
#: matched as a prefix: a prefix would hand the geolocation permission to any
#: future `/snapshot` too, and that is precisely the sort of path somebody adds
#: without re-reading this file.
_SNAP_PATHS = ("/snap", "/snap/snap.js", "/snap/manifest.json")


def _is_snap(path: str) -> bool:
    return path in _SNAP_PATHS


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Outermost, so a refusal from any middleware below carries them too.

    Registered last on purpose: Starlette runs the most recently added first, so
    this wraps the host check, the body limit, the CSRF check and the setup gate
    rather than sitting inside them. Anything added after this line would run
    outside it, and its refusals would go out bare.
    """
    response = await call_next(request)
    headers = SNAP_SECURITY_HEADERS if _is_snap(request.url.path) else BASE_SECURITY_HEADERS
    for name, value in headers.items():
        response.headers.setdefault(name, value)

    if _is_ours(request.url.path):
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)

    # The ledger, `/api/me` and the user list are JSON a shared browser would
    # otherwise keep in its disk cache after sign-out -- `client.clear()` only
    # empties the in-memory copy (#194). A route that chose its own caching
    # keeps it: receipt bytes are content-addressed and cached for a year on
    # purpose.
    if request.url.path.startswith(f"{API_PREFIX}/"):
        response.headers.setdefault("Cache-Control", "no-store")

    # Only in production, and only on an instance that is actually HTTPS. The
    # deployment is HTTPS over the tailnet and the cookies are already `Secure`,
    # so this costs nothing there -- but pinning a developer's browser to HTTPS
    # on a plain-HTTP localhost is a self-inflicted afternoon.
    #
    # `cookie_secure` is the second half of that test, and it is not cosmetic.
    # An instance that has turned it off is by definition serving plain HTTP at
    # a LAN address; sending HSTS from there pins every browser that loads it to
    # HTTPS **for that host and port, for a year**, and the app becomes
    # unreachable at the address it just told them to use. The user-facing cure
    # is a trip into chrome://net-internals, per device.
    #
    # Nor over plain HTTP to loopback (#196): the container on somebody's own
    # computer is production, at `http://localhost:8848`. A browser ignores
    # HSTS that arrives over HTTP anyway, so this only stops sending noise.
    # The host is the one the cookie names are chosen by; this middleware runs
    # outside the host check, but a request the check refuses gets no cookie.
    plain_loopback = request.url.scheme == "http" and cookies.is_loopback(
        request_host(request)
    )
    if not _DEV and settings.cookie_secure and not plain_loopback:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def _speaks_to_agent(request: Request) -> bool:
    """Whether this answer goes to a program holding an agent key.

    The codes exist for the client's translations. An agent reads ``detail``,
    and the localisation work promised it byte-identical answers (#48), so it
    gets none of them yet. Offering them is this one condition plus the
    descriptor and `/llms.txt` saying what they are.
    """
    return request.url.path.startswith(f"{API_PREFIX}/agent/") or _carries_a_key(request)


@app.exception_handler(DomainError)
def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    """Domain rules answer with their own status code and their own words.

    A raise that carries a ``code`` also sends it, with its ``params`` nested
    beside ``detail`` so they can never collide with ``fields`` (#65). Not to
    an agent: its answers stay byte-identical until the codes are documented
    for agents in `/llms.txt` -- see `_speaks_to_agent`.
    """
    headers = dict(getattr(exc, "headers", {}) or {})
    if isinstance(exc, TooManyAttempts):
        headers["Retry-After"] = str(exc.retry_after)
    content = {"detail": str(exc), **(getattr(exc, "fields", None) or {})}
    if not _speaks_to_agent(request):
        content.update(exc.wire())
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)


@app.exception_handler(RequestValidationError)
def handle_schema_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's own 422, less the ``input`` of each error.

    The default handler returns each failing value back to the sender, and a
    body can carry a secret -- a YNAB token, a password -- that must not be
    echoed even to the person who sent it (#224). Where it failed and why is
    all a client needs; it already has what it sent.
    """
    errors = [
        {key: value for key, value in error.items() if key != "input"}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.exception_handler(OperationalError)
def handle_operational_error(request: Request, exc: OperationalError) -> JSONResponse:
    """A write that lost the race for SQLite's one write lock is a 409 (#240).

    Under WAL a late writer is refused with "database is locked" once
    `busy_timeout` runs out -- two people changing the ledger at the same
    moment, not a fault in the server -- and it used to surface as a 500.
    Anything else the database says is still a 500.
    """
    if "database is locked" in str(exc.orig) or "database is busy" in str(exc.orig):
        return JSONResponse(
            status_code=409,
            content={"detail": LEDGER_BUSY},
        )
    raise exc


#: What a request refused for a busy database is told.
LEDGER_BUSY = "the ledger is busy with another change at this moment; try again"


@app.exception_handler(UnreadableStatement)
def handle_unreadable_statement(request: Request, exc: UnreadableStatement) -> JSONResponse:
    """The `statements` library refused a file, and said why in a sentence.

    It is a separate library with no idea what HTTP is, so the status code is
    chosen here: 422, the same answer the domain gives for anything else the
    user uploaded that cannot be used.
    """
    return JSONResponse(status_code=422, content={"detail": str(exc)})


#: `no-store`, not `public, max-age=3600`. Both documents are rendered from the
#: `Host` header, and a shared cache holding one that a rebinding page asked for
#: would go on serving the attacker's `base_url` to everybody after it. The page
#: is a few kilobytes built from constants; caching it saves nothing worth that.
_DISCOVERY_CACHE = "no-store"

#: The one page served to a caller with no credential at all. See
#: `app/api/discovery.py` for why this is not the same decision as serving the
#: OpenAPI schema anonymously.
_LLMS_HEADERS = {
    # It is prose, and a model or a person may read it in a browser. Plain text
    # rather than `text/markdown` because every client renders text and the
    # sniffing rules for markdown are not worth the argument.
    "Content-Type": "text/plain; charset=utf-8",
    "Cache-Control": _DISCOVERY_CACHE,
}


@app.get("/llms.txt", include_in_schema=False)
@app.get("/.well-known/llms.txt", include_in_schema=False)
def llms_txt(request: Request) -> Response:
    """How to use this app, for whoever arrived without a key.

    Anonymous on purpose, and the only anonymous thing here that is not a sign-in
    flow. An agent handed a bare URL could otherwise learn nothing at all -- not
    the base path, not that a key is what it needs, and not how a person would
    give it one.
    """
    origin = deps.public_origin(request)
    return Response(content=discovery.document(base_url=origin), headers=_LLMS_HEADERS)


@app.get(discovery.WELL_KNOWN, include_in_schema=False)
def agent_descriptor(request: Request) -> JSONResponse:
    """`/llms.txt`, for a caller that went looking in the conventional place.

    Anonymous for the same reason and on the same terms as `llms_txt`: it is
    rendered from the same constants, it describes the software rather than
    this deployment, and it names no household, no account, no id and no route
    inventory. Issue #39 -- the page existed and was very good, and neither
    test agent found it.
    """
    origin = deps.public_origin(request)
    return JSONResponse(
        content=discovery.descriptor(base_url=origin),
        headers={"Cache-Control": _DISCOVERY_CACHE},
    )


@app.get("/api/health", tags=["meta"])
def health() -> dict:
    with session_scope() as session:
        configured = setup_service.is_configured(session)
    # The short SHA and nothing else about the build: this answers without a
    # session, and the branch name and the dirty flag are the owner's business.
    return {
        "status": "ok",
        "version": __version__,
        "commit": build.short(build.current()),
        "setup_required": not configured,
    }


for router in (
    agent.router,
    agent_matching.router,
    agent_links.router,
    agent_review.router,
    setup.router,
    auth.router,
    households.router,
    transactions.router,
    imports.router,
    identifiers.router,
    admin.router,
    updates.router,
    invites.router,
    account_resets.router,
    profile.router,
    categories.router,
    receipts.router,
    reporting.router,
    stats.router,
    transfers.router,
    translations.router,
    one_time_import.router,
):
    app.include_router(router, prefix=API_PREFIX)


@app.get("/api/openapi.json", include_in_schema=False)
def schema_for_key_holders(agent: deps.CurrentAgent) -> JSONResponse:
    """The full schema, to anybody holding a valid key.

    A *route* rather than flipping `openapi_url`, deliberately. Flipping it
    would serve the complete route inventory to anonymous visitors, which is
    the decision `_DEV` exists to make and the one Review Round 2 cared about.
    This keeps the anonymous door shut and opens it only to a credential.

    In development `_DEV` already mounts FastAPI's own `/api/openapi.json`, and
    a route registered here would be shadowed by it -- which is correct: a
    developer should not need a key to read their own schema.
    """
    return JSONResponse(app.openapi())


#: The mobile capture page. Two static files and a manifest, outside the SPA --
#: see the comment at the top of `app/static/snap/index.html` for why it is not
#: a screen in the app.
_SNAP = Path(__file__).parent / "static" / "snap"


@app.get("/snap", include_in_schema=False)
def snap(request: Request):
    """The page, behind the session -- but redirecting rather than refusing.

    `current_user` would answer 401 with a JSON body, which on a phone is a
    blank screen. And a redirect to the register would be a lost receipt: the
    person is standing at a till holding a piece of paper, so they come back
    *here* once they are signed in.
    """
    from .auth import sessions as session_service

    # Its own short session rather than the request dependency: this route
    # reads one row and `current_user` would also touch `last_seen_at`, which
    # is a write on a page load that changes nothing.
    with session_scope() as db_session:
        row = session_service.lookup(
            db_session, cookies.session_value(request)
        )
    if row is None:
        return RedirectResponse("/?next=%2Fsnap", status_code=303)
    return FileResponse(_SNAP / "index.html")


@app.get("/snap/snap.js", include_in_schema=False)
def snap_script() -> FileResponse:
    """Served as its own file, and it has to be.

    The CSP is `script-src 'self'` with no 'unsafe-inline', so the obvious
    shape for something this small -- one file with a `<script>` block --
    renders blank with nothing but a console violation. There is a test for
    that, because the failure is invisible and the fix looks like a regression
    in tidiness.

    No session check: it is code, it carries no data, and the manifest beside
    it has to be reachable for the page to install to a home screen.
    """
    return FileResponse(_SNAP / "snap.js", media_type="text/javascript")


@app.get("/snap/manifest.json", include_in_schema=False)
def snap_manifest() -> FileResponse:
    return FileResponse(_SNAP / "manifest.json", media_type="application/manifest+json")


# Mounted before the SPA fallback, which otherwise swallows every unknown path.
if dbview.enabled():

    @app.get(dbview.MOUNT, include_in_schema=False)
    def _db_root() -> RedirectResponse:
        """A Mount only matches paths *under* it.

        Without this the bare `/db` falls through to the SPA catch-all below --
        which matches everything and would answer 200 with the app shell, so an
        unauthenticated visitor would get a page rather than the refusal the
        mount gives them one slash later.
        """
        return RedirectResponse(f"{dbview.MOUNT}/", status_code=307)

    app.mount(dbview.MOUNT, dbview.GuardedDatasette())


#: What an unmatched path under `/api/` says. It names the agent surface and
#: the discovery document rather than only refusing, for the same reason
#: `_NO_KEY_HEADERS` carries a `WWW-Authenticate` pointing at /llms.txt: a
#: request that arrives wrong should teach rather than only fail.
_NO_SUCH_ENDPOINT = (
    f"no such endpoint. The agent API is at /api/agent/v{agent.API_VERSION}; "
    "see /llms.txt for how to use it."
)

_static = Path(__file__).parent / "static" / "dist"
if _static.exists():  # pragma: no cover - only present once the client is built
    app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")
    _static_root = _static.resolve()

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_static / "index.html")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        """Client-side routing: unknown paths fall through to the app shell.

        The containment check is not optional. ASGI hands the raw path through
        without normalising ``..``, so without it this route will serve any file
        the process can open -- which in the previous build meant the database.

        `/api/` is excluded, and that exclusion is the whole of issue #36. For a
        browser, answering an unknown path with the shell is correct routing.
        For a program probing the surface it is the worst available answer:
        every guess "succeeds" with 200 text/html, so no status code carries
        information and a real endpoint is distinguishable from an invented one
        only by sniffing Content-Type on every call. Both test agents on the
        2026-09-22 import run hit this independently.

        The 405 a mistyped method would otherwise earn is not lost here,
        because this route has always swallowed it: it matches every GET, so a
        GET on a POST-only route reached the shell rather than the router's
        partial match. Under `/api/` that now surfaces as a 404 with a sentence
        instead of a 200 with a web page, which is strictly more information
        than before.
        """
        if path == "api" or path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": _NO_SUCH_ENDPOINT})
        candidate = (_static / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(_static_root):
            return FileResponse(candidate)
        return FileResponse(_static / "index.html")

else:
    # Without this the process that knows the answer says nothing. The API is
    # genuinely fine -- /api/health returns ok and every route responds -- and
    # only `/` 404s, so "why is my app 404ing" turns into a search instead of a
    # read of the first screen of output. Reported from a fresh clone, issue #1.
    log.warning(
        "app/static/dist is missing, so the SPA is not being served and / will 404. "
        "Run `make client` to build it. The API itself is unaffected."
    )
