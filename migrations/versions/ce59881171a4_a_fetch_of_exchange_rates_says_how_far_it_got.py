"""a fetch of exchange rates says how far it got

Issue #409. Fetch now asks for its range one calendar year per request and
stores each year as it arrives, so a fetch over many years no longer fails on
the size or the time of a single answer, and one that does fail keeps the
years before it. Four columns on `fx_fetches` say how far it got:
`chunks_total`, `chunks_done`, and `chunk_since`/`chunk_until`, the dates of
the request in flight or of the one it stopped on.

A fetch recorded before this made one request for its whole range, so it is
filled in as 1 of 1 -- done if it finished well, not done otherwise.

Reversible: lossy -- how far each recorded fetch got (the four progress columns of the fetch history). The rates it stored stay, and nothing in the ledger changes.

Revision ID: ce59881171a4
Revises: 8e1c4a7f5d02
Create Date: 2026-11-09 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ce59881171a4"
down_revision: str | Sequence[str] | None = "8e1c4a7f5d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("fx_fetches", schema=None) as batch_op:
        batch_op.add_column(sa.Column("chunks_total", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("chunks_done", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("chunk_since", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("chunk_until", sa.Date(), nullable=True))
    # `fx_fetches` is not audited, so a plain statement is right here.
    op.execute(
        "UPDATE fx_fetches SET chunks_total = 1, "
        "chunks_done = CASE WHEN outcome = 'ok' THEN 1 ELSE 0 END"
    )


def downgrade() -> None:
    with op.batch_alter_table("fx_fetches", schema=None) as batch_op:
        batch_op.drop_column("chunk_until")
        batch_op.drop_column("chunk_since")
        batch_op.drop_column("chunks_done")
        batch_op.drop_column("chunks_total")
