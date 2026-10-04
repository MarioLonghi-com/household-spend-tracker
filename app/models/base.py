"""Declarative base, id/timestamp helpers, and the three global mapper policies
the audit log depends on.

Each of the three exists because of a verified failure mode, and each is
documented where it sits: primary keys must exist before flush, every audited
column must fetch its old value on set, and every table must declare whether it
is audited.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import DateTime, MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

#: SQLite cannot ALTER a constraint, so Alembic recreates the table instead --
#: and it can only do that when every constraint has a deterministic name.
#: Without this, migrations work until the first constraint change and then stop.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Naive UTC, because the columns are plain ``DateTime``."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    #: Every table says whether its rows go in the audit log. There is no
    #: default: a new table that forgets to decide fails at import, which is
    #: earlier and louder than failing in a test.
    __audit__: ClassVar[bool]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # Checked *before* the declarative machinery runs, so a model that
        # forgets to decide is never mapped at all -- otherwise the rejected
        # class stays in the registry and poisons everything that reads it.
        declares_table = not cls.__dict__.get("__abstract__") and "__tablename__" in cls.__dict__
        if declares_table and "__audit__" not in cls.__dict__:
            raise TypeError(
                f"{cls.__name__} must declare __audit__ = True or False. "
                "Every table is either in the audit log or deliberately out of it."
            )
        super().__init_subclass__(**kwargs)


class UUIDPrimaryKey:
    """A 32-char hex id, assigned at construction rather than at flush.

    A ``mapped_column(default=new_id)`` default is evaluated by the persistence
    step, which runs *after* ``before_flush`` -- so the audit hook would read
    ``id`` as ``None`` and write a change row that points at nothing. The
    ``init`` event in ``audit.hook`` assigns it eagerly; the column default
    stays as the backstop for inserts that bypass the ORM constructor.
    """

    __uuid_pk__: ClassVar[bool] = True

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class EnumStr(TypeDecorator):
    """Store an enum by its *value* in a VARCHAR, and hand back the member.

    A plain ``String`` column round-trips as a bare string, which silently
    breaks ``account.type.is_liability`` -- it fails as an ``AttributeError``
    deep in a service rather than anywhere near the schema. Native DB enums are
    avoided because they make migrations painful.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type, length: int = 32) -> None:
        self.enum_class = enum_class
        super().__init__(length)

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return self.enum_class(value).value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        return self.enum_class(value)
