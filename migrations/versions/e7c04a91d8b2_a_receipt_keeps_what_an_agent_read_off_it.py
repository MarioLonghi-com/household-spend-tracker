"""a receipt keeps what an agent read off it, as a claim and never as truth

An agent that photographs a till receipt can read the merchant and the total.
That reading is worth storing -- a person can compare it against the picture,
and a better tool can improve on it later without re-reading every image.

It is worth storing as a CLAIM. Nothing in the ledger is derived from it: not
an amount, not a date, not a payee, not a category. `/candidates` matches on
the ledger's own figures and deliberately not on this, because a suggestion
built from a guess would launder the guess into a decision.

The app does no OCR and calls no model, which is what keeps it a tailnet ledger
in fact rather than only in configuration. The agent reads the image -- that is
what an agent is for -- and posts the bytes and its reading together.

One nullable JSON column on a table that is already audited and already
CARRIED_WHOLE, so there is no classification decision to make.

> [!important] If this and another migration both claim their parent
> Branches that merge second re-point `down_revision`. See
> `d3b16f8c4a27` for the same note; the two touch different tables.

Reversible: lossy -- what an agent read off each receipt.

Revision ID: e7c04a91d8b2
Revises: d3b16f8c4a27
Create Date: 2026-09-21 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7c04a91d8b2"
down_revision: str | Sequence[str] | None = "d3b16f8c4a27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("receipts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("extracted", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("receipts", schema=None) as batch_op:
        batch_op.drop_column("extracted")
