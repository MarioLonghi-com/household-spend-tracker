"""The upgrade drill: what would change, what it would cost, and then doing it.

The only operation in this project whose failure mode is unrecoverable, so it
is one command rather than seven remembered ones, and it refuses rather than
guesses.

    python -m scripts.upgrade --check     # report only. Writes nothing.
    python -m scripts.upgrade             # the drill

## What it does, and what it deliberately does not

It does: read what is deployed, read what is published, work out which
migrations sit between them, say which of those cannot be undone, take a
verified backup, hold the port with a maintenance page, run the migrations,
prove the result opens, and write a log of all of it.

It does **not** stop or start your service. It cannot: systemd, a container, a
terminal somebody left running and `tailscale serve` are four different answers
and guessing wrong is worse than asking. It refuses to run while the port is
still answering, and tells you what to do.

It does **not** `git pull`. Which commit to deploy is a decision; a script that
makes it while also migrating a database is a script whose mistakes are two
deep. `--check` tells you what the delta is and you check out the tag yourself.

## Why the backup is the whole point

`app/schema_check.py` refuses to boot against a database that is behind or
ahead, and says which revision each side is at, so a mismatched deploy is one
sentence at startup rather than a 500 on sign-in three days later. That guard
is free and it is excellent, and it protects against the *wrong* failure.

**Alembic runs DDL and raw SQL on a plain connection.** No ORM session, no
flush, no `before_flush` hook -- so the audit log, which is what makes every
other mistake in this app undoable, never sees a migration at all.
`e1f3a77c04b2` rewrites every household's theme and there is no `batches` row
for it anywhere. The backup is the only thing standing between a bad migration
and a lost ledger, and the project's own strongest feature invites exactly the
wrong assumption here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import http.server
import pathlib
import re
import socket
import subprocess
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "migrations" / "versions"

#: Two spellings of the same declaration live in `migrations/versions/`: the
#: four oldest files were generated when Alembic's template still wrote
#: `Union[str, Sequence[str], None]`, and everything since writes
#: `str | Sequence[str] | None`. Matching only the modern one silently cut the
#: chain at the fifth migration and made the head look like an unknown
#: revision -- so the annotation is skipped entirely rather than spelled out.
_REVISION = re.compile(r"""^revision: [^=]+= ['"](?P<id>[^'"]+)['"]""", re.M)
_DOWN = re.compile(r"^down_revision: [^=]+= (?P<down>.+)$", re.M)
_REVERSIBLE = re.compile(r"^Reversible: (?P<verdict>\w+) -- (?P<note>.+)$", re.M)
_TITLE = re.compile(r'^"""(?P<title>.+)$', re.M)
_VERSION_IN = re.compile(r'__version__ = "(?P<version>[^"]+)"')


class Step:
    """One line of the audit log, printed as it happens and kept for the file."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        stamped = f"{dt.datetime.now().astimezone().isoformat(timespec='seconds')}  {text}"
        self.lines.append(stamped)
        print(text, flush=True)


# --------------------------------------------------------------------------- #
# What is between here and there
# --------------------------------------------------------------------------- #


class Migration:
    def __init__(self, path: pathlib.Path) -> None:
        text = path.read_text()
        self.path = path
        self.revision = (_REVISION.search(text) or {"id": path.name})["id"]
        down = _DOWN.search(text)
        raw = down["down"].strip() if down else "None"
        self.down = None if raw == "None" else raw.strip('"').strip("'")
        title = _TITLE.search(text)
        self.title = title["title"].strip() if title else path.stem
        found = _REVERSIBLE.search(text)
        self.reversible = found["verdict"] if found else "undeclared"
        self.note = found["note"] if found else "this migration does not say."

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return f"<Migration {self.revision} {self.reversible}>"


def all_migrations() -> dict[str, Migration]:
    return {m.revision: m for m in (Migration(p) for p in VERSIONS.glob("*.py"))}


def chain() -> list[Migration]:
    """Every migration, oldest first, following `down_revision`."""
    by_id = all_migrations()
    children = {m.down: m for m in by_id.values()}
    ordered: list[Migration] = []
    cursor = children.get(None)
    while cursor is not None:
        ordered.append(cursor)
        cursor = children.get(cursor.revision)
    return ordered


def pending(from_revision: str | None) -> list[Migration]:
    """The migrations between the database's stamp and the code's head."""
    ordered = chain()
    if from_revision is None:
        return ordered
    for index, one in enumerate(ordered):
        if one.revision == from_revision:
            return ordered[index + 1 :]
    # Stamped at something this code has never heard of: the database is ahead,
    # or from a branch that was never merged. `schema_check` says the same
    # thing at boot; saying it here means nobody starts an upgrade first.
    raise SystemExit(
        f"this database is stamped {from_revision}, which is not a revision this code "
        "knows about. It is ahead of this checkout, or it came from a branch that was "
        "never merged. Do not migrate it: work out which code wrote it first."
    )


def stamped() -> str | None:
    import sqlite3

    from app.config import settings

    if not settings.database_url.startswith("sqlite:"):
        raise SystemExit("this drill is written for SQLite; see the docstring")
    path = pathlib.Path(settings.database_url.split("///", 1)[-1])
    if not path.exists():
        return None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        try:
            row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.OperationalError:
            return None
    return row[0] if row else None


def published() -> tuple[str | None, str | None]:
    """The version on the published `main`, and the commit it is at.

    Read with `git show origin/main:app/__init__.py` rather than by pulling:
    this reports, it does not deploy. `(None, None)` when there is no remote or
    no network, which is a perfectly ordinary state for a self-hosted box and
    not a reason to refuse to upgrade.
    """
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", "origin", "main"],
            cwd=ROOT, check=True, capture_output=True, timeout=30,
        )
        blob = subprocess.run(
            ["git", "show", "origin/main:app/__init__.py"],
            cwd=ROOT, check=True, capture_output=True, text=True, timeout=30,
        ).stdout
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "origin/main"],
            cwd=ROOT, check=True, capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None, None
    found = _VERSION_IN.search(blob)
    return (found["version"] if found else None), commit


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #


def report(say: Step) -> list[Migration]:
    from app import __version__
    from app.config import settings
    from scripts import in_a_container

    at = stamped()
    upcoming = pending(at)
    there, commit = published()

    say("")
    say("  What is deployed")
    # Here rather than only in `run`: `--check` is the command the README and
    # UPGRADING.md send people to when they ask where their data is, and it
    # used to answer everything except that.
    say(f"    data directory   {settings.data_dir}")
    say(f"    app version      {__version__}")
    say(f"    database stamped {at or 'nothing -- this database has never been migrated'}")
    # A passkey works only for the host name it was made under, so one made
    # under another name is the one thing an upgrade check can see that
    # nothing else reports (#47 §1.2).
    if settings.database_url.startswith("sqlite:"):
        from app.auth import passkeys

        database = pathlib.Path(settings.database_url.split("///", 1)[-1])
        for line in passkeys.stranded(passkeys.hosts_in(database), settings.rp_id):
            say(f"    passkeys         {line}")
    say("")
    say("  What is published on main")
    if there is None and in_a_container():
        # There is no git in the image, and there should not be. Saying "no
        # remote, or no network" would send somebody looking for a network
        # problem that does not exist.
        say("    not checked -- there is no git in the image, by design.")
        say("    This reports what *this image* would do to the volume it is")
        say("    looking at, which is the question that matters here: you")
        say("    already chose the image by pulling or building it.")
    elif there is None:
        say("    unknown -- no remote, or no network. Reporting on this checkout alone.")
    else:
        say(f"    app version      {there}  ({commit})")
        if there == __version__:
            say("    the same version this checkout is. Nothing new has been released.")
    say("")

    if not upcoming:
        say("  No migrations to run. The database is already at this code's head.")
        return upcoming

    say(f"  {len(upcoming)} migration{'s' if len(upcoming) > 1 else ''} would run:")
    say("")
    for one in upcoming:
        say(f"    {one.revision}  {one.title}")
        say(f"      rolling back: {one.reversible} -- {one.note}")
    say("")

    lossy = [one for one in upcoming if one.reversible != "clean"]
    if lossy:
        say("  ROLLING BACK WOULD DESTROY SOMETHING.")
        say("")
        say("    Checking out the old tag is not enough after these. `alembic")
        say("    downgrade` restores the shape and cannot restore the content, so")
        say("    the only faithful way back is restoring the backup -- and")
        say("    everything written since the upgrade is then gone too.")
        say("")
        say("    Decide now, not at 23:00: is this release one you are prepared")
        say("    to go forward from?")
    else:
        say("  Every one of these is cleanly reversible. `alembic downgrade` plus the")
        say("  old tag would put you back with nothing lost.")
    say("")
    return upcoming


# --------------------------------------------------------------------------- #
# The placard
# --------------------------------------------------------------------------- #

PLACARD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upgrading</title>
<style>
 :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
 body {{ margin: 0; display: grid; place-items: center; min-height: 100vh; padding: 16px; }}
 main {{ max-width: 34rem; }}
 h1 {{ font-size: 1.4rem; margin: 0 0 .6rem; }}
 p {{ line-height: 1.5; }}
 pre {{ overflow-x: auto; padding: .8rem; border-radius: .4rem;
        background: color-mix(in srgb, currentColor 8%, transparent); font-size: .85rem; }}
</style></head>
<body><main>
<h1>This instance is being upgraded</h1>
<p>Your ledger is not being served right now. A verified backup was taken
before anything was changed, and it is still there whatever happens next.</p>
<p>This page is the upgrade itself answering, not the application &mdash; so if
you can read it, the upgrade is still running.</p>
<pre>{log}</pre>
</main></body></html>
"""


#: The placard is a static page with one inline stylesheet and nothing else,
#: so it can say so. It carries alembic's output, and a migration that prints
#: something shaped like markup -- a column default, a docstring, an error
#: quoting a value from the ledger -- is not something to hand a browser as
#: HTML. Escaping is the fix; this is the layer behind it.
PLACARD_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"


def render_placard(lines: list[str]) -> bytes:
    """The page, with the log escaped. Alembic's stdout and stderr are text."""
    return PLACARD.format(log=html.escape("\n".join(lines[-40:]))).encode()


class Placard(http.server.BaseHTTPRequestHandler):
    """503 and the log, on the app's own port, while the app is down.

    A connection refused says nothing; this says what is happening, that the
    backup exists, and how far it has got. `Retry-After` is what a reverse
    proxy reads, and 503 is what stops anything caching the page.
    """

    say: Step

    def do_GET(self) -> None:  # noqa: N802 - the stdlib names it
        body = render_placard(self.say.lines)
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", PLACARD_CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", "120")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: D102 - silence the stdlib's stderr
        pass


def port_is_busy(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


# --------------------------------------------------------------------------- #
# The drill
# --------------------------------------------------------------------------- #


def _key_still_opens_a_secret() -> str:
    """Can `secret.key` still decrypt what is in the database?

    The question the review asked as "check if the database and keys will be
    accessible after the upgrade", and it is a real one with a specific failure
    mode: an upgrade that restores the ledger against the **wrong**
    `secret.key` leaves every row readable and every authenticator refused.
    That failure does not surface until somebody tries to sign in, which on a
    single-household instance can be days.

    So: take one real sealed TOTP secret out of the upgraded database and open
    it. Nothing is printed but whether it worked -- the secret itself is the
    one thing in this app that must not reach a log file, and this writes a log
    file.

    A household with no enrolled user yet has nothing to check, and says so.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.auth import crypto
    from app.db import engine
    from app.models import User

    try:
        with Session(engine) as session:
            row = session.execute(
                select(User).where(User.totp_secret.is_not(None)).limit(1)
            ).scalars().first()
            if row is None:
                return "secret.key: nothing enrolled yet, so nothing to check against"
            crypto.open_totp_secret(row.totp_secret, user_id=row.id)
    except Exception as broken:  # noqa: BLE001 - any failure here is the same answer
        return (
            f"SECRET.KEY CANNOT DECRYPT THIS DATABASE ({type(broken).__name__}). "
            "Do not start the service: every authenticator will be refused. The "
            "wrong key is in the data directory, or it was not restored with it."
        )
    return "secret.key opens a real TOTP secret, so authenticators will still work"


def run(port: int, *, yes: bool) -> int:
    from app import __version__
    from app.config import settings
    from scripts import backup as backup_script
    from scripts import in_a_container

    say = Step()
    say(f"Spend Tracker upgrade, {dt.datetime.now().astimezone().isoformat(timespec='seconds')}")

    upcoming = report(say)

    if port_is_busy(port) and not in_a_container():
        raise SystemExit(
            f"something is still answering on port {port}.\n\n"
            "Stop the service first. This does not stop it for you, because systemd,\n"
            "a container and a terminal somebody left running are three different\n"
            "answers and guessing wrong is worse than asking.\n\n"
            "Then run this again. It will hold the port with a maintenance page so\n"
            "anyone visiting sees what is happening rather than a refused connection."
        )

    if upcoming and not yes:
        lossy = [one for one in upcoming if one.reversible != "clean"]
        wording = (
            "Some of these cannot be undone. Type 'upgrade' to go ahead: "
            if lossy
            else "Type 'upgrade' to go ahead: "
        )
        if input(wording).strip() != "upgrade":
            say("Nothing was changed.")
            return 1

    say("")
    say("Taking a backup. This runs VACUUM INTO, which reads across the WAL, and")
    say("then reopens the copy and counts its rows before reporting success.")
    folder = backup_script.take()
    say(f"  backup       {folder}")
    say("  verified     the copy was reopened, its revision read, its rows counted")

    if not upcoming:
        say("")
        say("Nothing to migrate. The backup is taken; start the service again.")
        (folder / "upgrade.log").write_text("\n".join(say.lines) + "\n")
        return 0

    # Skipped in a container, where it would bind the container's *own*
    # loopback -- an address nothing outside it can reach, so it would serve
    # the maintenance page to nobody while looking like it had worked. In
    # Docker the unit that is down is the container, and `docker compose stop`
    # is what took it down.
    server = None
    if in_a_container():
        say("  no maintenance page: in a container it would bind an address")
        say("  nothing outside the container can reach.")
    else:
        Placard.say = say
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Placard)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        say(f"  maintenance page on http://127.0.0.1:{port}")

    try:
        say("")
        say("Migrating.")
        done = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT, capture_output=True, text=True,
        )
        for line in (done.stdout + done.stderr).splitlines():
            if line.strip():
                say(f"    {line.rstrip()}")
        if done.returncode != 0:
            say("")
            say("THE MIGRATION FAILED. Nothing has been started back up.")
            say(f"The database as it was before is at {folder}.")
            say("Restore it with:  python -m scripts.restore " + str(folder))
            (folder / "upgrade.log").write_text("\n".join(say.lines) + "\n")
            return done.returncode

        say("")
        say("Checking the result.")
        now = stamped()
        say(f"    database stamped {now}")
        left = pending(now)
        if left:
            say(f"    {len(left)} migration(s) still pending, which should not happen")
            (folder / "upgrade.log").write_text("\n".join(say.lines) + "\n")
            return 1
        checked = backup_script.verify(folder)
        say(f"    the backup is still readable, at {checked['revision']}")
        say(f"    {_key_still_opens_a_secret()}")
        # The live database, counted again: a migration that dropped rows shows
        # up here as a number that moved, which is the assertion the CI
        # rehearsal makes too.
        import sqlite3

        live = pathlib.Path(settings.database_url.split("///", 1)[-1])
        with sqlite3.connect(f"file:{live}?mode=ro", uri=True) as conn:
            for table, was in sorted(checked["rows"].items()):
                now_count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                mark = "" if now_count == was else f"   <-- was {was:,}"
                say(f"    {now_count:>9,}  {table}{mark}")
    finally:
        if server is not None:
            server.shutdown()

    say("")
    say("Done. What to do now:")
    if in_a_container():
        say("  1. Start the service.            docker compose up -d")
        say("  2. Check what is running.        curl -s localhost:8848/api/health")
    else:
        say(f"  1. Start the service.            make serve PORT={port}")
        say(f"  2. Check what is running.        curl -s localhost:{port}/api/health")
    say(f"     It must answer version {__version__}.")
    say("  3. Sign in, and open the register. If the ledger is wrong, stop and")
    say(f"     restore: python -m scripts.restore {folder}")
    say("")
    say(f"  The backup stays at {folder} until you remove it. Keep it until you")
    say("  have used the app for a day, not until the page loads.")

    (folder / "upgrade.log").write_text("\n".join(say.lines) + "\n")
    print(f"\nThe full log of this run is at {folder / 'upgrade.log'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only; write nothing")
    parser.add_argument("--port", type=int, default=8848, help="the port the app serves on")
    parser.add_argument("--yes", action="store_true", help="do not ask before migrating")
    args = parser.parse_args(argv)

    if args.check:
        report(Step())
        return 0
    return run(args.port, yes=args.yes)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
