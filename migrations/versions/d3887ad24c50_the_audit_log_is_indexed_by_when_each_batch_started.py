"""the audit log is indexed by when each batch started

`batches.started_at` had an index only behind `household_id`, so a question
asked of the whole instance by time -- the owner's notice of resets and new
owners, "the last fourteen days", on every owner's page load -- read every
batch the ledger had ever written to answer it: about 220 ms at 200k
batches, to return nothing (#286). `(started_at)` on its own answers it from
the window.

Nothing but one index: no row is read or written.

Reversible: clean -- downgrading drops the index and loses nothing.

Revision ID: d3887ad24c50
Revises: 541128a33fd4
Create Date: 2026-10-01 22:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d3887ad24c50"
down_revision: str | Sequence[str] | None = "541128a33fd4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_batches_started_at", "batches", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_batches_started_at", table_name="batches")
