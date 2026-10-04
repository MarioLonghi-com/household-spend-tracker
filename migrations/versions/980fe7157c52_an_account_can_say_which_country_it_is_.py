"""an account can say which country it is in

ISO 3166-1 alpha-2, nullable and not backfilled: no existing account has said,
and guessing one from the currency would be wrong often enough to matter -- a
euro account is not necessarily in any particular country.

Reversible: lossy -- the country on every account.

Revision ID: 980fe7157c52
Revises: 51f43a45fada
Create Date: 2026-09-19 16:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "980fe7157c52"
down_revision: str | Sequence[str] | None = "51f43a45fada"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("country", sa.String(length=2), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_column("country")
