"""Build a snapshot of the database that is safe to point a browser at.

Three problems this solves, in order of how likely they are to bite:

1. **The live file is not the whole database.** SQLite runs in WAL mode here, so
   recent writes sit in ``spendtracker.sqlite3-wal`` until a checkpoint folds
   them in. Anything that opens the main file on its own -- a viewer, a backup,
   a copy -- silently shows stale data with no error. ``VACUUM INTO`` takes a
   consistent point-in-time copy with the WAL already applied.

2. **The live file is being written to.** Opening it in a desktop viewer while
   the server is running means competing for the write lock, and a viewer that
   opens read-write can block the app.

3. **The database holds credential material.** Not much, but enough that it
   should not be served over HTTP by something with no authentication in front
   of it. Those columns are blanked in the snapshot, so what gets served cannot
   leak them however it is reached.

The snapshot is a throwaway. Regenerate it whenever you want current data.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

#: Tables that are nothing but live authentication state. A snapshot has no use
#: for them and they are the most sensitive rows in the file, so they are
#: emptied rather than blanked.
#:
#: `login_attempts` joined this list in the 2026-09-19 review. It is the same
#: kind of table as the other three -- a record of what happened at the door,
#: not of the ledger -- and it was the only one carrying an email address and an
#: IP for every attempt ever made. Serving that over HTTP to browse is not what
#: the viewer is for, and with nothing pruning the table it was *every* attempt
#: since first boot.
#: `receipt_blobs` is here for three independent reasons, any one of which
#: would do. The snapshot is a file you point a browser at, and Datasette will
#: run read-only SQL for whoever reaches it -- so it must not contain
#: photographs of a household's life. It would multiply the file's size by
#: fifty. And nothing in a browsable copy can render a BLOB usefully anyway.
PURGE = (
    "sessions",
    "trusted_devices",
    "pending_sign_ins",
    "step_up_grants",
    # A challenge is spent or swept within minutes; there is nothing to browse.
    "webauthn_challenges",
    "login_attempts",
    "agent_requests",
    "agent_replays",
    "receipt_blobs",
)

#: Tables a reader is meant to see whole. Everything in the ledger, plus the
#: audit log, plus the instance row.
#:
#: This list exists so that :data:`PURGE`, :data:`REDACTIONS` and this together
#: **classify every table in the file**, and :func:`build` refuses one it has
#: never heard of. Before it, both loops skipped what they did not recognise and
#: said nothing -- so a table added later landed in the snapshot unredacted,
#: with no error and no failing test.
#:
#: It is the same shape as `Base.__audit__`, and for the same reason: *"every
#: table is either in the audit log or deliberately out of it"*. Every table is
#: now either safe to browse or deliberately not, and a new one has to say
#: which before a snapshot can be built.
CARRIED_WHOLE = (
    "accounts",
    "alembic_version",
    "batches",
    "categories",
    "category_groups",
    "changes",
    "household_members",
    "households",
    "instance",
    # Carried whole, `pattern` and all: a rule is the household's own words for
    # its payees, which the ledger shows anyway.
    "payee_rules",
    "payees",
    # Carried whole, which includes where and when each photo was taken --
    # `gps_lat`, `gps_lon`, `captured_at`, `camera` -- and the `exif` it was
    # read from. The photographs themselves are `receipt_blobs`, purged above.
    "receipts",
    "reconciliations",
    "transactions",
    # Two transaction ids, who and when: pairs a person said are not a
    # transfer (#131). Nothing in it that the ledger does not already show.
    "transfer_rejections",
)

#: table -> {column: replacement}, for tables worth reading with the secret
#: parts taken out. Everything blanked here is a hash, a key or a token: none
#: of it reversible on its own, but a password hash is still something to grind
#: offline and a sealed TOTP secret is one stolen `secret.key` away from being a
#: second factor.
#:
#: `|| rowid` on the unique ones is not decoration. `invitations.token_hash`
#: carries a unique index, so setting every row to the same literal fails on the
#: second row -- which a database with one invitation, or none, never shows you.
REDACTIONS: dict[str, dict[str, str]] = {
    "users": {
        "password_hash": "'-- redacted --'",
        "totp_secret": "x''",
        "totp_last_counter": "NULL",
        # Random, and stored by the member's authenticators: a browsable copy
        # has no use for it. NULL, not a literal, for its unique index.
        "webauthn_user_handle": "NULL",
    },
    "recovery_codes": {"code_hash": "'-- redacted --'"},
    "invitations": {"token_hash": "'-- redacted -- ' || rowid"},
    # Which accounts have a reset link outstanding, what it resets and who
    # issued it is what an owner would open this to check (#284). The token's
    # hash goes, `|| rowid` for its unique index as above.
    "account_resets": {"token_hash": "'-- redacted -- ' || rowid"},
    # REDACTIONS rather than PURGE: `agent_keys` is audited, and which keys
    # exist, what they are for and when they expire is exactly the kind of
    # thing somebody opens this viewer to check. Only the credential goes.
    #
    # `|| rowid` for the same reason `invitations.token_hash` has it -- the
    # column carries a unique index, so one literal for every row fails on the
    # second, in a way an instance with a single key never shows you.
    "agent_keys": {"token_hash": "'-- redacted -- ' || rowid"},
    # Which passkeys a member has, what they are called, the host each was made
    # for and when it was last used is what somebody opens this to check. The
    # public key is not a secret, but a browsable copy has no use for it, and
    # the credential id is what an authenticator presents to sign in; it goes
    # with `|| rowid` for its unique index.
    "passkeys": {
        "public_key": "x''",
        "credential_id": "'-- redacted -- ' || rowid",
    },
    # IBANs, account and card numbers, and how a bank spells a person's name
    # (issue #66). Which account has *an* identifier of which kind is worth
    # browsing; the number itself is not something a snapshot should carry.
    # The last four stay, which is what a person recognises an account by, and
    # `normalised` gets `|| rowid` because it carries the unique index.
    "account_identifiers": {
        # Spaces out first: an IBAN is typed in groups of four, and the last
        # four characters of `GB82 ... 7654 32` are `4 32`.
        "value": "'…' || substr(replace(value, ' ', ''), -4)",
        "normalised": "'…' || substr(normalised, -4) || '#' || rowid",
    },
    # Identifiers a person was offered and said no to (#130). An ignored
    # `A/C` or card number is still somebody's number -- often one of the
    # household's own, which is why it was suggested -- so it is cut to its
    # last four exactly as `account_identifiers` is. `|| rowid` on
    # `normalised` for the unique index it is part of.
    "ignored_identifier_suggestions": {
        "value": "'…' || substr(replace(value, ' ', ''), -4)",
        "normalised": "'…' || substr(normalised, -4) || '#' || rowid",
    },
    # The statement line exactly as the bank wrote it, and what was read out of
    # it (issue #91). A bank line routinely carries the account's own IBAN or a
    # card number, so this was a second copy of what `account_identifiers`
    # above is careful not to carry. What the line *became* -- outcome, reason,
    # the transaction it made -- stays, and that is what a reader of an import
    # wants. `raw` is NOT NULL, hence a literal rather than NULL.
    "import_lines": {
        "raw": "'-- redacted --'",
        "parsed": "NULL",
    },
}

#: Identifier kinds that are a number rather than a word. Their full value is
#: additionally scrubbed from **every text column of every table** -- see
#: `_scrub_known_numbers`. Holder names and aliases are not: they are words a
#: payee legitimately shares, and masking "Instant Access Savings" out of a
#: memo would damage the ledger's own record to hide nothing secret.
_NUMBER_KINDS = ("iban", "number", "card")

#: Below this a "number" is a last-four, which is already what the snapshot
#: shows and is what a person recognises an account by.
_SCRUB_MIN = 6


def database_path() -> pathlib.Path:
    url = settings.database_url
    if not url.startswith("sqlite"):
        raise SystemExit(f"this only knows how to snapshot SQLite, not {url!r}")
    return pathlib.Path(url.split("///", 1)[1]).resolve()


#: SQLite's own bookkeeping, which is not anybody's data.
_SQLITE_INTERNAL = {"sqlite_sequence", "sqlite_stat1", "sqlite_autoindex"}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute("select name from sqlite_master where type='table'")
    }


def _refuse_unclassified(present: set[str]) -> None:
    """Stop before writing anything if a table is unaccounted for.

    The loops below both skip what they do not recognise, which is right for a
    database that is behind the code and wrong for one that is ahead of it: a
    table added later would land in the snapshot exactly as it sits in the
    ledger, with no error and no failing test. So every table is classified,
    and an unclassified one is a refusal rather than a silent copy.

    The same promise `Base.__audit__` makes about the audit log -- every table
    is in it or deliberately out of it -- made about the browsable copy.
    """
    unclassified = sorted(
        name
        for name in present - set(PURGE) - set(REDACTIONS) - set(CARRIED_WHOLE)
        if not name.startswith("sqlite_") and name not in _SQLITE_INTERNAL
    )
    if unclassified:
        raise SystemExit(
            "scripts/db_view.py does not know what to do with "
            + ", ".join(unclassified)
            + ".\nAdd each one to PURGE, REDACTIONS or CARRIED_WHOLE and say why. "
            "Until then this snapshot would serve it as-is."
        )


def _redact_value(snap: sqlite3.Connection, expression: str, image: dict, seq: int):
    """One REDACTIONS expression, evaluated against a JSON image of the row.

    The expressions are SQL written against the live columns (`substr(value,
    -4)`, `'...' || rowid`), so they are evaluated as SQL here too -- over a
    one-row subquery that names the image's values as those columns -- rather
    than being re-implemented in Python, where the two would drift. `rowid`
    is the change's own sequence number: the table's rowid is not in the
    image, and it is only there to keep a unique index happy.
    """
    names = [key for key in image if key != "rowid"]
    values = [
        json.dumps(image[key]) if isinstance(image[key], (dict, list)) else image[key]
        for key in names
    ]
    columns = ", ".join([*(f'? AS "{key}"' for key in names), "? AS rowid"])
    (result,) = snap.execute(
        f"SELECT {expression} FROM (SELECT {columns})",  # noqa: S608
        (*values, seq),
    ).fetchone()
    return None if isinstance(result, bytes) else result


def _redact_changes(snap: sqlite3.Connection) -> int:
    """Apply each redacted table's redactions to its rows' images in `changes`.

    Issue #91. `account_identifiers` is audited with nothing in
    `__audit_redact__` -- undo needs the number to put it back -- so every
    insert or edit of an IBAN kept the full value in `changes.before` and
    `changes.after`, and the snapshot copied `changes` whole. Blanking the
    table and carrying its history verbatim redacted nothing.

    The auth tables are already redacted at write time by `__audit_redact__`,
    so for them this finds the column absent and leaves it absent.
    """
    touched = 0
    for table, columns in REDACTIONS.items():
        rows = snap.execute(
            "SELECT seq, before, after FROM changes WHERE table_name = ?", (table,)
        ).fetchall()
        for seq, before, after in rows:
            images = []
            for raw in (before, after):
                image = json.loads(raw) if raw else None
                if isinstance(image, dict):
                    for column, expression in columns.items():
                        if image.get(column) is not None:
                            image[column] = _redact_value(snap, expression, image, seq)
                images.append(None if image is None else json.dumps(image))
            snap.execute(
                "UPDATE changes SET before = ?, after = ? WHERE seq = ?", (*images, seq)
            )
            touched += 1
    return touched


def _scrub_known_numbers(snap: sqlite3.Connection) -> int:
    """Take the household's own account numbers out of every text column.

    Belt and braces for #91, and it has to be, because the numbers do not stay
    in the two tables that hold them. A transfer's descriptor names the IBAN it
    went to, so it is in `transactions.payee`; a staged import keeps its
    `source`; an agent's rows keep `details`. Listing every column that *might*
    hold one is the approach that missed `changes` and `import_lines` in the
    first place. So: every identifier that is a number, matched with or
    without the spacing a bank prints it with, in every text column of every
    table, replaced by the same `…` + last four that `account_identifiers`
    shows.

    Read from the copy before REDACTIONS truncates it -- the snapshot is the
    only place this runs, and the full values are needed exactly once, here.
    """
    kinds = ", ".join("?" for _ in _NUMBER_KINDS)
    found = {
        row[0]
        for row in snap.execute(
            f"SELECT normalised FROM account_identifiers WHERE kind IN ({kinds})",  # noqa: S608
            _NUMBER_KINDS,
        )
    }
    # And every one that has since been removed, which is still named in the
    # payees and memos of the transfers it once recognised. The audit log is
    # the only place its value survives, so that is where it is read from.
    for images in snap.execute(
        "SELECT before, after FROM changes WHERE table_name = 'account_identifiers'"
    ):
        for raw in images:
            image = json.loads(raw) if raw else None
            if isinstance(image, dict) and image.get("kind") in _NUMBER_KINDS:
                found.add(image.get("normalised"))
    numbers = sorted(
        (one for one in found if isinstance(one, str) and len(one) >= _SCRUB_MIN),
        key=len,
        reverse=True,
    )
    if not numbers:
        return 0
    pattern = re.compile(
        "|".join(r"[\s-]?".join(re.escape(ch) for ch in number) for number in numbers),
        re.IGNORECASE,
    )

    def scrub(text):
        if not isinstance(text, str):
            return text
        return pattern.sub(
            lambda found: "…" + re.sub(r"[\s-]", "", found.group(0))[-4:], text
        )

    snap.create_function("scrub_numbers", 1, scrub, deterministic=True)
    scrubbed = 0
    # `account_identifiers` itself is REDACTIONS' job, and scrubbing its
    # unique `normalised` here would collide two accounts sharing a last four.
    for table in sorted(_tables(snap) - {"account_identifiers"}):
        if table.startswith("sqlite_"):
            continue
        for _, column, declared, *_ in snap.execute(f"PRAGMA table_info({table})"):
            if not any(t in (declared or "").upper() for t in ("CHAR", "TEXT", "CLOB", "JSON")):
                continue
            where = f'WHERE "{column}" IS NOT NULL AND "{column}" != scrub_numbers("{column}")'
            try:
                done = snap.execute(
                    f'UPDATE {table} SET "{column}" = scrub_numbers("{column}") {where}'  # noqa: S608
                )
            except sqlite3.IntegrityError:
                # A unique column in which two values differ only by the
                # number. Kept apart by rowid, the way REDACTIONS does it.
                done = snap.execute(
                    f'UPDATE {table} SET "{column}" = '  # noqa: S608
                    f"scrub_numbers(\"{column}\") || '#' || rowid {where}"
                )
            scrubbed += done.rowcount or 0
    return scrubbed


def build(source: pathlib.Path, destination: pathlib.Path) -> dict[str, int]:
    if not source.exists():
        raise SystemExit(f"no database at {source}. Run `make migrate` first.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    # Read-only, so a running server is never disturbed. VACUUM INTO writes a
    # consistent copy even while the source is being written to.
    live = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        # Refused here, against the *source*, and not one line later against the
        # copy. Checking the copy is too late by definition: by then the file
        # exists on disk with the unclassified table and everything in it, so a
        # refusal that leaves it there has refused nothing. The first version of
        # this check did exactly that, and its own test caught it.
        _refuse_unclassified(_tables(live))
        live.execute("VACUUM INTO ?", (str(destination),))
    finally:
        live.close()

    wiped: dict[str, int] = {}
    snap = sqlite3.connect(destination)
    try:
        present = _tables(snap)

        # A redaction naming a column that is no longer there is the same
        # failure wearing a different hat: the column was renamed, the blanking
        # silently stopped applying, and the secret is back in the snapshot.
        for table, columns in REDACTIONS.items():
            if table not in present:
                continue
            have = {row[1] for row in snap.execute(f"PRAGMA table_info({table})")}
            gone = sorted(set(columns) - have)
            if gone:
                raise SystemExit(
                    f"{table} no longer has the column(s) {', '.join(gone)}, which "
                    "REDACTIONS blanks. Update the list -- silently skipping it puts "
                    "whatever replaced them into the snapshot."
                )

        # Before the REDACTIONS below, which truncate the very numbers this
        # has to look for.
        if "account_identifiers" in present:
            scrubbed = _scrub_known_numbers(snap)
            if scrubbed:
                wiped["(account numbers found in other columns)"] = scrubbed
        if "changes" in present:
            wiped["changes"] = _redact_changes(snap)

        for table in PURGE:
            if table in present:
                done = snap.execute(f"DELETE FROM {table}")  # noqa: S608
                wiped[table] = done.rowcount or 0
        for table, columns in REDACTIONS.items():
            if table not in present:
                continue
            sets = [f"{col} = {value}" for col, value in columns.items()]
            # Plain SQL against a detached copy: this file is not the ledger and
            # nothing here is audited. The app's own writes still go through the
            # ORM and a batch, as they must.
            done = snap.execute(f"UPDATE {table} SET {', '.join(sets)}")  # noqa: S608
            wiped[table] = done.rowcount or 0
        snap.commit()
        snap.execute("VACUUM")
    finally:
        snap.close()
    return wiped


def default_destination() -> pathlib.Path:
    """Where the snapshot goes, and so where `/db` and `make db-view` look.

    Beside the database, in the resolved data directory. This is the only
    place that answers the question: `make db-view` once named
    `data/snapshot.sqlite3` itself, and when the default data directory moved
    to `~/.local/share/spend-tracker` the snapshot moved with it and the
    Makefile did not -- so the snapshot was built and Datasette was handed a
    path that did not exist (#64).
    """
    return pathlib.Path(settings.data_dir) / "snapshot.sqlite3"


def serve(destination: pathlib.Path, port: int) -> None:
    """Replace this process with Datasette over the snapshot just written.

    Loopback only, always. On its own Datasette has no authentication and will
    run read-only SQL for anybody who reaches it; `/db` is the way in from
    another machine.
    """
    datasette = pathlib.Path(sys.executable).parent / "datasette"
    if not datasette.exists():
        raise SystemExit(
            f"no datasette beside {sys.executable}. It is a development "
            "dependency: `make install-py` installs it."
        )
    argv = [
        str(datasette),
        str(destination),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--setting", "allow_download", "off",
        "--setting", "suggest_facets", "off",
    ]  # fmt: skip
    print(f"   serving: http://127.0.0.1:{port}  (loopback only)", flush=True)
    os.execv(argv[0], argv)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(default_destination()),
        help="where to write the snapshot",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="then browse it with Datasette on 127.0.0.1",
    )
    parser.add_argument("--port", type=int, default=8899, help="for --serve")
    args = parser.parse_args()

    source = database_path()
    destination = pathlib.Path(args.out).resolve()
    wiped = build(source, destination)

    print(f"snapshot of {source}")
    print(f"        -> {destination}  ({destination.stat().st_size / 1024:.0f} KB)")
    if wiped:
        summary = ", ".join(f"{table} ({rows})" for table, rows in sorted(wiped.items()))
        print(f"   redacted: {summary}")
    else:
        print("   redacted: nothing to redact -- no credential rows present")
    if args.serve:
        serve(destination, args.port)


if __name__ == "__main__":
    main()
