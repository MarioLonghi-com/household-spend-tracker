"""a desktop may open with Touch ID

"Open with Touch ID" on the desktop (#432): a convenience on top of the
password, never instead of it. The backend issues 256 random bits and keeps
only their SHA-256 here, exactly as `sessions` and `trusted_devices` do; the
shell keeps the value, encrypted by the operating system, and presents it only
after the person has passed the biometric prompt. The password itself is never
stored anywhere.

One row per person, revoked by turning it off, a password change, a recovery
code, a restore from backup and an account reset. Unaudited, like every other
table that records what happened at the door rather than what happened to the
ledger, and therefore in `EXPECTED_EXCLUDED` and in `PURGE` for the `/db`
snapshot.

Reversible: clean -- drops the table. Whoever had Touch ID on turns it on again.

Revision ID: e4b7a2c91d05
Revises: ce59881171a4
Create Date: 2026-11-12 08:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4b7a2c91d05"
down_revision: str | Sequence[str] | None = "ce59881171a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_unlocks",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_device_unlocks_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_device_unlocks")),
    )
    op.create_index(op.f("ix_device_unlocks_user_id"), "device_unlocks", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_device_unlocks_user_id"), table_name="device_unlocks")
    op.drop_table("device_unlocks")
