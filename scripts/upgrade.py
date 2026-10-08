"""The upgrade drill: what would change, what it would cost, and then doing it.

The only operation in this project whose failure mode is unrecoverable, so it
is one command rather than seven remembered ones, and it refuses rather than
guesses.

    python -m scripts.upgrade --check            # report only. Writes nothing.
    python -m scripts.upgrade --check --json     # the same, as one JSON document
    python -m scripts.upgrade                    # the drill
    python -m scripts.upgrade --yes --report PATH   # unattended: no prompt, outcome to PATH

## Exit status

A person reads the output; an updater reads the exit status, and it has to be
able to tell "nothing happened" from "the ledger is migrated and the key does
not open it". So the codes are distinct and this is where they are defined.

    0   done. The backup is verified, the migration ran, the stamp is at head,
        the key opens a secret, no table lost rows.
    1   refused before anything changed: the port is still answering, the
        stamp is unknown to this code, the prompt was declined, or the backup
        could not be taken or verified. The ledger is as it was.
    2   a usage error (argparse's own).
    3   the migration failed. The backup is intact; the ledger may not be.
    4   the migration ran and left migrations pending, which should not happen.
    5   secret.key does not open a real TOTP secret in the migrated ledger.
    6   a table holds fewer rows than the backup counted. Outranks 5 when both
        are true, because a lost ledger is worse than lost authenticators.

Anything 3 and up is "restore from the backup folder the output names". With
`--report PATH` the same facts go into a JSON document, whatever the exit.

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
import json
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


def gather() -> dict:
    """Everything `--check` reports, as values. Reads the ledger; writes nothing.

    One place, so the sentences a person reads and the document an updater
    parses cannot drift: `report` renders this, `--json` prints it. The
    updater's prepare step (design notes, 4.4) runs this from the *new* image
    against the live volume and keeps `pending` as printed, which is why each
    migration carries its verdict here rather than only a summary.
    """
    from app import __version__, build
    from app.config import settings
    from scripts import in_a_container

    at = stamped()
    upcoming = pending(at)
    there, commit = published()
    walked = chain()

    # A passkey works only for the host name it was made under, so one made
    # under another name is the one thing an upgrade check can see that
    # nothing else reports (#47 §1.2).
    stranded: list[str] = []
    if settings.database_url.startswith("sqlite:"):
        from app.auth import passkeys

        database = pathlib.Path(settings.database_url.split("///", 1)[-1])
        stranded = list(passkeys.stranded(passkeys.hosts_in(database), settings.rp_id))

    return {
        "data_dir": str(settings.data_dir),
        "app_version": __version__,
        "commit": build.short(build.current()),
        "database_stamped": at,
        "code_head": walked[-1].revision if walked else None,
        # `None` for both when nothing could say: no remote, no network, or
        # no git in the image. `in_container` is what tells the two apart.
        "published": {"version": there, "commit": commit},
        "in_container": in_a_container(),
        "passkeys": stranded,
        "pending": [
            {
                "revision": one.revision,
                "title": one.title,
                "reversible": one.reversible,
                "note": one.note,
            }
            for one in upcoming
        ],
        # `undeclared` counts as lossy: a migration that does not say is not
        # one to assume the best of. The same reading the screen gives it.
        "lossy": any(one.reversible != "clean" for one in upcoming),
    }


def report(say: Step, facts: dict | None = None) -> list[Migration]:
    from app import __version__

    facts = gather() if facts is None else facts
    at = facts["database_stamped"]
    upcoming = pending(at)
    there, commit = facts["published"]["version"], facts["published"]["commit"]

    say("")
    say("  What is deployed")
    # Here rather than only in `run`: `--check` is the command the README and
    # UPGRADING.md send people to when they ask where their data is, and it
    # used to answer everything except that.
    say(f"    data directory   {facts['data_dir']}")
    say(f"    app version      {__version__}")
    say(f"    database stamped {at or 'nothing -- this database has never been migrated'}")
    for line in facts["passkeys"]:
        say(f"    passkeys         {line}")
    say("")
    say("  What is published on main")
    if there is None and facts["in_container"]:
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

    if facts["lossy"]:
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


def _key_still_opens_a_secret() -> tuple[bool, str]:
    """Can `secret.key` still decrypt what is in the database? The verdict, and why.

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
                return True, "secret.key: nothing enrolled yet, so nothing to check against"
            crypto.open_totp_secret(row.totp_secret, user_id=row.id)
    except Exception as broken:  # noqa: BLE001 - any failure here is the same answer
        return False, (
            f"SECRET.KEY CANNOT DECRYPT THIS DATABASE ({type(broken).__name__}). "
            "Do not start the service: every authenticator will be refused. The "
            "wrong key is in the data directory, or it was not restored with it."
        )
    return True, "secret.key opens a real TOTP secret, so authenticators will still work"


#: The exit codes, named. The docstring is where they are explained.
DONE = 0
REFUSED = 1
MIGRATION_FAILED = 3
STILL_PENDING = 4
KEY_DOES_NOT_OPEN = 5
ROWS_DROPPED = 6


def _write_report(path: pathlib.Path, outcome: dict) -> None:
    """The outcome, as one document. Written whole or not at all.

    An updater reads this after the process has gone, so it must never find
    half a file: the write goes beside the target and is renamed into place.
    """
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(json.dumps(outcome, indent=2) + "\n")
    scratch.replace(path)


def run(port: int, *, yes: bool, report_to: pathlib.Path | None = None) -> int:
    from app import __version__
    from app.config import settings
    from scripts import backup as backup_script
    from scripts import in_a_container

    say = Step()
    say(f"Spend Tracker upgrade, {dt.datetime.now().astimezone().isoformat(timespec='seconds')}")

    # What `--report` writes. Filled in as the run goes, so whatever it reaches
    # -- a refused port, a failed backup, a migration that did not finish --
    # the document says how far it got and what is on disk.
    outcome: dict = {
        "started_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "finished_at": None,
        "app_version": __version__,
        "data_dir": str(settings.data_dir),
        "exit": None,
        "outcome": None,
        "backup": {"folder": None, "verified": False},
        "migrated": False,
        "stamp": {"before": None, "after": None},
        "rows": {"before": {}, "after": {}},
        "dropped": [],
        "key": {"ok": None, "message": None},
        "error": None,
        "log": say.lines,
    }
    folder: pathlib.Path | None = None

    def finish(code: int, word: str) -> int:
        outcome["exit"] = code
        outcome["outcome"] = word
        outcome["finished_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        if folder is not None:
            (folder / "upgrade.log").write_text("\n".join(say.lines) + "\n")
        if report_to is not None:
            _write_report(report_to, outcome)
        return code

    try:
        facts = gather()
        outcome["stamp"]["before"] = facts["database_stamped"]
        upcoming = report(say, facts)

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
            wording = (
                "Some of these cannot be undone. Type 'upgrade' to go ahead: "
                if facts["lossy"]
                else "Type 'upgrade' to go ahead: "
            )
            if input(wording).strip() != "upgrade":
                say("Nothing was changed.")
                return finish(REFUSED, "declined")

        say("")
        say("Taking a backup. This runs VACUUM INTO, which reads across the WAL, and")
        say("then reopens the copy and counts its rows before reporting success.")
        folder = backup_script.take()
        say(f"  backup       {folder}")
        say("  verified     the copy was reopened, its revision read, its rows counted")
        # `take` has already reopened the copy and counted it against the live
        # database, so the manifest's counts are the "before" of every table.
        manifest = json.loads((folder / "manifest.json").read_text())
        outcome["backup"] = {"folder": str(folder), "verified": True}
        outcome["rows"]["before"] = manifest["rows"]

        if not upcoming:
            say("")
            say("Nothing to migrate. The backup is taken; start the service again.")
            outcome["stamp"]["after"] = outcome["stamp"]["before"]
            outcome["rows"]["after"] = manifest["rows"]
            return finish(DONE, "nothing-to-migrate")

        return _migrate(port, say, folder, outcome, finish)
    except BaseException as stopped:
        # A refusal is a sentence (SystemExit with a string) and exits 1; a
        # traceback is a bug and exits 1 too. Either way the report says what
        # stopped the run, because an updater cannot read the terminal.
        code = stopped.code if isinstance(stopped, SystemExit) else None
        outcome["error"] = str(stopped) or type(stopped).__name__
        finish(code if isinstance(code, int) else REFUSED, "stopped")
        raise


def _migrate(port: int, say: Step, folder: pathlib.Path, outcome: dict, finish) -> int:
    """From the backup onwards: the placard, alembic, and the three checks."""
    from app import __version__
    from app.config import settings
    from scripts import backup as backup_script
    from scripts import in_a_container

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
            outcome["stamp"]["after"] = stamped()
            return finish(MIGRATION_FAILED, "migration-failed")
        outcome["migrated"] = True

        say("")
        say("Checking the result.")
        now = stamped()
        outcome["stamp"]["after"] = now
        say(f"    database stamped {now}")
        left = pending(now)
        if left:
            say(f"    {len(left)} migration(s) still pending, which should not happen")
            return finish(STILL_PENDING, "still-pending")
        checked = backup_script.verify(folder)
        say(f"    the backup is still readable, at {checked['revision']}")
        key_ok, key_said = _key_still_opens_a_secret()
        outcome["key"] = {"ok": key_ok, "message": key_said}
        say(f"    {key_said}")
        # The live database, counted again: a migration that dropped rows shows
        # up here as a number that moved, which is the assertion the CI
        # rehearsal makes too. More rows is a backfill; fewer is the failure.
        import sqlite3

        live = pathlib.Path(settings.database_url.split("///", 1)[-1])
        dropped: list[str] = []
        with sqlite3.connect(f"file:{live}?mode=ro", uri=True) as conn:
            for table, was in sorted(checked["rows"].items()):
                now_count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                outcome["rows"]["after"][table] = now_count
                mark = "" if now_count == was else f"   <-- was {was:,}"
                say(f"    {now_count:>9,}  {table}{mark}")
                if now_count < was:
                    dropped.append(table)
        outcome["dropped"] = dropped
    finally:
        if server is not None:
            server.shutdown()

    # Printed and exited 0 until #155, which a person reading the output
    # catches and an updater does not. Both are "restore from the backup", so
    # both say so and exit with their own code.
    if dropped or not key_ok:
        say("")
        say("DO NOT START THE SERVICE.")
        if dropped:
            say(f"  These tables hold fewer rows than the backup counted: {', '.join(dropped)}.")
        if not key_ok:
            say("  secret.key does not open the migrated ledger.")
        say(f"  The database as it was before is at {folder}.")
        say("  Restore it with:  python -m scripts.restore " + str(folder))
        if dropped:
            return finish(ROWS_DROPPED, "rows-dropped")
        return finish(KEY_DOES_NOT_OPEN, "key-does-not-open")

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

    code = finish(DONE, "done")
    print(f"\nThe full log of this run is at {folder / 'upgrade.log'}")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only; write nothing")
    parser.add_argument(
        "--json", action="store_true", help="with --check: one JSON document instead of prose"
    )
    parser.add_argument("--port", type=int, default=8848, help="the port the app serves on")
    parser.add_argument("--yes", action="store_true", help="do not ask before migrating")
    parser.add_argument(
        "--report", type=pathlib.Path, metavar="PATH",
        help="write the outcome of the run to PATH as JSON, whatever the exit status",
    )
    args = parser.parse_args(argv)

    # Each flag belongs to one mode, and a caller mixing them would otherwise
    # get a file that was never written, or prose where it expected a document.
    if args.json and not args.check:
        parser.error("--json goes with --check")
    if args.report and args.check:
        parser.error("--report is for the run; --check writes nothing. Use --json.")

    if args.check:
        if args.json:
            print(json.dumps(gather(), indent=2))
        else:
            report(Step())
        return 0
    return run(args.port, yes=args.yes, report_to=args.report)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
