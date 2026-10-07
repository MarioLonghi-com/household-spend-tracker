"""Where the log goes, and how much of it there is.

Until this existed there was no answer to "show me the logs": the app called
`logging.getLogger` in three places and configured nothing, so everything it
said went wherever uvicorn's own handlers happened to point -- a terminal that
is closed by the time anyone asks. A self-hosted instance somebody runs for
their household has no journal to read and no operator watching it, so the log
has to be a file, and the file has to be somewhere the owner can reach.

Two decisions worth writing down.

**It writes into `data_dir`, beside the database.** That directory is already
the one thing a deployment has to back up and the one thing that is not the
code, and it is per-worktree on a development machine -- so two checkouts do
not write into one log.

**The style is chosen at runtime and it persists.** A level in an environment
variable means the moment you want more detail is the moment you have to
restart the thing you were trying to observe, which loses whatever it was
doing. So the choice is a file in `data_dir` and a route on the admin page, and
a restart keeps it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import pathlib
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import config
from .permissions import private_dir


def _settings():
    """The settings object that is current *now*.

    `config.settings` read at call time, never `from .config import settings`.
    That form binds a *reference to the object*, and the suite rebuilds that
    object with `importlib.reload(app.config)` to point each test at its own
    temporary data directory. A module holding the old one goes on writing into
    the previous test's directory -- which does not fail, it quietly reads back
    somebody else's log, and a test asserting on a log file passes for the
    wrong reason. Every path below goes through here.
    """
    return config.settings


#: Handlers are identified by name rather than by a module flag: the `client`
#: fixture reloads `app.main` per test, and a flag would let a second handler
#: onto a root logger that already has one -- every line duplicated, quietly,
#: for the rest of the run.
#:
#: Not `spendtracker-file`, which is what this was called while the files were
#: all there was. The console is one of these now and is not a file.
HANDLER_PREFIX = "spendtracker-log"

#: The main file. Kept because "the" handler used to mean this one.
HANDLER_NAME = f"{HANDLER_PREFIX}:app"

#: The console, which is not one of the `STREAMS`: it carries what `app.log`
#: carries and nothing else.
CONSOLE_NAME = f"{HANDLER_PREFIX}:console"

#: Each file is rotated at 1 MiB and five are kept. The bound is the point: a
#: log that fills the disk takes the ledger with it. 1 MiB is a few hundred
#: thousand lines of this app's output.
MAX_BYTES = 1024 * 1024
KEEP = 5

#: Milliseconds and the UTC offset, so two files can be read side by side and
#: a line from a machine in another timezone still orders correctly against
#: one from here. The previous format had neither.
LOG_FORMAT = "%(asctime)s.%(msecs)03d%(tzoffset)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class _WithOffset(logging.Formatter):
    """`%(tzoffset)s`, because `%(asctime)s` has no way to carry one.

    `logging.Formatter` formats the time with `time.localtime` and offers no
    directive for the offset. Without it every timestamp in these files is
    ambiguous the moment the instance moves, the clocks change, or somebody
    reads the file somewhere else -- which is most of the reasons anybody is
    reading a log.
    """

    def format(self, record: logging.LogRecord) -> str:
        record.tzoffset = datetime.now().astimezone().strftime("%z")
        return super().format(record)


class ConsoleHandler(logging.StreamHandler):
    """stderr, resolved at emit time rather than captured at construction.

    `logging.StreamHandler()` stores whatever `sys.stderr` was when it was
    built. The root logger is process-wide and this handler is attached once,
    so a stored stream outlives anything that rebinds `sys.stderr` afterwards
    -- and then writes into a stream nobody is reading, silently, for the rest
    of the process.

    That is not only a test concern, though the tests are where it shows first:
    pytest rebinds `sys.stderr` per test, so the handler installed by the first
    test would write into the first test's buffer for the whole run. A process
    that reopens its own stderr -- a supervisor handing over a new pipe, a
    rotation of a redirected file -- has the same problem with no test to catch
    it.
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


class TimestampedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotate on size, but name the rotated file after the moment it was closed.

    The stdlib gives you one or the other. `RotatingFileHandler` bounds the
    disk and produces `app.log.1`, `app.log.2` -- names that say nothing and
    that *shift under you*, so the `.1` you were reading is `.2` a minute
    later and the line numbers you noted are in a different file.
    `TimedRotatingFileHandler` names files by time and has **no size bound at
    all**, which is the one property worth keeping: a log that fills the disk
    takes the ledger with it.

    So: size decides *when*, the clock decides *what it is called*, and pruning
    is done here because the stdlib's own pruning looks for the numbered names
    this does not produce.
    """

    def rotation_filename(self, default_name: str) -> str:
        # `default_name` is `<base>.1`, which is what the stdlib is about to
        # rename the live file to. The number is meaningless here.
        base = pathlib.Path(self.baseFilename)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return str(base.with_name(f"{base.stem}-{stamp}{base.suffix}"))

    def doRollover(self) -> None:  # noqa: N802 - the stdlib names it
        if self.stream:
            self.stream.close()
            self.stream = None
        # The stdlib's shuffle loop is skipped entirely: there are no numbered
        # files to walk along, and its rename of `<base>.1` is the one this
        # needs -- redirected by `rotation_filename` above.
        self.rotate(self.baseFilename, self.rotation_filename(self.baseFilename + ".1"))
        self._prune()
        if not self.delay:
            self.stream = self._open()

    def _prune(self) -> None:
        """Keep the newest `backupCount` rotations of *this* file.

        Matched on the stem so `app-*.log` never reaches `access-*.log`. The
        names sort lexicographically in time order because the stamp is
        `%Y%m%d-%H%M%S`, which is the whole reason for that format.
        """
        if self.backupCount <= 0:
            return
        base = pathlib.Path(self.baseFilename)
        rotated = sorted(base.parent.glob(f"{base.stem}-*{base.suffix}"))
        for old in rotated[: -self.backupCount] if len(rotated) > self.backupCount else []:
            with contextlib.suppress(OSError):
                old.unlink()


class RedactRequestLines(logging.Filter):
    """What `access.log` may not say: an invitation or reset token, or a query string.

    `access.log` is described as safe to share, and it was not. Two things
    reached it verbatim:

    - **`/invite/<token>` and `/api/invite/<token>`.** The token is a bearer
      credential for 72 hours -- whoever holds it can join the household --
      and `Referrer-Policy: no-referrer` exists precisely so it cannot leak
      out of the address bar. Writing it into a file somebody sends for help
      undid that.
    - **Every query string**, which on the register is `?search=<text>`: payee
      names and memos, i.e. the ledger, in the one file that promised not to
      hold it.

    On the logger, not a handler, so every handler -- the file, and uvicorn's
    own console handler -- gets the cleaned line.

    **Each argument is cleaned where it stands, and the record keeps its
    shape.** It used to format the line, clean it, and put it back as a
    finished message with `args = ()`. The file never minded; uvicorn's
    console handler did. Its `AccessFormatter` unpacks exactly five args, so
    every request carrying a query string -- the receipts list, the register
    -- printed a `--- Logging error ---` traceback instead of its line. Still
    independent of *which* argument holds the path: every string argument is
    cleaned, and a status code or an address has nothing either pattern
    matches. A record with no args is cleaned as a message, as before.
    """

    #: Not `begin`, `enrol` or `complete`, which are the invitation's own
    #: routes and carry no token.
    _INVITE = re.compile(r"(/(?:api/)?invite/)(?!(?:begin|enrol|complete)(?:[/?\s\"]|$))[^/?\s\"]+")
    #: Every `/reset/...` path carries the token: the page, its lookup, and
    #: both of its writes (#284). A reset link is a way into an account that
    #: has been shut, so it is worth more than an invitation, not less.
    _RESET = re.compile(r"(/(?:api/)?reset/)[^/?\s\"]+")
    _QUERY = re.compile(r"\?[^\s\"]*")

    def _clean(self, text: str) -> str:
        text = self._RESET.sub(r"\1<token>", self._INVITE.sub(r"\1<token>", text))
        return self._QUERY.sub("", text)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(
                self._clean(one) if isinstance(one, str) else one for one in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._clean(record.msg)
        return True


@dataclass(frozen=True, slots=True)
class Stream:
    """One log file, and which loggers write into it.

    Separated because the three answer different questions and one of them is
    dangerous to hand over:

    - **app** is what the instance did. This is the one you read, and the one
      you would send somebody.
    - **access** is one line per HTTP request. Mechanical, high-volume, and it
      drowns everything else when they share a file.
    - **sql** is only written at the `sql` style, and it contains **the ledger
      in plain text** -- every statement and every row it returned, so
      transactions, payees and amounts. Keeping it in its own file is what
      makes "send me your log" a safe thing to ask.
    """

    key: str
    filename: str
    label: str
    #: Loggers routed here. Empty means the root, which catches everything that
    #: is not claimed by one of the others.
    loggers: tuple[str, ...]
    blurb: str


STREAMS: tuple[Stream, ...] = (
    Stream(
        key="app",
        filename="app.log",
        label="Application",
        loggers=(),
        blurb="What the instance did: startups, the schema revision, housekeeping, errors.",
    ),
    Stream(
        key="access",
        filename="access.log",
        label="Requests",
        loggers=("uvicorn.access",),
        blurb="One line per HTTP request. Mechanical, and it drowns everything else.",
    ),
    Stream(
        key="sql",
        filename="sql.log",
        label="SQL",
        loggers=("sqlalchemy.engine",),
        blurb=(
            "Only written at the “Verbose, with SQL” style, and it holds the ledger "
            "in plain text. Its own file so the other two stay safe to share."
        ),
    ),
)

BY_STREAM = {stream.key: stream for stream in STREAMS}


@dataclass(frozen=True, slots=True)
class Style:
    """One logging style, as the admin screen offers it."""

    key: str
    label: str
    level: int
    #: Whether SQLAlchemy echoes every statement. Its own flag rather than a
    #: level, because it is a different question: `sqlalchemy.engine` at DEBUG
    #: prints every query *and* every row it got back, which is the ledger
    #: going into a file. Only `sql` turns it on, and only deliberately.
    echo_sql: bool
    blurb: str


STYLES: tuple[Style, ...] = (
    Style(
        key="quiet",
        label="Quiet",
        level=logging.WARNING,
        echo_sql=False,
        blurb="Only what went wrong. A healthy instance writes almost nothing.",
    ),
    Style(
        key="normal",
        label="Normal",
        level=logging.INFO,
        echo_sql=False,
        blurb="What the instance did: startups, the schema revision, housekeeping sweeps.",
    ),
    Style(
        key="verbose",
        label="Verbose",
        level=logging.DEBUG,
        echo_sql=False,
        blurb="Everything the app and its libraries have to say. For chasing something specific.",
    ),
    Style(
        key="sql",
        label="Verbose, with SQL",
        level=logging.DEBUG,
        echo_sql=True,
        blurb=(
            "Verbose, plus every statement and every row it returned — which means "
            "transactions, payees and amounts written into a file in plain text. "
            "Turn it on to diagnose something and turn it off afterwards."
        ),
    ),
)

BY_KEY = {style.key: style for style in STYLES}
DEFAULT = "normal"

#: Loggers whose level this moves. The root would be enough for the app's own
#: output; uvicorn installs its own and sets levels on them, so they are named.
_GOVERNED = ("", "spendtracker", "uvicorn", "uvicorn.error", "uvicorn.access")

log = logging.getLogger("spendtracker")


def log_dir() -> Path:
    return _settings().data_dir / "logs"


def log_file(stream: str = "app") -> Path:
    """The live file for one stream. `app` unless asked otherwise.

    The default keeps every existing caller correct: before the split there
    was one file and this returned it.
    """
    return log_dir() / BY_STREAM[stream].filename


def _choice_file() -> Path:
    return _settings().data_dir / "logging.json"


def current() -> Style:
    """The style in force, from the file, defaulting rather than failing.

    A corrupt or hand-edited choice file falls back to `normal` instead of
    refusing to boot. Logging is not load-bearing; an instance that will not
    start because it could not parse its own logging preference is worse than
    one that logs at the wrong level.
    """
    try:
        raw = json.loads(_choice_file().read_text())
        return BY_KEY.get(str(raw.get("style")), BY_KEY[DEFAULT])
    except (OSError, ValueError):
        return BY_KEY[DEFAULT]


def our_handlers() -> list[logging.Handler]:
    """Every handler this module owns -- the three files and the console."""
    seen: dict[int, logging.Handler] = {}
    for name in ("", *(one for stream in STREAMS for one in stream.loggers)):
        for handler in logging.getLogger(name).handlers:
            if (handler.name or "").startswith(HANDLER_PREFIX):
                seen[id(handler)] = handler
    return list(seen.values())


def mark(message: str) -> None:
    """Write one line into **every** log file, whatever the level is set to.

    The ask this exists for: *when the log state changes, it has to show in the
    file.* The obvious version -- `log.info("logging style is now quiet")` --
    is exactly wrong, because switching to `quiet` raises the level to WARNING
    first and the INFO line is then dropped. **Turning logging down left no
    record that it had been turned down**, which is the one transition somebody
    would go looking for afterwards.

    Logging it at WARNING would work today, since `quiet` is the highest floor
    any style sets. That is a fact about the current `STYLES` table rather than
    a property, so this does not rely on it: the record is handed to each
    handler's `emit` directly, which is below the level checks on both the
    logger and the handler.

    It goes into *every* stream because each file needs to explain its own
    silence. `access.log` simply stopping is indistinguishable from the
    instance dying; `access.log` stopping one line after "style changed:
    normal -> quiet" is not.
    """
    record = logging.LogRecord(
        name="spendtracker.logging",
        level=logging.WARNING,
        pathname=__file__,
        lineno=0,
        msg=message,
        args=(),
        exc_info=None,
    )
    for handler in our_handlers():
        with contextlib.suppress(Exception):
            handler.emit(record)


def apply(style: Style) -> None:
    """Point every governed logger at this style's level, and the engine at its echo."""
    for name in _GOVERNED:
        logging.getLogger(name).setLevel(style.level)
    # Each handler has its own level so that raising a logger's level cannot be
    # undone by a handler still set to WARNING from a previous style.
    for handler in our_handlers():
        handler.setLevel(style.level)

    # Imported here, not at module scope: `app.db` imports `app.config` and the
    # audit guard, and a top-level import would put this module in that chain
    # for anything that only wanted to read the current style.
    from .db import engine

    # By level, never by `engine.echo`. Setting `echo = True` makes SQLAlchemy
    # attach its own `StreamHandler(sys.stdout)` to `sqlalchemy.engine.Engine`
    # whenever that logger has no handler -- below the `propagate = False` on
    # `sqlalchemy.engine` that keeps the ledger out of the console -- so the
    # `sql` style printed every statement and its values to stdout, which is
    # `docker logs` (#108: the "order-dependent" test that caught it passed
    # only when an earlier test's handler was already there). At INFO the
    # statements reach `sql.log` without it. A handler SQLAlchemy added anyway
    # -- `SPENDTRACKER_ECHO_SQL` at `create_engine` -- is taken back off.
    engine.echo = False
    sql_engine = logging.getLogger("sqlalchemy.engine.Engine")
    ours = {id(handler) for handler in our_handlers()}
    for handler in list(sql_engine.handlers):
        if id(handler) not in ours:
            sql_engine.removeHandler(handler)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if style.echo_sql else logging.WARNING
    )


def choose(key: str, *, by: str | None = None) -> Style:
    """Set the style and remember it. Raises `KeyError` for a name that is not one.

    The change is written into every log file, before and after, by `mark` --
    see there for why it is not simply logged.
    """
    was = current()
    style = BY_KEY[key]
    who = f" by {by}" if by else ""

    if style.key == was.key:
        mark(f"logging style reselected: {style.key}{who} (unchanged)")
        apply(style)
        return style

    # Before, so the file being written at the *old* level carries the reason
    # its next line looks different -- and after, so a file that was silent at
    # the old level still says what happened.
    mark(f"logging style changing: {was.key} -> {style.key}{who}")
    _choice_file().parent.mkdir(parents=True, exist_ok=True)
    _choice_file().write_text(json.dumps({"style": style.key}, indent=2) + "\n")
    apply(style)
    mark(
        f"logging style is now {style.key} ({style.label}){who}"
        + ("; SQL echo ON -- sql.log now holds ledger values" if style.echo_sql else "")
        + ("" if style.echo_sql or not was.echo_sql else "; SQL echo off")
    )
    return style


def configure() -> Style:
    """Attach one file handler per stream and apply the stored style.

    Idempotent, and it has to be: `lifespan` runs once per process in
    production and once per test in the suite, against a root logger that is
    process-wide and shared between them.
    """
    directory = log_dir()
    # 0700, tightened if it already existed: `app.log` carries the setup token
    # on a first boot, and `sql.log` the ledger.
    private_dir(directory)
    fresh = False

    for stream in STREAMS:
        wanted = str(directory / stream.filename)
        name = f"{HANDLER_PREFIX}:{stream.key}"
        # A handler is reattached when the data directory has moved under it,
        # not merely when there is none. The suite reloads `app.main` per test
        # against a fresh `tmp_path`, and a handler left pointing at the
        # previous test's directory keeps writing perfectly happily into a
        # deleted file -- so the logs it is supposed to be testing are
        # somewhere nobody can read them.
        targets = [logging.getLogger(one) for one in (stream.loggers or ("",))]
        for logger in targets:
            for handler in list(logger.handlers):
                if handler.name == name and getattr(handler, "baseFilename", None) != wanted:
                    logger.removeHandler(handler)
                    handler.close()

        if any(handler.name == name for logger in targets for handler in logger.handlers):
            continue

        fresh = True
        handler = TimestampedRotatingFileHandler(
            wanted, maxBytes=MAX_BYTES, backupCount=KEEP, encoding="utf-8"
        )
        handler.name = name
        handler.setFormatter(_WithOffset(LOG_FORMAT, datefmt=DATE_FORMAT))
        for logger in targets:
            logger.addHandler(handler)
            if stream.loggers:
                # Claimed, so it does not also land in app.log. Without this,
                # separating the streams would only *duplicate* them -- and the
                # one that must not be duplicated is sql.log, which is the
                # ledger in plain text.
                logger.propagate = False

    # Before anything is written to `access.log`, and on every configure,
    # because the logger outlives the handlers the suite keeps replacing.
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(one, RedactRequestLines) for one in access.filters):
        access.addFilter(RedactRequestLines())

    # **And the console.** Until this existed the application's own output went
    # only to a file, and uvicorn's went only to the terminal -- so `docker
    # compose logs` showed the server starting and *not* the line telling you
    # the setup token, which is the one line a first run exists to produce. The
    # setup screen said "printed to the server log", and in a container the
    # server log is `docker logs`, where it was not.
    #
    # On the root logger, so it carries what `app.log` carries and nothing
    # else: `uvicorn.access` and `sqlalchemy.engine` are claimed by their own
    # streams with `propagate = False`, which means the ledger cannot reach the
    # console even at the `sql` style. That is deliberate -- container logs get
    # shipped to places a ledger should not go.
    #
    # stderr rather than stdout, so a command whose output is being piped
    # somewhere is not interleaved with logging. uvicorn's own default handler
    # does the same.
    root = logging.getLogger()
    if not any(handler.name == CONSOLE_NAME for handler in root.handlers):
        console = ConsoleHandler()
        console.name = CONSOLE_NAME
        console.setFormatter(_WithOffset(LOG_FORMAT, datefmt=DATE_FORMAT))
        root.addHandler(console)
        fresh = True

    style = current()
    apply(style)
    if fresh:
        # Every start says which style is in force. A file that begins
        # mid-conversation at WARNING is otherwise indistinguishable from one
        # whose instance had nothing to say.
        mark(f"logging started at style {style.key} ({style.label}); files in {directory}")
    return style


def reset() -> None:
    """Detach every handler, console included. For a test that has finished with
    a temporary data dir."""
    for name in ("", *(one for stream in STREAMS for one in stream.loggers)):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if (handler.name or "").startswith(HANDLER_PREFIX):
                logger.removeHandler(handler)
                handler.close()
        if name:
            # Put propagation back, or a later test with no handler attached
            # loses those loggers' output entirely.
            logger.propagate = True


@dataclass(frozen=True, slots=True)
class LogFile:
    name: str
    bytes: int
    modified: datetime


def files() -> list[LogFile]:
    """Every log file this instance has, newest first.

    The rotation suffixes (`.1`, `.2`) are files in their own right and are
    listed: "dump all the log files available" means the rotated ones too, and
    they are precisely where yesterday's problem is.
    """
    directory = log_dir()
    if not directory.is_dir():
        return []
    found = [
        LogFile(
            name=path.name,
            bytes=path.stat().st_size,
            modified=datetime.fromtimestamp(path.stat().st_mtime, UTC),
        )
        for path in directory.iterdir()
        if path.is_file()
    ]
    return sorted(found, key=lambda one: (one.modified, one.name), reverse=True)


#: How much of one file a read hands back. A log is read to see what just
#: happened, and an unbounded read of a rotated megabyte into a JSON response
#: is a way to make the browser the thing that falls over.
TAIL_BYTES = 256 * 1024


def tail(name: str, *, limit: int = TAIL_BYTES) -> str:
    """The end of one log file.

    `name` is matched against the listing rather than joined onto a path. The
    difference matters: `os.path.join(dir, "../../etc/passwd")` is a path this
    process can open, and a containment check after the join is the version of
    this that has been got wrong in public more than once. A name that is not
    in the listing does not exist, and there is nothing to normalise.
    """
    if name not in {one.name for one in files()}:
        raise FileNotFoundError(name)
    path = log_dir() / name
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > limit:
            handle.seek(size - limit)
        raw = handle.read()
    text = raw.decode("utf-8", errors="replace")
    if size > limit:
        # Dropped mid-line by the seek, so the first partial line goes with it
        # rather than being served as though it were a record.
        _, _, text = text.partition("\n")
        text = f"… the first {size - limit:,} bytes of this file are not shown …\n{text}"
    return text
