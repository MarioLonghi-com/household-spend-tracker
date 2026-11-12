"""a household says which day its weeks start on

`households.week_start` (#452): monday, sunday or saturday. Weekly totals
begin on that day and are labelled by its date. Every existing household gets
monday, which is what the app assumed for everyone before, so no figure moves.

Reversible: clean -- dropping the column loses only the choice of day; every household's weeks go back to starting on Monday.

Revision ID: a7d3f5c20b64
Revises: e4b7a2c91d05
Create Date: 2026-11-12 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3f5c20b64"
down_revision: str | Sequence[str] | None = "e4b7a2c91d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The server default is what fills the existing households.
    with op.batch_alter_table("households", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("week_start", sa.String(length=16), nullable=False, server_default="monday")
        )


def downgrade() -> None:
    with op.batch_alter_table("households", schema=None) as batch_op:
        batch_op.drop_column("week_start")
