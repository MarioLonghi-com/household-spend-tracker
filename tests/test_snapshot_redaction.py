"""The `/db` snapshot carries no full account number anywhere. Issue #91.

`scripts/db_view.py` cut `account_identifiers.value` down to its last four and
then copied `changes` -- whose before and after images hold the full value of
every identifier ever added or removed -- and `import_lines.raw`, the bank's
own line, whole. The table was redacted and two copies of what it held went
out beside it.

So this does what a household does -- adds identifiers, removes one, imports a
statement line naming the account's IBAN -- builds the snapshot, and reads
every text column of every table looking for the numbers, spaced or not.

Every number here is made up: `GB82WEST12345698765432` is the IBAN registry's
own example, and the others are nobody's.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from tests.conftest import HEADERS, _setup_owner

IBAN = "GB82WEST12345698765432"
CARD = "4111119988776655"
NUMBER = "70112233"
#: A deliberately failing check digit, so it cannot be anybody's account
#: (`test_data_hygiene` refuses any valid IBAN but the registry example).
SECOND_IBAN = "DE00370400440532013000"
HOLDER = "Doe Jane Fictional"

#: As a bank prints it on a line, in groups of four.
SPACED_IBAN = " ".join(IBAN[i : i + 4] for i in range(0, len(IBAN), 4))

STATEMENT = (
    "Fecha;Concepto;Importe;Saldo\r\n"
    f"05/01/2026;TRANSFERENCIA A {SPACED_IBAN};-450,00;8.810,07\r\n"
    f"07/01/2026;TARJETA {CARD} MERCADONA;-45,20;8.764,87\r\n"
    f"09/01/2026;RECIBO CUENTA {SECOND_IBAN};-12,50;8.752,37\r\n"
)


def _household(client) -> tuple[str, str, str]:
    _setup_owner(client)
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    base = f"/api/households/{house['id']}"
    checking = client.post(
        f"{base}/accounts", json={"name": "Checking", "type": "checking"}, headers=HEADERS
    ).json()
    savings = client.post(
        f"{base}/accounts", json={"name": "Savings", "type": "savings"}, headers=HEADERS
    ).json()
    return base, checking["id"], savings["id"]


def _identify(client, base: str, kind: str, value: str, account: str | None) -> dict:
    made = client.post(
        f"{base}/identifiers",
        json={"kind": kind, "value": value, "account_id": account},
        headers=HEADERS,
    )
    assert made.status_code == 201, made.text
    return made.json()


def _every_text_value(path: Path):
    snap = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in snap.execute("select name from sqlite_master where type='table'")
            if not row[0].startswith("sqlite_")
        ]
        for table in tables:
            for _, column, *_ in snap.execute(f"PRAGMA table_info({table})"):
                for (value,) in snap.execute(f'select "{column}" from {table}'):  # noqa: S608
                    if isinstance(value, str):
                        yield table, column, value
    finally:
        snap.close()


def _build(client, tmp_path) -> tuple[Path, dict]:
    import app.db as db
    from scripts.db_view import build

    source = Path(str(db.engine.url).split("///", 1)[1])
    destination = tmp_path / "snap.sqlite3"
    return destination, build(source, destination)


def test_no_text_column_of_the_snapshot_carries_a_full_account_number(client, tmp_path):
    base, checking, savings = _household(client)
    _identify(client, base, "iban", SPACED_IBAN, checking)
    _identify(client, base, "card", CARD, checking)
    _identify(client, base, "number", NUMBER, savings)
    # Added and then removed: the number now lives ONLY in the audit log's
    # before and after images, which is the copy that was being missed.
    gone = _identify(client, base, "iban", SECOND_IBAN, savings)
    assert client.delete(f"{base}/identifiers/{gone['id']}", headers=HEADERS).status_code == 204
    # How the bank spells the holder. Not a number, so nothing scrubs it out of
    # the ledger's payees -- but the identifier's own history is redacted the
    # way the table is, and that is the half of the fix this one proves.
    _identify(client, base, "holder", HOLDER, None)

    staged = client.post(
        f"{base}/imports",
        data={"account_id": checking, "force": "false"},
        files={"file": ("enero.csv", STATEMENT.encode(), "text/csv")},
        headers=HEADERS,
    )
    assert staged.status_code == 201, staged.text
    committed = client.post(
        f"{base}/imports/{staged.json()['batch_id']}/commit", json={}, headers=HEADERS
    )
    assert committed.status_code == 200, committed.text

    import app.db as db

    # The premise, checked on the ledger itself: every number is there to find,
    # so a clean snapshot is the snapshot's doing and not an empty fixture's.
    live = Path(str(db.engine.url).split("///", 1)[1])
    before = "\n".join(value for _, _, value in _every_text_value(live))
    for number in (IBAN, CARD, NUMBER, SECOND_IBAN):
        assert number in before.replace(" ", ""), number

    destination, wiped = _build(client, tmp_path)

    # Spacing ignored, the way a bank ignores it.
    hunted = [
        re.compile(r"[\s-]?".join(re.escape(ch) for ch in number), re.IGNORECASE)
        for number in (IBAN, CARD, NUMBER, SECOND_IBAN)
    ]
    leaks = [
        (table, column, value[:80])
        for table, column, value in _every_text_value(destination)
        if any(pattern.search(value) for pattern in hunted)
    ]
    assert leaks == []

    # And what a reader opens the snapshot for is still there: which account
    # has which identifier, by its last four, and what each line became.
    snap = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        shown = {row[0] for row in snap.execute("select value from account_identifiers")}
        assert shown == {"…5432", "…6655", "…2233", "…onal"}
        # The history of the identifiers table, redacted the way the table is.
        history = [
            row[0]
            for row in snap.execute(
                "select coalesce(before, '') || coalesce(after, '') from changes "
                "where table_name = 'account_identifiers'"
            )
        ]
        assert len(history) >= 5
        assert not [one for one in history if HOLDER.upper() in one.upper()]

        lines = snap.execute("select raw, parsed, outcome from import_lines").fetchall()
        assert [(raw, parsed) for raw, parsed, _ in lines] == [("-- redacted --", None)] * 3
        assert all(outcome for _, _, outcome in lines)
        payees = " ".join(
            row[0] or "" for row in snap.execute("select name from payees")
        )
        assert "…5432" in payees and "MERCADONA" in payees
    finally:
        snap.close()
    assert wiped["changes"] > 0


def test_an_ignored_suggestion_is_cut_to_its_last_four_in_the_snapshot(client, tmp_path):
    """A number a person ignored is not one of the household's identifiers, so
    the scrub of known numbers never hears of it: the table's own redaction,
    and its history's, are all that stand between it and the snapshot (#130)."""
    base, _, _ = _household(client)
    ignored = client.post(
        f"{base}/identifiers/suggestions/ignore",
        json={"kind": "number", "value": NUMBER}, headers=HEADERS,
    )
    assert ignored.status_code == 201, ignored.text

    destination, _ = _build(client, tmp_path)
    leaks = [
        (table, column) for table, column, value in _every_text_value(destination)
        if NUMBER in value
    ]
    assert leaks == []
    snap = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
    try:
        assert [row[0] for row in snap.execute("select value from ignored_identifier_suggestions")] == [
            "…2233"
        ]
    finally:
        snap.close()
