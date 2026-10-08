"""The client's copy of the money rules must match the server's.

app/money.py and client/src/lib/money.ts both hold the list of currencies whose
minor unit is not two decimal places. The previous build had exactly this
duplication with a comment acknowledging it and nothing checking it, so the two
could drift apart and the only symptom would be a wrong number on a screen.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.money import _EXPONENTS

# Reads files outside the backend; runs on every pull request. See tests.yml.
pytestmark = pytest.mark.repo_wide

TS_FILE = Path(__file__).resolve().parent.parent / "client" / "src" / "lib" / "money.ts"


def _exponents_from_typescript() -> dict[str, int]:
    source = TS_FILE.read_text()
    match = re.search(r"export const EXPONENTS: Record<string, number> = \{(.*?)\};", source, re.S)
    assert match, "EXPONENTS is not where the test expects it in money.ts"

    body = match.group(1)
    # Strip comments, then read the object as JSON with quoted keys.
    body = re.sub(r"//.*", "", body)
    pairs = re.findall(r"([A-Z]{3})\s*:\s*(\d+)", body)
    return {code: int(value) for code, value in pairs}


@pytest.mark.skipif(not TS_FILE.exists(), reason="the client is not checked out")
def test_the_client_knows_the_same_currencies_as_the_server():
    from_ts = _exponents_from_typescript()
    assert from_ts == _EXPONENTS, (
        "money.ts and money.py disagree about minor units. "
        f"only in the client: {set(from_ts) - set(_EXPONENTS)}; "
        f"only in the server: {set(_EXPONENTS) - set(from_ts)}"
    )


@pytest.mark.skipif(not TS_FILE.exists(), reason="the client is not checked out")
def test_both_default_to_two_decimal_places():
    """Anything unlisted is a two-decimal currency on both sides."""
    from app.money import exponent

    assert exponent("EUR") == 2, "the server defaults to two"
    assert "EUR" not in _exponents_from_typescript(), "so EUR is deliberately not in either table"
    assert "?? 2" in TS_FILE.read_text(), "and the client falls back to the same two"


# --------------------------------------------------------------------------- #
# The tables agreeing is not enough: the two must round the same way too.
# --------------------------------------------------------------------------- #

CLIENT_DIR = TS_FILE.resolve().parent.parent.parent

#: Values chosen so a float implementation fails. `8.165 * 100` is
#: 816.4999999999999 in binary floating point, so `Math.round` gives 816 while
#: the server's Decimal quantize gives 817 -- one cent, silently, on every
#: hand-typed amount. The client's number is the only one stored for manual
#: entry, so a disagreement here is a disagreement nothing later reconciles.
ROUNDING_CASES = [
    ("8.165", "EUR"),
    ("1.005", "EUR"),
    ("0.145", "EUR"),
    ("2.675", "EUR"),
    ("0.005", "EUR"),
    ("-8.165", "EUR"),
    ("-0.005", "EUR"),
    ("999999.995", "EUR"),
    ("12.34", "EUR"),
    ("1234.5", "JPY"),
    ("8.165", "JPY"),
    ("0.5", "JPY"),
    ("8.1655", "BHD"),
    ("1.0005", "BHD"),
    ("7.7", "GBP"),
]

def _node_runs() -> bool:
    """Is there a node here that actually executes?

    ``shutil.which("npx")`` is not the question. A leftover x86_64 node on an
    arm64 Mac is on ``PATH`` and answers ``which`` perfectly; ``npx`` is a shim
    starting ``#!/usr/bin/env node``, so it re-execs whatever node ``PATH``
    finds first and dies with ``env: node: Bad CPU type in executable``. This
    test then *fails* -- reporting "the client harness failed" under a name
    about rounding, which sends whoever reads it into ``money.ts`` looking for
    an arithmetic bug that is not there.

    `make preflight` already learned this (issue #1) and probes with `node -v`
    for exactly this reason; the same lesson belongs here, one layer down. A
    machine with no usable node should skip this honestly, and a machine with
    one should run it.
    """
    node = shutil.which("node")
    if node is None:
        return False
    try:
        return subprocess.run([node, "-v"], capture_output=True, timeout=30).returncode == 0
    except OSError:
        return False


_HARNESS = """
import { parse } from "./src/lib/money.ts";
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(([t, c]) => parse(t, c))));
"""


def _client_parse(cases: list[tuple[str, str]], tmp_path: Path) -> list[int | None]:
    # Named per process: it is written into the client checkout, not tmp_path,
    # because the import has to resolve against client/src. Two xdist workers
    # (or two worktrees' runs sharing nothing but this name) must not delete
    # each other's harness mid-run.
    harness = CLIENT_DIR / f"money_harness_{os.getpid()}.mts"
    harness.write_text(_HARNESS)
    try:
        done = subprocess.run(
            ["npx", "--no-install", "tsx", str(harness), json.dumps([list(one) for one in cases])],
            cwd=CLIENT_DIR,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        harness.unlink(missing_ok=True)
    assert done.returncode == 0, f"the client harness failed:\n{done.stderr}"
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not TS_FILE.exists(), reason="the client is not checked out")
@pytest.mark.skipif(not _node_runs(), reason="node is missing, or will not run on this machine")
@pytest.mark.skipif(
    not (CLIENT_DIR / "node_modules").exists(), reason="client dependencies are not installed"
)
def test_the_client_rounds_exactly_as_the_server_does(tmp_path):
    from app.money import to_minor

    got = _client_parse(ROUNDING_CASES, tmp_path)
    wrong = [
        (text, currency, mine, to_minor(text, currency))
        for (text, currency), mine in zip(ROUNDING_CASES, got, strict=True)
        if mine != to_minor(text, currency)
    ]
    assert not wrong, "money.ts and money.py round differently: " + "; ".join(
        f"{c} {t!r}: client={a} server={b}" for t, c, a, b in wrong
    )


# --------------------------------------------------------------------------- #
# The receipt shape, on both sides
# --------------------------------------------------------------------------- #

TYPES_FILE = TS_FILE.resolve().parent / "types.ts"


def _interface_fields(name: str, file: Path | None = None) -> set[str]:
    file = file or TYPES_FILE
    source = file.read_text()
    match = re.search(rf"(?:export )?interface {name} \{{(.*?)\n\}}", source, re.S)
    assert match, f"{name} is not where the test expects it in {file.name}"
    body = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S)
    body = re.sub(r"//.*", "", body)
    return set(re.findall(r"^\s*(\w+)\??:", body, re.M))


@pytest.mark.skipif(not TYPES_FILE.exists(), reason="the client is not checked out")
def test_the_client_knows_every_field_of_a_receipt():
    """Test 19.

    `ReceiptOut` carries fifteen more fields than a transaction does and most
    of them are optional, so a field added on the server and forgotten on the
    client is invisible: the panel renders, the row is just missing. This is
    what makes that a failing test instead.
    """
    from app.schemas import ReceiptOut

    server = set(ReceiptOut.model_fields)
    client = _interface_fields("Receipt")
    assert server == client, (
        f"only on the server: {sorted(server - client)}; "
        f"only in the client: {sorted(client - server)}"
    )


@pytest.mark.skipif(not TYPES_FILE.exists(), reason="the client is not checked out")
def test_the_client_knows_a_register_row_says_whether_it_has_a_receipt():
    from app.schemas import TransactionOut

    assert "has_receipt" in TransactionOut.model_fields
    assert "has_receipt" in _interface_fields("Transaction"), (
        "the paperclip is driven by this field, and a missing one renders as "
        "undefined, which is falsy -- so every row would quietly lose its marker"
    )


RESULTS_FILE = TS_FILE.resolve().parent.parent / "screens" / "ynab" / "results.tsx"


@pytest.mark.skipif(not RESULTS_FILE.exists(), reason="the client is not checked out")
def test_the_import_report_knows_how_many_suggestions_the_rules_screen_lists():
    """#269: the report says "(the 20 largest are listed)" on both sides, and
    a cap changed on one side only would make one of them wrong."""
    from app.services.payees import SUGGESTIONS_LISTED

    match = re.search(r"export const RULES_LISTED = (\d+);", RESULTS_FILE.read_text())
    assert match, "RULES_LISTED is not where the test expects it in results.tsx"
    assert int(match.group(1)) == SUGGESTIONS_LISTED


# --------------------------------------------------------------------------- #
# The Application screen's answers that #165 changed
# --------------------------------------------------------------------------- #

SCREENS = TS_FILE.resolve().parent.parent / "screens"


@pytest.mark.skipif(not SCREENS.exists(), reason="the client is not checked out")
@pytest.mark.parametrize(
    ("model", "interface", "file"),
    [
        ("UpstreamOut", "Upstream", "Updates.tsx"),
        ("ReleaseOut", "Release", "Updates.tsx"),
        ("UpdaterOfferOut", "UpdaterOffer", "Updates.tsx"),
        ("BackupOut", "Backup", "Backups.tsx"),
        # The Updates section (#166): everything `GET .../update` answers with,
        # and what the requests it sends answer.
        ("UpdateStateOut", "UpdateState", "Updates.tsx"),
        ("UpdateHeartbeatOut", "Heartbeat", "Updates.tsx"),
        ("UpdateStatusOut", "UpdateStatus", "Updates.tsx"),
        ("UpdateReportOut", "Report", "Updates.tsx"),
        ("UpdateMigrationOut", "Migration", "Updates.tsx"),
        ("UpdateOutcomeOut", "Outcome", "Updates.tsx"),
        ("UpdateRecoveryCodeOut", "RecoveryCode", "Updates.tsx"),
        ("UpdateRequestOut", "UpdateRequest", "Updates.tsx"),
    ],
)
def test_the_client_knows_every_field_of_the_check_and_of_a_backup(model, interface, file):
    """The check now returns releases with their notes, and a backup can be an
    update's folder that the server will not delete. A field the client does
    not declare is one the screen cannot show."""
    from app import schemas

    server = set(getattr(schemas, model).model_fields)
    client = _interface_fields(interface, SCREENS / file)
    assert server == client, (
        f"only on the server: {sorted(server - client)}; "
        f"only in the client: {sorted(client - server)}"
    )


@pytest.mark.skipif(not (SCREENS / "Updates.tsx").exists(), reason="the client is not checked out")
def test_the_client_asks_for_the_recovery_code_with_a_post():
    """R21: the route is a `POST` because issuing a code replaces the held one.
    A screen still calling it with `GET` would get a 405 and no code at all."""
    from app.api.routers.updates import router

    methods = {
        method
        for route in router.routes
        if getattr(route, "path", "").endswith("/recovery-code")
        for method in route.methods
    }
    assert methods == {"POST"}
    source = (SCREENS / "Updates.tsx").read_text()
    calls = re.findall(r"api\.(\w+)<\w+>\(`\$\{BASE\}/recovery-code`", source)
    assert calls == ["post"], calls
