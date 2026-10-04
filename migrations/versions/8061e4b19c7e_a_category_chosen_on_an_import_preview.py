"""a category chosen on an import preview

Nullable and with no backfill: null means "whatever the payee's rule decides",
which is what every existing staged line already meant.

Reversible: clean -- a category picked on an import preview, on lines not yet committed. Committed transactions keep theirs.

Revision ID: 8061e4b19c7e
Revises: 9dc3906a0961
Create Date: 2026-09-19 10:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8061e4b19c7e"
down_revision: str | Sequence[str] | None = "9dc3906a0961"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("import_lines", schema=None) as batch_op:
        batch_op.add_column(sa.Column("category_id", sa.String(length=32), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_import_lines_category_id_categories"),
            "categories",
            ["category_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("import_lines", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_import_lines_category_id_categories"), type_="foreignkey"
        )
        batch_op.drop_column("category_id")
