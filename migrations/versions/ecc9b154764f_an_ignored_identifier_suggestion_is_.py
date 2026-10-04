"""an ignored identifier suggestion is remembered

Issue #130. The Accounts screen suggests identifiers it can read off the
ledger -- a pocket's quoted name, an `A/C` number, a card's last four, the tag
in a statement's file name -- and a person adds each one or ignores it.
`ignored_identifier_suggestions` is the "ignore": one row per value and kind,
never offered again. The table starts empty; nothing existing changes.

Reversible: lossy -- downgrading drops every ignored suggestion, so they are offered again; nothing in the ledger changes.

Revision ID: ecc9b154764f
Revises: db3cf4a5f9d3
Create Date: 2026-09-24 21:08:34.573893
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ecc9b154764f"
down_revision: str | Sequence[str] | None = "db3cf4a5f9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `kind` rendered as the sa.String it actually is, not as EnumStr: a
    # migration carries no application import.
    op.create_table(
        "ignored_identifier_suggestions",
        sa.Column("household_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("value", sa.String(length=120), nullable=False),
        sa.Column("normalised", sa.String(length=120), nullable=False),
        sa.Column("ignored_by_id", sa.String(length=32), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_ignored_identifier_suggestions_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["ignored_by_id"],
            ["users.id"],
            name=op.f("fk_ignored_identifier_suggestions_ignored_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ignored_identifier_suggestions")),
        sa.UniqueConstraint(
            "household_id", "kind", "normalised", name="uq_ignored_identifier_suggestions_value"
        ),
    )
    with op.batch_alter_table("ignored_identifier_suggestions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_ignored_identifier_suggestions_household_id"),
            ["household_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("ignored_identifier_suggestions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_ignored_identifier_suggestions_household_id"))

    op.drop_table("ignored_identifier_suggestions")
