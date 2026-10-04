"""history says which key acted, beside the person whose authority it borrowed

An agent is not a user and never becomes one. `batches.actor_id` stays the
human, because that is whose authority was used; `agent_key_id` says which key
used it. A History row reads "Jane Doe · via Claude (receipt filer)" rather
than replacing one name with the other.

SET NULL rather than RESTRICT, unlike `actor_id`. A user is never deleted while
their history stands, so RESTRICT is right there. A key is revoked and swept on
a thirty-day window, and History must keep reading correctly afterwards -- so
the foreign key is the live link and `source["agent"]` carries a denormalised
copy of the name. "via a key since removed" is a worse sentence than "via
Claude (receipt filer)", and the audit log's whole premise is that it stays
readable.

There is deliberately NO `BatchKind.agent`. BatchKind answers *what was done* --
import, bulk edit, reconcile, split. *Who did it* is a different axis, and a
new kind would mean an agent import and a human import sorting into different
buckets, so every query asking "show me the imports" would have to know about
both. An agent import is `BatchKind.imported` with `agent_key_id` set.

Reversible: lossy -- which agent key made each change. History keeps the person; it loses the key.

Revision ID: f5a2c8e04b19
Revises: e7c04a91d8b2
Create Date: 2026-09-21 17:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5a2c8e04b19"
down_revision: str | Sequence[str] | None = "e7c04a91d8b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("batches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("agent_key_id", sa.String(length=32), nullable=True))
        batch_op.create_index(op.f("ix_batches_agent_key_id"), ["agent_key_id"])
        batch_op.create_foreign_key(
            op.f("fk_batches_agent_key_id_agent_keys"),
            "agent_keys", ["agent_key_id"], ["id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("batches", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("fk_batches_agent_key_id_agent_keys"), type_="foreignkey")
        batch_op.drop_index(op.f("ix_batches_agent_key_id"))
        batch_op.drop_column("agent_key_id")
