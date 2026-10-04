"""a credential for a program, scoped to one household and revocable at once

An agent is not a user and deliberately gets no `users` row. Sign-in here is
password then TOTP and `users.totp_secret` is NOT NULL on purpose, so anything
that could sign in would have to hold both factors -- a second factor stored
beside the first, which is not a second factor. The door has to be a different
kind of credential.

Opaque, 256 bits, stored as its SHA-256, like `sessions`, `trusted_devices` and
`invitations`: the lookup is the verification, so there is no signature and no
dependency on `secret.key`, and revocation is a row read rather than a wait for
a token to expire.

Audited, unlike the other credential tables. Issuing one is a deliberate act a
household should be able to read in History, and an undo must never resurrect a
revoked key -- which is why `token_hash` is redacted, alongside `last_used_at`,
whose redaction is what lets a GET record the use of the key that authorised it
without opening a batch.

`household_id` is RESTRICT, like `transactions` and `receipts`: a household
reaches its keys through an ORM relationship the audit hook walks, and a
database-level CASCADE would be a second path that deletes the same rows with
nothing recorded. `user_id` is CASCADE, like `recovery_codes` and
`invitations`, and is covered by a relationship on `User` for the same reason.

`scope` is one column of two values rather than the JSON list the spec drafted.
`write` implies `read`, so a list has more shapes than meanings and every
invalid one would have to be refused in code instead of by the schema.

Reversible: lossy -- every agent key. Each one has to be reissued and re-pasted into whatever used it.

Revision ID: b7f3d94a15c8
Revises: a4c81e26f9d0
Create Date: 2026-09-20 12:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f3d94a15c8"
down_revision: str | Sequence[str] | None = "a4c81e26f9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("agent_name", sa.String(length=80), nullable=True),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("household_id", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("may_commit", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["household_id"], ["households.id"],
            name=op.f("fk_agent_keys_household_id_households"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name=op.f("fk_agent_keys_user_id_users"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_keys")),
    )
    op.create_index(op.f("ix_agent_keys_token_hash"), "agent_keys", ["token_hash"], unique=True)
    op.create_index(op.f("ix_agent_keys_user_id"), "agent_keys", ["user_id"])
    op.create_index(op.f("ix_agent_keys_household_id"), "agent_keys", ["household_id"])
    op.create_index("ix_agent_keys_user_household", "agent_keys", ["user_id", "household_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_keys_user_household", table_name="agent_keys")
    op.drop_index(op.f("ix_agent_keys_household_id"), table_name="agent_keys")
    op.drop_index(op.f("ix_agent_keys_user_id"), table_name="agent_keys")
    op.drop_index(op.f("ix_agent_keys_token_hash"), table_name="agent_keys")
    op.drop_table("agent_keys")
