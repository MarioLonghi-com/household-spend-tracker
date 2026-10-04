"""a receipt says which bytes it holds, separately from what identifies it

`content_sha256` was doing two jobs and only one of them is safe to let a
client influence. It is the **dedupe key**, and the mobile capture page
supplies it, because the phone compresses before uploading and the server
never sees the original -- without that, the same photograph sent from the
phone and from the desktop becomes two different receipts.

It was also the **pointer into the blob store**, which means a client could
choose which stored bytes its new receipt pointed at. Upload anything at all
while claiming the hash of a receipt in another household, and the picture
served back is theirs. The spec named this property and required it to hold;
the first implementation broke it, and the test that asked for the image back
got a different image.

So: `blob_sha256` is the hash of what this server actually received, computed
here and never supplied. `content_sha256` stays the dedupe key. They are equal
on every desktop upload and differ on every compressed one.

Backfilled from `content_sha256`, which is correct for every row that can
exist at this revision: nothing has shipped, and before this the two were the
same value by construction.

Reversible: clean -- a hash recomputed from the bytes, which are still there.

Revision ID: d7e2b410c8a6
Revises: c58a1d7b2e40
Create Date: 2026-09-19 22:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7e2b410c8a6"
down_revision: str | Sequence[str] | None = "c58a1d7b2e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("receipts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("blob_sha256", sa.String(length=64), nullable=True))
    op.execute("UPDATE receipts SET blob_sha256 = content_sha256 WHERE blob_sha256 IS NULL")
    with op.batch_alter_table("receipts", schema=None) as batch_op:
        batch_op.alter_column("blob_sha256", existing_type=sa.String(length=64), nullable=False)
        batch_op.create_index(op.f("ix_receipts_blob_sha256"), ["blob_sha256"])


def downgrade() -> None:
    with op.batch_alter_table("receipts", schema=None) as batch_op:
        batch_op.drop_index(op.f("ix_receipts_blob_sha256"))
        batch_op.drop_column("blob_sha256")
