"""receipts, and the content-addressed store their bytes live in

Two create_tables and nothing else. `transactions` is untouched on purpose:
whether a row has a receipt is computed from one indexed read per register
request, not stored on the row -- storing it would be a second place the truth
lives and four code paths that have to maintain it.

The cheapest possible migration to apply and to reverse: a downgrade drops two
tables that nothing else references.

Reversible: lossy -- every receipt and every stored image. The worst one on this list.

Revision ID: b31c7e04a9d5
Revises: 980fe7157c52
Create Date: 2026-09-19 20:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b31c7e04a9d5"
down_revision: str | Sequence[str] | None = "980fe7157c52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receipts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("household_id", sa.String(length=32), nullable=False),
        sa.Column("transaction_id", sa.String(length=32), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_filename", sa.String(length=300), nullable=True),
        sa.Column("media_type", sa.String(length=80), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=True),
        sa.Column("captured_at_is_local", sa.Boolean(), nullable=False),
        sa.Column("gps_lat", sa.Float(), nullable=True),
        sa.Column("gps_lon", sa.Float(), nullable=True),
        sa.Column("gps_accuracy_m", sa.Float(), nullable=True),
        sa.Column("gps_bearing", sa.Float(), nullable=True),
        sa.Column("camera", sa.String(length=120), nullable=True),
        sa.Column("exif", sa.JSON(), nullable=True),
        sa.Column("client_encoded", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("uploaded_by_id", sa.String(length=32), nullable=True),
        # RESTRICT: a household reaches its receipts through the ORM
        # relationship, which the audit hook walks. A database-level cascade
        # would delete the same rows without the hook hearing about it.
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_receipts_household_id_households"),
            ondelete="RESTRICT",
        ),
        # SET NULL: deleting a transaction must not destroy the evidence for
        # it. The receipt falls back to the inbox, visible and re-attachable.
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_receipts_transaction_id_transactions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            name=op.f("fk_receipts_uploaded_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_receipts")),
        sa.UniqueConstraint("transaction_id", "content_sha256", name="uq_receipts_txn_content"),
    )
    op.create_index(op.f("ix_receipts_content_sha256"), "receipts", ["content_sha256"])
    op.create_index(op.f("ix_receipts_household_id"), "receipts", ["household_id"])
    op.create_index(op.f("ix_receipts_transaction_id"), "receipts", ["transaction_id"])
    op.create_index("ix_receipts_gps", "receipts", ["household_id", "gps_lat", "gps_lon"])
    op.create_index("ix_receipts_household_txn", "receipts", ["household_id", "transaction_id"])
    op.create_index("ix_receipts_inbox", "receipts", ["household_id", "created_at"])

    op.create_table(
        "receipt_blobs",
        # No surrogate id: this table is not audited, so nothing needs a single
        # row_id -- and (sha256, role) means the store *cannot* hold the same
        # content twice, as a property of the schema rather than of the code.
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("media_type", sa.String(length=80), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("orphaned_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("sha256", "role", name=op.f("pk_receipt_blobs")),
    )


def downgrade() -> None:
    op.drop_table("receipt_blobs")
    op.drop_index("ix_receipts_inbox", table_name="receipts")
    op.drop_index("ix_receipts_household_txn", table_name="receipts")
    op.drop_index("ix_receipts_gps", table_name="receipts")
    op.drop_index(op.f("ix_receipts_transaction_id"), table_name="receipts")
    op.drop_index(op.f("ix_receipts_household_id"), table_name="receipts")
    op.drop_index(op.f("ix_receipts_content_sha256"), table_name="receipts")
    op.drop_table("receipts")
