"""households wear a colour

Schema only. The columns arrive with a server default so that adding a NOT NULL
column to a table that already has rows is legal; the default is then dropped,
because the application model is what decides a new household's palette and a
database default that disagrees with it is a second answer to the same question.

Spreading existing households across the palettes is data, not schema, and is
its own revision.

Reversible: lossy -- each household's palette and accent.

Revision ID: cddca0dc2fc9
Revises: 90265808d207
Create Date: 2026-09-18 23:08:53.696139
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cddca0dc2fc9"
down_revision: str | Sequence[str] | None = "90265808d207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("households", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("theme", sa.String(length=32), nullable=False, server_default="moss")
        )
        batch_op.add_column(sa.Column("accent", sa.String(length=7), nullable=True))
    with op.batch_alter_table("households", schema=None) as batch_op:
        batch_op.alter_column("theme", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("households", schema=None) as batch_op:
        batch_op.drop_column("accent")
        batch_op.drop_column("theme")
