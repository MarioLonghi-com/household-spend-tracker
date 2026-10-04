"""Runtime configuration.

Env-driven, read once into a module-level singleton. The one secret the server
holds is generated rather than demanded: a key nobody had to invent beats a key
someone pasted out of an example. The cost is that ``secret.key`` must be backed
up alongside the database — lose it and every authenticator secret is sealed
with a key nobody has, so every member has to re-enrol. Sessions, trusted
devices, passwords and recovery codes are not encrypted with it, so losing it
signs nobody out by itself -- but each member re-enrols through a recovery
code, which revokes their sessions, trusted browsers and agent keys.
``app/auth/crypto.py``, SECURITY.md and the README say the same.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from .permissions import private_dir, tighten_umask


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


#: The variable that supplies the key instead of `secret.key`. When it is set
#: the file is never read, whatever it holds.
SECRET_KEY_ENV = "SPENDTRACKER_SECRET_KEY"


def _key_from_env() -> str:
    return (os.environ.get(SECRET_KEY_ENV) or "").strip()


def _load_or_create_secret_key(data_dir: Path) -> str:
    """A 32-byte urlsafe key, created on first boot with 0600 and reused after."""
    from_env = _key_from_env()
    if from_env:
        return from_env

    private_dir(data_dir)
    path = data_dir / "secret.key"
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing

    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    # Write with the mode set from the start; a chmod after the fact leaves a
    # window where the key is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(key)
    return key


#: Where a deployment's ledger and key live when nothing says otherwise.
#:
#: `$XDG_DATA_HOME/spend-tracker`, or `~/.local/share/spend-tracker`. The same
#: on macOS as on Linux on purpose: this is a server, it is configured by
#: environment variable, and one predictable path is worth more here than
#: following a desktop convention nobody is going to read a plist from.
def _user_data_dir() -> Path:
    base = (os.environ.get("XDG_DATA_HOME") or "").strip()
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "spend-tracker"


def resolve_data_dir() -> Path:
    """The directory holding `spendtracker.sqlite3` and `secret.key`.

    Three answers, in order:

    1. **`SPENDTRACKER_DATA_DIR`**, if set. A container sets it, and so does
       every test.
    2. **`./data`, if it already exists.** Every install made before this
       function did, which is all of them, and none of them should move
       because a release changed a default.
    3. **`~/.local/share/spend-tracker`**, for a fresh clone.

    Why the default moved out of the checkout at all:

    > `git clean -xdf` deletes the ledger and the encryption key.

    `-x` means "including ignored files", and `data/` is ignored. That command
    is the standard reflex for "my build is in a weird state, start clean", and
    in the old layout it took `spendtracker.sqlite3`, its WAL, and
    `secret.key` with it. The key is the worse half: without it every TOTP
    secret is undecryptable and every member has to re-enrol (recovery codes
    are not encrypted with it and still work). No warning fired, and nothing
    in the repository said so.

    Keeping the data outside the working tree is the structural fix. Saying it
    in a README would have been the documented one.
    """
    named = (os.environ.get("SPENDTRACKER_DATA_DIR") or "").strip()
    if named:
        return Path(named).resolve()

    here = Path("./data")
    if here.exists():
        return here.resolve()

    return _user_data_dir().resolve()


#: Written in place of a name in `SPENDTRACKER_ALLOWED_HOSTS` to mean "any
#: address typed as an IP literal": `192.168.1.50`, `100.101.102.103`, `[::1]`.
IP_LITERALS = "ip"

#: Who may be named in the `Host` header when `SPENDTRACKER_ALLOWED_HOSTS` is
#: not set. Every way this app is reached today, and nothing else:
#:
#: - `localhost`, `127.0.0.1` and `[::1]` -- `make dev`, `make serve`, the
#:   container's healthcheck, and Playwright.
#: - **any IP literal** -- `make lan`, which binds `0.0.0.0` and is reached at
#:   whatever address DHCP handed out this week, a tailnet `100.x` address, and
#:   a container published on the host's address. An IP literal is not a DNS
#:   rebinding vector: rebinding needs a *name* the attacker controls, and a
#:   browser that typed an address sends that address.
#: - `*.ts.net` -- `tailscale serve`, the deployment this is designed for.
#: - `testserver` -- Starlette's test client, which is what the suite sends.
#:
#: A name outside this -- a `.local` mDNS name, a reverse proxy's own domain --
#: is added by setting the variable, which then replaces this list whole.
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]", IP_LITERALS, "*.ts.net", "testserver")


def _public_url(raw: str | None) -> str:
    """`SPENDTRACKER_PUBLIC_URL`, trimmed, or empty. Refused at boot if it is
    not an absolute http(s) origin: a typo here would otherwise go out in
    every invitation link, which is found out by the person who cannot open it.
    """
    value = (raw or "").strip().rstrip("/")
    if not value:
        return ""
    scheme, _, rest = value.partition("://")
    if scheme not in {"http", "https"} or not rest or "/" in rest:
        raise ValueError(
            f"SPENDTRACKER_PUBLIC_URL must be an origin such as "
            f"https://spend.example.ts.net, not {value!r}"
        )
    return value


def _hosts(raw: str | None) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return DEFAULT_ALLOWED_HOSTS
    return tuple(one.strip().lower() for one in raw.split(",") if one.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    secret_key: str
    echo_sql: bool
    #: "development" turns on the OpenAPI schema and the two doc viewers. They
    #: hand an unauthenticated visitor the whole route inventory, so a deployed
    #: instance keeps them off unless somebody asks for them by name.
    environment: str = "production"
    #: The database browser at /db. On by default but it only mounts when a
    #: snapshot has actually been built, so a deployment that never runs
    #: `make snapshot` never serves it. `SPENDTRACKER_DB_VIEW=off` to be sure.
    db_view: str = "on"
    app_name: str = "Spend Tracker"

    #: Whether the session and device cookies carry ``Secure``. True everywhere
    #: that matters, and the only reason it is a setting at all is the local
    #: network.
    #:
    #: ``Secure`` means a browser will not *store* the cookie unless the origin
    #: is trustworthy. ``http://localhost`` counts; ``http://192.168.1.50:8848``
    #: does not. So an instance bound to the LAN over plain HTTP answers 200 to
    #: the sign-in, sets a cookie the browser silently drops, and bounces
    #: straight back to the sign-in screen -- with nothing in the server log,
    #: because as far as the server is concerned it worked. That failure costs
    #: an hour to diagnose and its obvious field fix is to delete the flag
    #: outright, which is how it ends up off in production too.
    #:
    #: Hence: off is a decision somebody types, once, by name. The deployment
    #: this is designed for is HTTPS over the tailnet and never touches it.
    cookie_secure: bool = True

    #: What the `Host` header may name; see `DEFAULT_ALLOWED_HOSTS`. Checked by
    #: `app/hosts.py` before anything else runs, because `/llms.txt`, the agent
    #: descriptor and the CSRF check all trust `request.url.netloc`, and that
    #: is whatever the client wrote.
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS

    #: Where people reach this instance, as an origin: `https://spend.x.ts.net`.
    #: Invitation links and the two discovery documents are built from it
    #: (#207). Empty means "from the request", which is right on `make dev` and
    #: wrong behind a proxy uvicorn does not trust: there the request says
    #: `http://` and the proxy's address, and an invitation went out as a link
    #: `tailscale serve` does not answer. Setting it also takes the `Host`
    #: header out of those documents altogether.
    public_url: str = ""

    #: Cookie and session lifetimes, in seconds. The trusted-device window is
    #: fixed from issuance and deliberately does not slide.
    session_absolute_seconds: int = 60 * 60 * 24 * 30
    session_idle_seconds: int = 60 * 60 * 24 * 14
    trusted_device_seconds: int = 60 * 60 * 24 * 30
    #: How stale ``sessions.last_seen_at`` may get before a request rewrites it.
    #: Writing on every request would make every read a write; 60 seconds is
    #: fine-grained enough for the "someone else is here" indicator and still at
    #: most one write per user per minute.
    session_touch_seconds: int = 60
    #: How recently a session must have been seen to count as online.
    presence_window_seconds: int = 60 * 5

    #: `secret_key` came from `SPENDTRACKER_SECRET_KEY`, not from `secret.key`
    #: in `data_dir` -- which is then never read, and may not exist. What the
    #: boot log and the owners' banner name in recovery mode (#287): telling
    #: an operator that `secret.key` does not open the ledger, beside a
    #: variable that wins over whatever they copy into it, sent them to fix
    #: the one thing the app was ignoring.
    secret_key_from_env: bool = False

    @property
    def secret_key_source(self) -> str:
        """Where the key this process runs with came from, as an operator
        would go and look for it."""
        if self.secret_key_from_env:
            return SECRET_KEY_ENV
        return f"secret.key in {self.data_dir}"

    @classmethod
    def from_env(cls) -> Settings:
        # First, before anything below creates a file: the data directory, the
        # key, and -- once `app.db` opens it -- the database and its WAL. Here
        # because every entry point reads the settings before it writes, the
        # server and every script alike. See `app/permissions.py`.
        tighten_umask()
        data_dir = resolve_data_dir()
        url = os.environ.get("DATABASE_URL") or f"sqlite:///{data_dir / 'spendtracker.sqlite3'}"
        return cls(
            database_url=url,
            data_dir=data_dir,
            secret_key=_load_or_create_secret_key(data_dir),
            secret_key_from_env=bool(_key_from_env()),
            echo_sql=_bool("SPENDTRACKER_ECHO_SQL"),
            environment=os.environ.get("SPENDTRACKER_ENV", "production"),
            db_view=os.environ.get("SPENDTRACKER_DB_VIEW", "on"),
            cookie_secure=_bool("SPENDTRACKER_COOKIE_SECURE", True),
            allowed_hosts=_hosts(os.environ.get("SPENDTRACKER_ALLOWED_HOSTS")),
            public_url=_public_url(os.environ.get("SPENDTRACKER_PUBLIC_URL")),
        )


settings = Settings.from_env()
