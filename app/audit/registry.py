"""Which tables are in the audit log, and which are deliberately out.

There is no default. ``Base.__init_subclass__`` refuses to create a model that
has not declared ``__audit__``, so a new table fails at *import* rather than
quietly inheriting someone else's decision. The previous build's equivalent
drifted to five tables stamped for a sync they never joined, and nothing noticed.
"""

from __future__ import annotations

from typing import Any

from ..models import Base

#: The tables that must stay out. Named here so the test that pins them has
#: something to compare against; the declaration itself lives on each model.
#: Tables deliberately outside the log. The first three *are* the log; the rest
#: record attempts at the door rather than changes to the ledger, so auditing
#: them would mean every sign-in demanded a batch.
EXPECTED_EXCLUDED: frozenset[str] = frozenset(
    {
        "batches",
        "changes",
        "import_lines",
        "sessions",
        "pending_sign_ins",
        # A spent proof of identity, single-use and swept. Out for the same
        # reason `pending_sign_ins` is: it records what happened at the door.
        "step_up_grants",
        "login_attempts",
        # What a key asked for. Out for the reason `login_attempts` is: it
        # records what happened at the door, not what happened to the ledger,
        # and auditing it would make every agent GET demand a batch.
        "agent_requests",
        # What a retry should be answered with. Out for the same reason:
        # auditing it would make replying to a retry demand a batch.
        "agent_replays",
        "trusted_devices",
        "instance",
        # Content-addressed receipt bytes. Out of the log because the row has no
        # history worth keeping -- it is inserted once, keyed by what it
        # contains, and deleted only when nothing references it -- and because
        # `snapshot.images()` writes complete rows and base64s `bytes`, so a
        # 175 KiB receipt would cost 233 KiB of log on insert and twice that on
        # every update. The audited half is `receipts`, which is what people
        # actually change.
        "receipt_blobs",
    }
)


def audited_models() -> dict[str, type]:
    """Table name -> model class, for everything in the log."""
    return {
        mapper.class_.__tablename__: mapper.class_
        for mapper in Base.registry.mappers
        if mapper.class_.__audit__
    }


def excluded_tables() -> frozenset[str]:
    return frozenset(
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if not mapper.class_.__audit__
    )


def is_audited(obj: Any) -> bool:
    return bool(getattr(type(obj), "__audit__", False))


def redacted_columns(model: type) -> frozenset[str]:
    return getattr(model, "__audit_redact__", frozenset())
