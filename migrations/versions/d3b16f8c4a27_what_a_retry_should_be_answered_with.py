"""what a retry should be answered with, so a timeout is not a double import

Agents retry. Without this, a timeout on a request that actually succeeded is
indistinguishable from one that failed, and the honest thing for the agent to
do -- try again -- is the thing that imports the rows twice.

The digest guard in `importing.previous_import_of` already catches the same
rows arriving twice and answers 409. That is the right answer to a mistake and
the wrong one to a retry: a retry should get the first reply, not an error
about itself. Two mechanisms, two questions.

Scoped by (key, header, route) with a unique constraint rather than a check in
a service, so it holds when two retries race rather than only when they arrive
politely in turn. `request_sha256` is there because a retry that changed the
body is not a retry, and answering it with the first result would silently
discard what it actually asked for.

Unaudited, like `agent_requests`: it records what happened at the door, and
auditing it would make replying to a retry demand a batch.

> [!important] If this migration and another both claim `c92a4e7b3d61`
> `feature/reports-income-expense` carries `f18b5c2a9e33` off the same parent.
> Two heads is not a merge conflict git will show you -- both files apply
> cleanly and `alembic upgrade head` then refuses. Whichever branch merges
> second re-points its `down_revision` at the other's revision id. Nothing else
> changes: these two touch different tables and neither reads the other's.

Reversible: clean -- the replay cache. A retry is answered afresh instead of from it.

Revision ID: d3b16f8c4a27
Revises: c92a4e7b3d61
Create Date: 2026-09-21 15:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3b16f8c4a27"
down_revision: str | Sequence[str] | None = "c92a4e7b3d61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_replays",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("agent_key_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("route", sa.String(length=120), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_replays")),
        sa.UniqueConstraint(
            "agent_key_id", "idempotency_key", "route", name="uq_agent_replays_key"
        ),
    )
    op.create_index(op.f("ix_agent_replays_agent_key_id"), "agent_replays", ["agent_key_id"])
    op.create_index(op.f("ix_agent_replays_at"), "agent_replays", ["at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_replays_at"), table_name="agent_replays")
    op.drop_index(op.f("ix_agent_replays_agent_key_id"), table_name="agent_replays")
    op.drop_table("agent_replays")
