"""reconciliations

One row per act of proving an account against a statement. It keeps the one
figure that cannot be recomputed -- what the bank said the closing balance was
-- and points at the batch that locked the rows, so undoing a reconciliation is
the same button as undoing anything else.

Reversible: lossy -- every reconciliation ever done.

Revision ID: 89742b01b2cc
Revises: e1f3a77c04b2
Create Date: 2026-09-19 01:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "89742b01b2cc"
down_revision: str | Sequence[str] | None = "e1f3a77c04b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconciliations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=32), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("statement_balance", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"],
            name=op.f("fk_reconciliations_account_id_accounts"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["batches.id"],
            name=op.f("fk_reconciliations_batch_id_batches"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliations")),
    )
    with op.batch_alter_table("reconciliations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_reconciliations_account_date", ["account_id", "statement_date"], unique=False
        )
        batch_op.create_index(
            op.f("ix_reconciliations_account_id"), ["account_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("reconciliations", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_reconciliations_account_id"))
        batch_op.drop_index("ix_reconciliations_account_date")
    op.drop_table("reconciliations")
