"""a transaction can be a work expense, and can say what paid it back

Two nullable columns and nothing backfilled: no existing row is a work
expense, and nothing in the ledger could honestly guess one. NULL is the
ordinary state and stays the ordinary state.

`reimbursed_by_id` is the second foreign key `transactions` has to itself.
Creating it rebuilds the table -- SQLite cannot add a constraint in place --
exactly as `9dc3906a0961` did for `category_id`. The alternative, declaring the
key in the model and not in the migration, would leave a migrated database
different from a fresh one with no test to notice, since Alembic's compare does
not look at foreign keys.

Reversible: lossy -- downgrading drops every work-expense flag and every link to the payment that repaid it; the transactions themselves stay.

Revision ID: a4c7e19d2b86
Revises: ecc9b154764f
Create Date: 2026-09-25 18:55:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c7e19d2b86"
down_revision: str | Sequence[str] | None = "ecc9b154764f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `reimbursement` rendered as the sa.String it actually is, not as EnumStr:
    # a migration carries no application import.
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reimbursement", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("reimbursed_by_id", sa.String(length=32), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_transactions_reimbursed_by_id"), ["reimbursed_by_id"], unique=False
        )
        batch_op.create_index(
            "ix_transactions_household_reimbursement",
            ["household_id", "reimbursement"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_transactions_reimbursed_by_id_transactions"),
            "transactions",
            ["reimbursed_by_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_transactions_reimbursed_by_id_transactions"), type_="foreignkey"
        )
        batch_op.drop_index("ix_transactions_household_reimbursement")
        batch_op.drop_index(batch_op.f("ix_transactions_reimbursed_by_id"))
        batch_op.drop_column("reimbursed_by_id")
        batch_op.drop_column("reimbursement")
