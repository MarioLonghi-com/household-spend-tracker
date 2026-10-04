"""an account says what banks call it

A table of identifiers -- IBAN, account number, card number, a pocket's name,
the tag a bank puts in its download's file name, and how a bank spells a
household member's name. Before this the only place an IBAN could live was the
account's display name, so neither a file nor a descriptor like
"TO A/C <number>" could be tied back to the account it meant (issue #66).

Nothing is backfilled. An account whose name happens to be its IBAN is not
assumed to want that as an identifier; the Accounts screen suggests it.

Reversible: lossy -- downgrading drops every identifier typed in since.

Revision ID: a3f1c9d27e40
Revises: c8a1f6b30d47
Create Date: 2026-09-23 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f1c9d27e40"
down_revision: str | Sequence[str] | None = "c8a1f6b30d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_identifiers",
        sa.Column("household_id", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=32), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=120), nullable=False),
        sa.Column("normalised", sa.String(length=120), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"],
            name=op.f("fk_account_identifiers_household_id_households"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"],
            name=op.f("fk_account_identifiers_account_id_accounts"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_identifiers")),
        sa.UniqueConstraint(
            "household_id", "normalised", name="uq_account_identifiers_value"
        ),
    )
    op.create_index(
        op.f("ix_account_identifiers_household_id"), "account_identifiers", ["household_id"]
    )
    op.create_index(
        op.f("ix_account_identifiers_account_id"), "account_identifiers", ["account_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_account_identifiers_account_id"), table_name="account_identifiers")
    op.drop_index(op.f("ix_account_identifiers_household_id"), table_name="account_identifiers")
    op.drop_table("account_identifiers")
