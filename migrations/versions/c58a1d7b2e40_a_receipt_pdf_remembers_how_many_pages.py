"""a receipt PDF remembers how many pages it has

Added after running the ingest over a real corpus of about a hundred receipt
PDFs rather than over fixtures. Most of them run to more than one page, some
to dozens. The frame shows page 1 and keeps the original for the rest,
which is right -- but without a count it presents one page as though it were
the document, which is the same class of quiet lie as a truncated register.

Nullable, because a photograph has no pages, and not backfilled, because no
receipt exists yet.

Reversible: clean -- a page count re-read from the stored PDF.

Revision ID: c58a1d7b2e40
Revises: b31c7e04a9d5
Create Date: 2026-09-19 21:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c58a1d7b2e40"
down_revision: str | Sequence[str] | None = "b31c7e04a9d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("receipts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("page_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("receipts", schema=None) as batch_op:
        batch_op.drop_column("page_count")
