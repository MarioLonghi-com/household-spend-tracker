"""`alembic upgrade head`, in this process (#318).

The upgrade drill used to run `[sys.executable, "-m", "alembic", "upgrade",
"head"]`. In a frozen desktop build `sys.executable` is the app itself, not a
Python, so that relaunched the app instead of migrating it. Alembic is a
library; `seed_demo` already called it in-process.

The configuration is built here rather than read from `alembic.ini`: with no
file, `migrations/env.py` leaves the process's logging alone (it calls
`fileConfig` only for a file), and the lines Alembic says are collected by a
handler of our own -- formatted as `alembic.ini` would have printed them, so a
drill's log reads as it did.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

#: Where the migrations are: beside `app/` in a checkout, an image and a frozen
#: build alike.
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

#: `alembic.ini`'s `formatter_generic`.
_FORMAT = "%(levelname)-5.5s [%(name)s] %(message)s"


class _Collect(logging.Handler):
    def __init__(self, say: Callable[[str], None]) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(logging.Formatter(_FORMAT))
        self._say = say

    def emit(self, record: logging.LogRecord) -> None:
        self._say(self.format(record))


def alembic_config(database_url: str | None = None):
    """Alembic's configuration, built here rather than read from `alembic.ini`.

    The one place that says where the migrations are: the frozen desktop build
    carries `migrations/` but not the ini file, and a file-based configuration
    would also have `env.py` reconfigure the process's logging.
    """
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    if database_url:
        # ConfigParser interpolation: a literal % in a path would be read as one.
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_to_head(*, database_url: str | None = None, say: Callable[[str], None] | None = None) -> None:
    """Migrate the ledger to this code's head. Raises whatever Alembic raises.

    `database_url` defaults to the app's own; `say` receives each line Alembic
    logs. Nothing goes to standard output, which the desktop app's backend
    keeps for its shell.
    """
    from alembic import command

    from . import config as app_config

    configured = alembic_config(database_url or app_config.settings.database_url)

    logger = logging.getLogger("alembic")
    handler = _Collect(say or (lambda _line: None))
    was = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        command.upgrade(configured, "head")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(was)
