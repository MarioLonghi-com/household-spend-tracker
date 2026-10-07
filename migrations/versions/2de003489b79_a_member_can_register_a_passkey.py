"""a member can register a passkey

Two tables and one column, for passkeys (#120, from #47 §2):

- `passkeys`: one row per registered WebAuthn credential -- its public key,
  signature counter, the RP ID (host name) it was made for, the provider's
  AAGUID and whether it is synced. Audited.
- `webauthn_challenges`: short-lived, single-use challenges for the register
  and sign-in ceremonies. Not audited; swept by `auth/housekeeping.py`.
- `users.webauthn_user_handle`: the random `user.id` a member's passkeys carry,
  NULL until their first registration.

Only additions: no existing row or column changes, so an upgrade keeps every
row and the rehearsal's counts cannot go down.

Reversible: lossy -- every registered passkey. The downgrade drops the passkeys table and the user handles; members sign in with password + code, as before passkeys existed, and register again after upgrading back. The challenges it drops expire within minutes anyway.

Revision ID: 2de003489b79
Revises: 2bec6ce88f3d
Create Date: 2026-10-07 14:31:06.267223
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2de003489b79"
down_revision: str | Sequence[str] | None = "2bec6ce88f3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "passkeys",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("credential_id", sa.String(length=1400), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("transports", sa.JSON(), nullable=True),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("rp_id", sa.String(length=253), nullable=False),
        sa.Column("aaguid", sa.String(length=36), nullable=True),
        sa.Column("backup_eligible", sa.Boolean(), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_passkeys_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_passkeys")),
    )
    with op.batch_alter_table("passkeys", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_passkeys_credential_id"), ["credential_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_passkeys_user_id"), ["user_id"], unique=False)

    op.create_table(
        "webauthn_challenges",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=True),
        sa.Column("purpose", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_webauthn_challenges_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_webauthn_challenges")),
    )
    with op.batch_alter_table("webauthn_challenges", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_webauthn_challenges_expires_at"), ["expires_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_webauthn_challenges_user_id"), ["user_id"], unique=False)

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("webauthn_user_handle", sa.LargeBinary(length=64), nullable=True))
        batch_op.create_unique_constraint(
            batch_op.f("uq_users_webauthn_user_handle"), ["webauthn_user_handle"]
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_users_webauthn_user_handle"), type_="unique")
        batch_op.drop_column("webauthn_user_handle")

    with op.batch_alter_table("webauthn_challenges", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_webauthn_challenges_user_id"))
        batch_op.drop_index(batch_op.f("ix_webauthn_challenges_expires_at"))
    op.drop_table("webauthn_challenges")

    with op.batch_alter_table("passkeys", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_passkeys_user_id"))
        batch_op.drop_index(batch_op.f("ix_passkeys_credential_id"))
    op.drop_table("passkeys")
