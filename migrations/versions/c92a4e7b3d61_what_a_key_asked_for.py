"""what a key asked for, which is also how often it asked

The audit log records writes, so a read-only key left no trace beyond
`last_used_at` and "what did that key do last Tuesday" had no answer. This is
that answer, and it is the same table the rate limit counts, because the record
of what a key did IS the count of what it did recently.

Not audited, like `login_attempts`: it records what happened at the door rather
than what happened to the ledger, and auditing it would make every agent GET
demand a batch -- the exact cost `agent_keys.last_used_at` is redacted to
avoid. So it is in EXPECTED_EXCLUDED, in PURGE for the /db snapshot, and swept
at thirty days.

`agent_key_id` is deliberately not a foreign key. Keys are swept thirty days
after revocation and this log outlives them: RESTRICT would block that sweep,
and CASCADE would erase the record of what a key did at the moment somebody
most wants to read it. `login_attempts.email_canonical` is unconstrained for
the same reason.

`route` holds the template and never the URL. The real path carries ids, and a
log swept on a window should not be accumulating them.

Reversible: lossy -- the record of what each agent key asked for.

Revision ID: c92a4e7b3d61
Revises: b7f3d94a15c8
Create Date: 2026-09-20 13:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c92a4e7b3d61"
down_revision: str | Sequence[str] | None = "b7f3d94a15c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_requests",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("agent_key_id", sa.String(length=32), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("route", sa.String(length=120), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=True),
        sa.Column("batch_id", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_requests")),
    )
    op.create_index(op.f("ix_agent_requests_agent_key_id"), "agent_requests", ["agent_key_id"])
    op.create_index(op.f("ix_agent_requests_at"), "agent_requests", ["at"])
    op.create_index(op.f("ix_agent_requests_batch_id"), "agent_requests", ["batch_id"])
    op.create_index("ix_agent_requests_key_at", "agent_requests", ["agent_key_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_agent_requests_key_at", table_name="agent_requests")
    op.drop_index(op.f("ix_agent_requests_batch_id"), table_name="agent_requests")
    op.drop_index(op.f("ix_agent_requests_at"), table_name="agent_requests")
    op.drop_index(op.f("ix_agent_requests_agent_key_id"), table_name="agent_requests")
    op.drop_table("agent_requests")
