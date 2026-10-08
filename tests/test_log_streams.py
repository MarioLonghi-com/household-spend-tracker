"""Separated, timestamped logs, and a file that says when its level moved.

Three asks, and the second one was a real defect rather than a nicety.

**Separated.** One file held the app's own output, one line per HTTP request,
and -- at the `sql` style -- every statement and every row it returned. The
last of those is the ledger in plain text, so "send me your log" could not be
asked safely. Three files now, and `sql.log` is the only one that is dangerous.

**Timestamped.** `app.log.1` says nothing and *moves*: the `.1` you were
reading is `.2` a minute later. Rotations are named for the moment they were
closed, and the size bound is kept, because a log that fills the disk takes the
ledger with it.

**The toggle shows in the file.** It did not. `choose()` wrote
`log.info("logging style is now quiet")` *after* raising the level to WARNING,
so the line was filtered out -- turning logging down left no record that it had
been turned down, which is the one transition anybody would look for
afterwards.
"""

from __future__ import annotations

import logging

from app import logging_setup
from tests.conftest import HEADERS, _setup_owner


def _read(client, name: str) -> str:
    path = logging_setup.log_dir() / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _set_style(client, key: str):
    made = client.post(
        "/api/admin/application/logging", json={"style": key}, headers=HEADERS
    )
    assert made.status_code == 200, made.text
    return made.json()


# --------------------------------------------------------------------------- #
# Separated
# --------------------------------------------------------------------------- #


def test_the_three_streams_exist_and_are_named(client):
    _setup_owner(client)
    state = client.get("/api/admin/application/logging", headers=HEADERS).json()
    assert [one["filename"] for one in state["streams"]] == [
        "app.log", "access.log", "sql.log",
    ]
    # And the one that is dangerous to share says so, so the screen can mark it.
    dangerous = [one["filename"] for one in state["streams"] if one["holds_ledger_values"]]
    assert dangerous == ["sql.log"]


def test_request_lines_do_not_land_in_the_application_log(client):
    """Separation that only *duplicates* is not separation.

    `uvicorn.access` propagates to the root logger by default, so attaching a
    second handler without claiming the logger would put every request line in
    both files. The one that must never be duplicated is `sql.log`.
    """
    _setup_owner(client)
    logging.getLogger("uvicorn.access").warning("GET /api/marker-for-the-test 200")

    assert "marker-for-the-test" in _read(client, "access.log")
    assert "marker-for-the-test" not in _read(client, "app.log")


def test_the_ledger_only_ever_goes_to_its_own_file(client):
    """The reason the split is worth a migration of habits.

    At the `sql` style SQLAlchemy writes every statement and every row it
    returned. If that reached `app.log`, the file somebody sends to get help
    would carry their transactions.
    """
    _setup_owner(client)
    _set_style(client, "sql")

    house = client.post("/api/households", json={"name": "Loud"}, headers=HEADERS)
    assert house.status_code == 201, house.text

    assert "INSERT INTO households" in _read(client, "sql.log")
    assert "INSERT INTO households" not in _read(client, "app.log")
    assert "INSERT INTO households" not in _read(client, "access.log")


# --------------------------------------------------------------------------- #
# The toggle, written down
# --------------------------------------------------------------------------- #


def test_turning_logging_down_still_says_so_in_the_file(client):
    """The defect. Quiet raises the level to WARNING; an INFO line is dropped.

    Asserted on the *content of the file after the change*, not on the call
    having been made -- the old code called a logger too.
    """
    _setup_owner(client)
    _set_style(client, "quiet")

    written = _read(client, "app.log")
    assert "logging style changing: normal -> quiet" in written
    assert "logging style is now quiet" in written


def test_the_change_is_recorded_in_every_file_so_each_explains_its_own_silence(client):
    """`access.log` simply stopping is indistinguishable from the app dying."""
    _setup_owner(client)
    _set_style(client, "quiet")

    for name in ("app.log", "access.log", "sql.log"):
        assert "-> quiet" in _read(client, name), name


def test_the_record_names_who_changed_it(client):
    _setup_owner(client)
    _set_style(client, "verbose")
    assert "by Jane.Doe@gmail.com" in _read(client, "app.log")


def test_turning_sql_on_says_that_the_file_now_holds_ledger_values(client):
    """Somebody flipping this deserves to be told in the file, not only in a form."""
    _setup_owner(client)
    _set_style(client, "sql")
    written = _read(client, "app.log")
    assert "SQL echo ON" in written
    assert "ledger values" in written


def test_turning_sql_off_is_recorded_too(client):
    _setup_owner(client)
    _set_style(client, "sql")
    _set_style(client, "normal")
    assert "SQL echo off" in _read(client, "sql.log")


def test_reselecting_the_same_style_says_it_changed_nothing(client):
    """Rather than a line claiming a transition that did not happen."""
    _setup_owner(client)
    _set_style(client, "normal")
    assert "reselected: normal" in _read(client, "app.log")


def test_a_start_says_which_style_is_in_force(client):
    """A file beginning mid-conversation at WARNING is otherwise ambiguous."""
    _setup_owner(client)
    assert "logging started at style normal" in _read(client, "app.log")


def test_the_mark_is_written_below_the_level_checks(client, tmp_path):
    """Not "it happens to be WARNING and quiet happens to be WARNING too".

    That is a fact about today's `STYLES` table, not a property. `mark` hands
    the record to each handler's `emit` directly, so a future style of
    ERROR-only would not silently start swallowing these.
    """
    _setup_owner(client)
    for handler in logging_setup.our_handlers():
        handler.setLevel(logging.CRITICAL)
    logging.getLogger().setLevel(logging.CRITICAL)

    logging_setup.mark("a line that every level check would have dropped")
    assert "every level check would have dropped" in _read(client, "app.log")


# --------------------------------------------------------------------------- #
# Timestamped
# --------------------------------------------------------------------------- #


def test_a_rotated_file_is_named_for_when_it_was_closed(client):
    _setup_owner(client)
    handler = next(
        one for one in logging_setup.our_handlers() if one.name.endswith(":app")
    )
    handler.doRollover()

    rotated = sorted(
        one.name for one in logging_setup.log_dir().glob("app-*.log")
    )
    assert len(rotated) == 1, rotated
    # app-YYYYmmdd-HHMMSS.log, which sorts in time order because of the format.
    stem = rotated[0].removeprefix("app-").removesuffix(".log")
    assert len(stem) == len("20260923-131545"), rotated
    assert stem[8] == "-" and stem.replace("-", "").isdigit(), rotated
    # And the live file is back, so the app goes on writing to one name.
    assert (logging_setup.log_dir() / "app.log").exists()


def test_rotations_are_pruned_to_the_bound_and_only_their_own_stream(client):
    """The disk bound is the property worth keeping from the stdlib handler.

    Its own pruning looks for `app.log.1`, which these names are not, so it had
    to be reimplemented -- and a prune that matched too widely would take
    `access-*.log` with it.
    """
    _setup_owner(client)
    handler = next(
        one for one in logging_setup.our_handlers() if one.name.endswith(":app")
    )
    directory = logging_setup.log_dir()
    (directory / "access-20200101-000000.log").write_text("not mine")

    for n in range(logging_setup.KEEP + 3):
        # Distinct names, since the stamp is per second and this loop is not.
        handler.doRollover()
        for one in sorted(directory.glob("app-*.log")):
            if one.stat().st_size == 0:
                one.rename(one.with_name(f"app-2026010{n}-00000{n}.log"))

    assert len(list(directory.glob("app-*.log"))) <= logging_setup.KEEP
    assert (directory / "access-20200101-000000.log").exists(), "pruned another stream"


def test_every_line_carries_milliseconds_and_an_offset(client):
    """`%(asctime)s` alone is ambiguous the moment the file is read elsewhere."""
    _setup_owner(client)
    import re

    first = next(
        line for line in _read(client, "app.log").splitlines() if " INFO " in line or " WARNING" in line
    )
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{4} ", first), first


# --------------------------------------------------------------------------- #
# The console, which is what `docker logs` reads
# --------------------------------------------------------------------------- #


def test_the_application_own_output_reaches_the_console(client, capsys):
    """Reported from a real Docker install: the setup token was nowhere.

    The app's own logger wrote to a file and to nothing else; uvicorn's wrote
    to the terminal and to nothing else. So `docker compose logs` showed the
    server starting and **not** the line naming the one-time setup token --
    which is the only line a first run exists to produce, and which the setup
    screen tells you to go and look for in "the server log".
    """
    _setup_owner(client)
    capsys.readouterr()

    logging.getLogger("spendtracker").warning("a line a container operator must see")

    assert "a line a container operator must see" in capsys.readouterr().err


def test_the_ledger_never_reaches_the_console_even_at_the_sql_style(client, capsys):
    """Container logs get shipped to places a ledger should not go.

    `sqlalchemy.engine` is claimed by `sql.log` with `propagate = False`, so it
    cannot reach the root logger and therefore cannot reach the console. This
    asserts the consequence rather than the mechanism.
    """
    _setup_owner(client)
    _set_style(client, "sql")
    capsys.readouterr()

    house = client.post("/api/households", json={"name": "Loud"}, headers=HEADERS)
    assert house.status_code == 201, house.text

    printed = capsys.readouterr()
    assert "INSERT INTO households" not in printed.out + printed.err
    # ...and it is still in its own file, so nothing was lost by keeping it out.
    assert "INSERT INTO households" in _read(client, "sql.log")


def test_the_sql_style_gives_sqlalchemy_no_console_of_its_own(client):
    """#108: what made the test above order-dependent was a real leak.

    `engine.echo = True` has SQLAlchemy attach a `StreamHandler(sys.stdout)`
    to `sqlalchemy.engine.Engine` when that logger has none, and that handler
    sits below the `propagate = False` meant to keep the ledger off the
    console. Run alone the test above saw the INSERT on stdout; after another
    test it passed only because that test's stale handler was already there.
    So: no handler on the Engine logger at the `sql` style, and one put there
    the way `create_engine(echo=True)` does is taken off.
    """
    import sys

    from app.db import engine

    _setup_owner(client)
    sql_engine = logging.getLogger("sqlalchemy.engine.Engine")
    stray = logging.StreamHandler(sys.stdout)
    sql_engine.addHandler(stray)
    try:
        _set_style(client, "sql")
        assert stray not in sql_engine.handlers
        assert sql_engine.handlers == []
        assert engine.echo is False
        assert logging.getLogger("sqlalchemy.engine").isEnabledFor(logging.INFO)

        house = client.post("/api/households", json={"name": "Quiet"}, headers=HEADERS)
        assert house.status_code == 201, house.text
        assert sql_engine.handlers == [], "and running a statement does not add one back"
        assert "'Quiet'" in _read(client, "sql.log")
    finally:
        sql_engine.removeHandler(stray)
        _set_style(client, "normal")


def test_a_style_change_is_visible_in_the_console_too(client, capsys):
    """So `docker logs` explains its own change of volume, like the files do."""
    _setup_owner(client)
    capsys.readouterr()
    _set_style(client, "quiet")
    assert "-> quiet" in capsys.readouterr().err


def test_the_console_follows_stderr_when_something_rebinds_it(client):
    """The handler must not hold the stream it was built with.

    The root logger is process-wide and this handler is attached once, so a
    captured stream outlives whatever rebound `sys.stderr` and then writes
    where nobody is reading. Under pytest that means the first test's buffer
    for the whole run; in a process it means a supervisor handing over a new
    pipe.
    """
    import io
    import sys

    _setup_owner(client)
    console = next(
        one for one in logging_setup.our_handlers() if one.name.endswith(":console")
    )

    caught = io.StringIO()
    was = sys.stderr
    sys.stderr = caught
    try:
        console.emit(
            logging.LogRecord("spendtracker", logging.WARNING, __file__, 0, "rebound", (), None)
        )
    finally:
        sys.stderr = was

    assert "rebound" in caught.getvalue()


# --------------------------------------------------------------------------- #
# What access.log may not say. Issue #93.
# --------------------------------------------------------------------------- #


def _request_line(path: str, status: int = 200) -> None:
    """One line, exactly the way uvicorn's h11 and httptools protocols write it."""
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "192.168.1.20:51234", "GET", path, "1.1", status
    )


def test_invitation_tokens_and_search_text_never_reach_access_log(client):
    """`access.log` is the file described as safe to send somebody.

    The token is a bearer credential for 72 hours, and `?search=` on the
    register is payee names and memos -- the ledger, in the file that promised
    not to hold it.
    """
    _setup_owner(client)
    token = "Zq9xTokenThatJoinsTheHousehold-_4f"
    _request_line(f"/invite/{token}")
    _request_line(f"/api/invite/{token}")
    _request_line(f"/api/invite/{token}?utm=mail")
    _request_line("/api/households/h1/transactions?search=FALAFEL%20BARBARA&limit=50")

    written = _read(client, "access.log")
    assert token not in written
    assert "FALAFEL" not in written and "search=" not in written
    # And the line is still a useful line: the route and the status survive.
    assert '"GET /invite/<token> HTTP/1.1" 200' in written
    assert '"GET /api/invite/<token> HTTP/1.1" 200' in written
    assert '"GET /api/households/h1/transactions HTTP/1.1" 200' in written


def test_the_invitation_routes_that_carry_no_token_are_left_alone(client):
    _setup_owner(client)
    for step in ("begin", "enrol", "complete"):
        _request_line(f"/api/invite/{step}")
    written = _read(client, "access.log")
    for step in ("begin", "enrol", "complete"):
        assert f"/api/invite/{step} HTTP" in written


def test_a_redacted_line_still_formats_on_uvicorns_own_console(client):
    """The line uvicorn prints is the cleaned one, and it is printed at all.

    The filter used to hand back a finished message with `args = ()`. The file
    handler never noticed; uvicorn's console handler formats with
    `AccessFormatter`, which unpacks exactly five args -- so every request with
    a query string printed a `--- Logging error ---` traceback in place of its
    line. Seen on `GET /api/households/<id>/receipts?...` in the e2e server.

    Formatted here with uvicorn's own formatter and its own default format, so
    this is the line a terminal or `docker logs` actually shows.
    """
    import io

    from uvicorn.config import LOGGING_CONFIG
    from uvicorn.logging import AccessFormatter

    _setup_owner(client)
    shown = io.StringIO()
    console = logging.StreamHandler(shown)
    console.setFormatter(
        AccessFormatter(LOGGING_CONFIG["formatters"]["access"]["fmt"], use_colors=False)
    )
    # Raise rather than print "--- Logging error ---" and carry on, which is
    # what made this invisible to every test that only read access.log.
    was = logging.raiseExceptions
    logging.raiseExceptions = True
    access = logging.getLogger("uvicorn.access")
    access.addHandler(console)
    try:
        _request_line("/api/households/h1/receipts?limit=50&search=FALAFEL")
        _request_line("/api/invite/Zq9xTokenThatJoinsTheHousehold-_4f?utm=mail", 404)
        _request_line("/api/households/h2/receipts")
    finally:
        access.removeHandler(console)
        logging.raiseExceptions = was

    lines = shown.getvalue().splitlines()
    assert lines == [
        'INFO:     192.168.1.20:51234 - "GET /api/households/h1/receipts HTTP/1.1" 200 OK',
        'INFO:     192.168.1.20:51234 - "GET /api/invite/<token> HTTP/1.1" 404 Not Found',
        'INFO:     192.168.1.20:51234 - "GET /api/households/h2/receipts HTTP/1.1" 200 OK',
    ]

    # And the file says the same, redacted, for all three.
    written = _read(client, "access.log")
    assert '"GET /api/households/h1/receipts HTTP/1.1" 200' in written
    assert '"GET /api/households/h2/receipts HTTP/1.1" 200' in written
    assert "FALAFEL" not in written and "Zq9x" not in written
