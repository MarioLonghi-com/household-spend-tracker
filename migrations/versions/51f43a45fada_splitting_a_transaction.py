"""splitting a transaction

A grouping, not a parent. The parts of one split share this id; the row they
came from is gone, and the batch that split it holds both the before-image and
the undo.

Reversible: lossy -- which transactions were the parts of one split. The rows survive; the grouping does not.

Revision ID: 51f43a45fada
Revises: 8061e4b19c7e
Create Date: 2026-09-19 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "51f43a45fada"
down_revision: str | Sequence[str] | None = "8061e4b19c7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("split_id", sa.String(length=32), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_transactions_split_id"), ["split_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_transactions_split_id"))
        batch_op.drop_column("split_id")
