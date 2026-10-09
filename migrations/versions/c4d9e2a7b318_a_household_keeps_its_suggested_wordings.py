"""a household keeps the wordings it suggests for the draft languages

Issue #272. An owner reviews the draft translations in the app and suggests a
better wording for a message; `translation_suggestions` keeps each one --
which language, which message (its English source and context), the words, an
optional note, who and when, and whether it has been applied to the
catalogs. The table starts empty; nothing existing changes.

Reversible: lossy -- downgrading drops every suggested wording, so export them first; nothing in the ledger changes.

Revision ID: c4d9e2a7b318
Revises: 2de003489b79
Create Date: 2026-10-09 04:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d9e2a7b318"
down_revision: str | Sequence[str] | None = "2de003489b79"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # `status` rendered as the sa.String it actually is, not as EnumStr: a
    # migration carries no application import.
    op.create_table(
        "translation_suggestions",
        sa.Column("household_id", sa.String(length=32), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column("context", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("suggested", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("suggested_by_id", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_translation_suggestions_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["suggested_by_id"],
            ["users.id"],
            name=op.f("fk_translation_suggestions_suggested_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_translation_suggestions")),
    )
    with op.batch_alter_table("translation_suggestions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_translation_suggestions_household_id"), ["household_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("translation_suggestions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_translation_suggestions_household_id"))

    op.drop_table("translation_suggestions")
