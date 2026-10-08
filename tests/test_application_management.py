"""The owner-only page that describes the instance rather than a ledger.

Four things are being asserted here, and the first is the one that matters
most: **none of this is reachable by a member.** The answers name every
household on the instance and count its rows, say where the database and the
secret key are on disk, and hand back whatever the application has written
about a request. A page that is merely hidden from the nav is not access
control, so every route is walked as a member and every one of them has to
refuse.

The other three: the figures are the real ones and not placeholders, the log
file exists and is readable from here with no way to read anything else, and
each of the four operations does what it says to something a test can check.
"""

from __future__ import annotations

import json

from tests.conftest import HEADERS, _setup_owner
from tests.test_invitations import _accept, _invite

#: Every route on the page, with the method it answers. Walked as a member.
ROUTES = [
    ("get", "/api/admin/application"),
    ("get", "/api/admin/application/tables.csv"),
    ("get", "/api/admin/application/logging"),
    ("post", "/api/admin/application/logging"),
    ("get", "/api/admin/application/logs/app.log"),
    ("get", "/api/admin/application/backups"),
    ("post", "/api/admin/application/backups"),
    ("get", "/api/admin/application/backups/spendtracker-x.sqlite3/download"),
    ("delete", "/api/admin/application/backups/spendtracker-x.sqlite3"),
    ("post", "/api/admin/application/upstream"),
]


def _instance(client) -> dict:
    answer = client.get("/api/admin/application", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _a_household_with_something_in_it(client) -> str:
    house = client.post("/api/households", json={"name": "Ours"}, headers=HEADERS).json()
    account = client.post(
        f"/api/households/{house['id']}/accounts",
        json={"name": "Checking", "type": "checking"},
        headers=HEADERS,
    ).json()
    for day, amount in (("2026-01-04", -1250), ("2026-01-05", 90000)):
        made = client.post(
            f"/api/households/{house['id']}/transactions",
            json={"account_id": account["id"], "date": day, "amount": amount},
            headers=HEADERS,
        )
        assert made.status_code == 201, made.text
    return house["id"]


# --------------------------------------------------------------------------- #
# Who can reach it
# --------------------------------------------------------------------------- #


def test_a_member_is_refused_every_route_on_the_page(client):
    """Not hidden. Refused, by the server, one route at a time.

    403 rather than 404 throughout, which is the rule the rest of the admin
    panel follows: somebody signed in as a member knows perfectly well that
    this instance has a database and a log, so pretending the route does not
    exist would be theatre.
    """
    _setup_owner(client)
    created = _invite(client, role="member")
    client.cookies.clear()
    _accept(client, created["token"], email="member@gmail.com", name="Member")

    for method, path in ROUTES:
        answer = getattr(client, method)(
            path, **({"json": {"style": "verbose"}} if method == "post" else {}), headers=HEADERS
        )
        assert answer.status_code == 403, f"{method.upper()} {path} answered {answer.status_code}"
        assert "owner" in answer.json()["detail"].lower()


def test_signed_out_it_is_not_reachable_at_all(client):
    _setup_owner(client)
    client.cookies.clear()
    for method, path in ROUTES:
        answer = getattr(client, method)(
            path, **({"json": {"style": "verbose"}} if method == "post" else {}), headers=HEADERS
        )
        assert answer.status_code == 401, f"{method.upper()} {path} answered {answer.status_code}"


# --------------------------------------------------------------------------- #
# What it says about the instance
# --------------------------------------------------------------------------- #


def test_the_page_describes_this_instance_and_not_a_template(client):
    _setup_owner(client)
    house = _a_household_with_something_in_it(client)
    it = _instance(client)

    assert it["version"], "the version of the software"
    assert it["schema_revision"], "the migration this database is actually at"
    assert it["database_url_scheme"] == "sqlite"
    assert it["python"].startswith("3."), it["python"]
    assert it["repository"].endswith("/household-spend-tracker")
    assert it["author"] == "https://mariolonghi.com/projects"

    # The size is the file on disk, and the file has something in it.
    assert it["size"]["main_bytes"] > 0
    assert it["size"]["total_bytes"] >= it["size"]["main_bytes"]
    assert it["size"]["page_size"] in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)

    # The amount of data, per household, counted rather than guessed.
    mine = next(one for one in it["households"] if one["id"] == house)
    assert mine["name"] == "Ours"
    assert mine["transactions"] == 2, "two rows were entered above"
    assert mine["receipts"] == 0

    # Where the files are, and whether each one is actually there.
    places = {one["what"]: one for one in it["places"]}
    assert places["Database"]["exists"] is True
    assert places["Database"]["path"].endswith(".sqlite3")
    assert places["Secret key"]["exists"] is True
    assert "back this up with the database" in places["Secret key"]["note"]
    assert places["Backups"]["exists"] is False, "nothing has been backed up yet"

    # What it is made of, and where it can be reached.
    names = {one["name"].lower() for one in it["packages"]}
    assert {"fastapi", "sqlalchemy", "pydantic"} <= names, sorted(names)[:10]
    assert any(one.startswith("http://") for one in it["addresses"])


def test_it_says_which_process_is_answering(client):
    """The one number that tells two instances on one machine apart.

    A dev run and the real one are identical on every other fact the screen
    carries, so "which of these is on 8848" ended in a terminal. Asserted as
    *this* process rather than as "an integer is present": a plausible number
    that belongs to something else is the failure worth catching.
    """
    import os

    _setup_owner(client)
    it = _instance(client)

    assert it["process_id"] == os.getpid(), "it should be the process that served the request"


def test_it_says_what_the_database_is_and_not_only_how_big(client):
    """It reported `sqlite`, as a note under the size, and nothing else.

    Not which SQLite, not whether it was in WAL mode -- although the WAL and
    SHM entries in Places already assumed it was -- and not where the file
    was, which was filed under paths rather than under the database.
    """
    import sqlite3

    _setup_owner(client)
    _a_household_with_something_in_it(client)
    it = _instance(client)

    engine = it["engine"]
    assert engine["name"] == "SQLite"
    assert engine["version"] == sqlite3.sqlite_version, (
        "the version reported has to be the one actually linked in"
    )
    assert engine["journal_mode"] is not None
    assert engine["path"] == next(
        one["path"] for one in it["places"] if one["what"] == "Database"
    ), "the database section and Places must name the same file"


def test_the_database_path_never_comes_from_the_url(client):
    """`settings.database_url` can carry a password; `database_path()` cannot.

    Asserted on the shape rather than on a contrived credential: the reported
    path is an absolute file with no scheme and no `@`, which a URL-derived
    value would fail on the first of those.
    """
    _setup_owner(client)
    it = _instance(client)

    path = it["engine"]["path"]
    assert path and path.startswith("/"), path
    assert "://" not in path and "@" not in path, "that looks like a URL, not a file"
    assert path.endswith(".sqlite3"), path


def test_the_household_figures_move_when_the_ledger_does(client):
    """Counted live. A number that never changes is a number nobody can trust."""
    _setup_owner(client)
    house = _a_household_with_something_in_it(client)
    before = next(one for one in _instance(client)["households"] if one["id"] == house)

    account = client.get(f"/api/households/{house}/accounts", headers=HEADERS).json()[0]
    client.post(
        f"/api/households/{house}/transactions",
        json={"account_id": account["id"], "date": "2026-02-02", "amount": -500},
        headers=HEADERS,
    )

    after = next(one for one in _instance(client)["households"] if one["id"] == house)
    assert after["transactions"] == before["transactions"] + 1


def test_the_table_report_is_a_csv_of_every_table_with_its_rows(client):
    import csv
    import io

    _setup_owner(client)
    _a_household_with_something_in_it(client)

    answer = client.get("/api/admin/application/tables.csv", headers=HEADERS)
    assert answer.status_code == 200, answer.text
    assert answer.headers["content-type"].startswith("text/csv")
    assert "attachment;" in answer.headers["content-disposition"]

    rows = list(csv.DictReader(io.StringIO(answer.text)))
    by_table = {row["table"]: row for row in rows}
    assert "transactions" in by_table and "users" in by_table and "changes" in by_table
    assert int(by_table["transactions"]["rows"]) == 2, "the two rows entered above"
    assert int(by_table["users"]["rows"]) == 1
    assert int(by_table["transactions"]["bytes"]) > 0
    # Which measurement produced the byte figure, on every row, because the two
    # are not comparable and a reader who does not know will compare them.
    assert {row["measured_by"] for row in rows} <= {"dbstat", "measured"}


# --------------------------------------------------------------------------- #
# The logs
# --------------------------------------------------------------------------- #


def test_the_instance_writes_a_log_file_and_the_page_can_read_it(client):
    """The whole point: a self-hosted instance with no journal to read.

    The startup line is asserted specifically because it is written by
    `lifespan` *after* `logging_setup.configure()` -- if the handler were
    attached too late, everything the boot has to say would go nowhere, which
    is the state this replaced.
    """
    _setup_owner(client)

    listed = client.get("/api/admin/application/logging", headers=HEADERS)
    assert listed.status_code == 200, listed.text
    state = listed.json()
    assert state["current"] == "normal", "the default, until somebody chooses"
    assert [one["key"] for one in state["styles"]] == ["quiet", "normal", "verbose", "sql"]
    assert any(one["echo_sql"] for one in state["styles"]), "one style says it logs SQL"
    assert state["directory"].endswith("logs")

    names = [one["name"] for one in state["files"]]
    # Three streams, separated on purpose -- see `app/logging_setup.STREAMS`.
    # `sql.log` exists and is empty until somebody chooses the `sql` style.
    assert "app.log" in names, names

    body = client.get("/api/admin/application/logs/app.log", headers=HEADERS)
    assert body.status_code == 200, body.text
    assert "schema at" in body.json()["text"], "the boot's own lines reached the file"
    assert body.json()["bytes"] > 0


def test_a_log_file_that_is_not_in_the_listing_is_a_404_and_not_a_path(client):
    """The traversal this deliberately does not have.

    `logging_setup.tail` matches the name against the listing rather than
    joining it onto a directory, so there is no path to normalise and no
    containment check to get subtly wrong.
    """
    from app.config import settings

    _setup_owner(client)
    secret = (settings.data_dir / "secret.key").read_text().strip()
    assert secret, "the fixture has no key, so this test proves nothing"

    # A name that could be a file in that directory, and is not in the listing.
    for name in ("nope.log", "secret.key", "logging.json"):
        answer = client.get(f"/api/admin/application/logs/{name}", headers=HEADERS)
        assert answer.status_code == 404, f"{name} answered {answer.status_code}: {answer.text}"
        assert answer.json()["detail"] == "no such log file"

    # And a name carrying a separator. `%2F` is decoded before the route is
    # matched, so these never reach the handler at all -- the assertion is on
    # the outcome rather than on the status, because "which layer said no" is
    # the implementation detail and "the key did not come back" is the point.
    for name in ("..%2Fsecret.key", "..%2F..%2F..%2Fetc%2Fpasswd", "%2E%2E%2Fsecret.key"):
        answer = client.get(f"/api/admin/application/logs/{name}", headers=HEADERS)
        assert secret not in answer.text, f"{name} handed back the secret key"
        assert answer.status_code == 404 or answer.headers["content-type"].startswith(
            "text/html"
        ), f"{name} got a JSON payload out of a route it should never have matched"


def test_changing_the_logging_style_takes_effect_and_survives(client):
    """Written to disk, so the choice outlives the process that made it.

    A level in an environment variable means the moment you want more detail is
    the moment you have to restart the thing you were observing.
    """
    import logging

    _setup_owner(client)
    changed = client.post(
        "/api/admin/application/logging", json={"style": "verbose"}, headers=HEADERS
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["current"] == "verbose"

    assert logging.getLogger("spendtracker").level == logging.DEBUG, (
        "the running process changed, not just a file"
    )

    from app.config import settings

    stored = json.loads((settings.data_dir / "logging.json").read_text())
    assert stored == {"style": "verbose"}

    # And reading it back says the same thing.
    assert client.get("/api/admin/application/logging", headers=HEADERS).json()["current"] == (
        "verbose"
    )

    # Put it back, so a style that echoes the ledger cannot leak into the rest
    # of the run through a module-level logger.
    client.post("/api/admin/application/logging", json={"style": "normal"}, headers=HEADERS)


def test_a_logging_style_that_is_not_one_is_refused_by_name(client):
    _setup_owner(client)
    refused = client.post(
        "/api/admin/application/logging", json={"style": "everything"}, headers=HEADERS
    )
    assert refused.status_code == 422, refused.text
    assert "quiet" in refused.json()["detail"], "and it says what the choices are"


# --------------------------------------------------------------------------- #
# The operations
# --------------------------------------------------------------------------- #


def test_a_backup_is_a_real_database_that_holds_the_same_rows(client):
    """Not that a file appeared -- that the file is the ledger.

    `VACUUM INTO` rather than a file copy, because this database runs in WAL
    mode and the main file on its own is missing every write since the last
    checkpoint. So the assertion is on the contents, opened with sqlite3: a
    backup that restores to last Tuesday looks perfectly valid from the outside.
    """
    import sqlite3

    _setup_owner(client)
    _a_household_with_something_in_it(client)

    assert client.get("/api/admin/application/backups", headers=HEADERS).json() == []

    made = client.post("/api/admin/application/backups", headers=HEADERS)
    assert made.status_code == 201, made.text
    backup = made.json()
    assert backup["bytes"] > 0
    assert backup["name"].startswith("spendtracker-") and backup["name"].endswith(".sqlite3")

    copy = sqlite3.connect(backup["path"])
    try:
        assert copy.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
        assert copy.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    finally:
        copy.close()

    # And it is now the latest backup the page reports.
    assert client.get("/api/admin/application/backups", headers=HEADERS).json()[0]["name"] == (
        backup["name"]
    )
    assert _instance(client)["latest_backup"]["name"] == backup["name"]


def test_the_upstream_check_answers_rather_than_failing_when_there_is_no_route(client):
    """An instance with no way out is the normal case, not an error.

    The network is not reached here: `check_upstream` is replaced, because a
    test that depends on GitHub being up is a test that fails on a train. What
    is being asserted is the contract -- a refusal comes back as a sentence on
    the screen and a 200, never as a 500.
    """
    from datetime import UTC, datetime

    from app.services import platform as platform_service

    original = platform_service.check_upstream
    try:
        platform_service.check_upstream = lambda: platform_service.Upstream(
            checked_at=datetime.now(UTC),
            running="0.1.0",
            latest=None,
            newer=False,
            problem="could not reach the repository: [Errno 8] nodename nor servname provided",
        )
        _setup_owner(client)
        answer = client.post("/api/admin/application/upstream", headers=HEADERS)
        assert answer.status_code == 200, answer.text
        assert answer.json()["problem"].startswith("could not reach")
        assert answer.json()["newer"] is False
    finally:
        platform_service.check_upstream = original


def test_a_newer_release_upstream_is_reported_as_newer(client):
    from datetime import UTC, datetime

    from app.services import platform as platform_service

    original = platform_service.check_upstream
    try:
        platform_service.check_upstream = lambda: platform_service.Upstream(
            checked_at=datetime.now(UTC),
            running="0.1.0",
            latest="0.2.0",
            newer=True,
            problem=None,
        )
        _setup_owner(client)
        answer = client.post("/api/admin/application/upstream", headers=HEADERS).json()
        assert (answer["latest"], answer["newer"], answer["problem"]) == ("0.2.0", True, None)
    finally:
        platform_service.check_upstream = original


def test_the_version_comparison_orders_by_number_and_not_by_string(client):
    """`v1.2.10` is newer than `v1.2.9`, which sorting as text gets backwards."""
    from app.services.platform import _as_numbers

    assert _as_numbers("v1.2.10") > _as_numbers("v1.2.9")
    assert _as_numbers("0.10.0") > _as_numbers("0.9.9")
    assert _as_numbers("1.2.0-rc1") == (1, 2, 0), "a pre-release compares as its release"
    assert _as_numbers("demo") == (), "and a tag that is not a version is ignored"


def test_setup_state_says_where_the_token_was_written(client):
    """The screen used to hard-code `data/setup-token`.

    Wrong in every container -- it is `/var/lib/spend-tracker/setup-token`
    there -- and wrong for any install since the default data directory moved
    out of the checkout. A hard-coded path in an instruction is a guess about
    somebody else's machine.
    """
    from app.auth import setup as setup_service

    state = client.get("/api/setup/state", headers=HEADERS).json()
    assert state["setup_required"] is True
    assert state["token_path"] == str(setup_service.setup_token_path())
    # And it is the file the token is actually in, not a path that looks right.
    assert setup_service.setup_token_path().read_text().strip()


def test_a_configured_instance_says_nothing_about_its_filesystem(client):
    """The path is only useful while the instance is unclaimed."""
    _setup_owner(client)
    state = client.get("/api/setup/state", headers=HEADERS).json()
    assert state == {"setup_required": False}
