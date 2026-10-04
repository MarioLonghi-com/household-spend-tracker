"""Alembic environment.

``render_as_batch`` is not optional on SQLite: it cannot ALTER a constraint, so
Alembic recreates the table instead -- which it can only do when every
constraint has a deterministic name. That is what the naming convention on
``Base.metadata`` is for.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.models import Base
from app.models.base import EnumStr

config = context.config

# A url passed by the caller wins; otherwise take the app's own. Tests and the
# container entrypoint both supply one explicitly, and silently overriding it
# migrates a different database than the caller is looking at.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    # `disable_existing_loggers=False`: the default switches off every logger
    # that exists when this runs. A migration run in-process -- a test, or
    # anything that imports the app first -- then silenced the app's own
    # loggers for the rest of the process, and the log-stream tests failed
    # whenever one landed after it on the same xdist worker.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render EnumStr columns as the VARCHAR they actually are.

    Autogenerate would otherwise emit ``app.models.base.EnumStr(length=12)`` --
    which neither imports ``app`` nor carries the enum class, so the migration
    fails to import. Rendering the underlying type keeps migrations free of
    application imports, which is what lets an old revision still run after the
    enum it was written against has changed.
    """
    if type_ == "type" and isinstance(obj, EnumStr):
        autogen_context.imports.add("import sqlalchemy as sa")
        return f"sa.String(length={obj.length})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=render_item,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
