"""an account can be reset by a one-time link

`account_resets`, and `users.password_hash` and `users.totp_secret` become
nullable (#284).

A reset link is issued for one account with two switches, *password* and
*authenticator*. Each clears its credential at issue time, so an account can
now be without a password or a secret until its holder follows the link and
sets a new one. NULL is that state; nothing else writes it.

`users` is rebuilt to drop the NOT NULLs -- SQLite cannot alter a column in
place. Rebuilding a table other tables point at is only safe with foreign key
enforcement off, because SQLite's DROP TABLE runs an implicit DELETE that
fires every ON DELETE CASCADE beneath it (sessions, recovery codes,
memberships, keys). `alembic upgrade` in its own process has it off; this
turns it off where it can, and refuses rather than rebuilding a populated
`users` while it is still on.

Reversible: lossy -- every pending reset link, and with it the only way back into its account. An account whose password a reset had cleared gets a hash no password matches, and the older version has nothing that sets a password for somebody who cannot sign in: it stays shut, an only owner's included, until the backup is restored or this version is back and a new link is followed. One whose authenticator was cleared gets an empty secret no key opens and needs `scripts/reset_authenticator.py`, which is enough only if its password was not reset too. Have every pending link followed before rolling back.

Revision ID: 541128a33fd4
Revises: ef3af4e09c4a
Create Date: 2026-10-01 18:00:00.000000
"""

import base64
import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "541128a33fd4"
down_revision: str | Sequence[str] | None = "ef3af4e09c4a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: The salt of the hash a downgrade writes where a reset had cleared the
#: password. The older version's column is NOT NULL, so it gets a real
#: argon2id hash of 32 random bytes nobody keeps: no password matches it, and
#: checking one costs what checking any other does, so that version refuses it
#: exactly as it refuses a wrong password -- an empty string would be refused
#: at once, and the speed would say the address is a member's. The salt is
#: fixed so the next upgrade can find these and put the NULL back; salting a
#: hash of a random secret protects nothing that needs it.
_NO_PASSWORD_SALT = b"spendtracker:a-reset-cleared-this-password"


def _no_password_matches() -> str:
    from argon2 import PasswordHasher, Type
    from argon2.low_level import hash_secret

    cost = PasswordHasher()  # the parameters `app/auth/passwords.py` hashes with
    return hash_secret(
        secrets.token_bytes(32),
        _NO_PASSWORD_SALT,
        time_cost=cost.time_cost,
        memory_cost=cost.memory_cost,
        parallelism=cost.parallelism,
        hash_len=cost.hash_len,
        type=Type.ID,
    ).decode()


def _no_password_pattern() -> str:
    """A LIKE pattern for those hashes: the salt as argon2 encodes it."""
    salt = base64.b64encode(_NO_PASSWORD_SALT).decode().rstrip("=")
    return f"$argon2id$%${salt}$%"


def _rebuild_users_safely() -> None:
    """Refuse to rebuild a populated `users` with foreign keys enforced."""
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")  # a no-op inside a transaction
    enforced = bind.exec_driver_sql("PRAGMA foreign_keys").scalar()
    populated = bind.exec_driver_sql("SELECT EXISTS (SELECT 1 FROM users)").scalar()
    if enforced and populated:
        raise RuntimeError(
            "users would be rebuilt with foreign keys enforced, which deletes every row "
            "that cascades from it. Run `alembic upgrade head` in its own process "
            "(`make migrate`), where they are off."
        )


def upgrade() -> None:
    op.create_table(
        "account_resets",
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("password", sa.Boolean(), nullable=False),
        sa.Column("authenticator", sa.Boolean(), nullable=False),
        sa.Column("created_by_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_account_resets_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_account_resets_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_account_resets")),
        sa.UniqueConstraint("user_id", name=op.f("uq_account_resets_user_id")),
    )
    with op.batch_alter_table("account_resets", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_account_resets_token_hash"), ["token_hash"], unique=True)

    _rebuild_users_safely()
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.String(length=255), nullable=True)
        batch_op.alter_column("totp_secret", existing_type=sa.LargeBinary(), nullable=True)

    # Back from a downgrade, what it had to fill in is NULL again: a password
    # nobody knows, and an authenticator a reset cleared. Left as the empty
    # secret, the code step would refuse that account with "cannot be read with
    # this server key" rather than say it was reset, and `keycheck`, a restore,
    # `scripts.upgrade` and the doctor would all blame a `secret.key` that is
    # right -- `backup_bundle.a_sealed_secret` samples the first user, who is
    # usually the owner a lost phone sends a reset link to. Nothing else leaves
    # an empty secret in a ledger: `setup.complete` seals over its placeholder
    # before the row is flushed.
    op.execute(
        sa.text("UPDATE users SET password_hash = NULL WHERE password_hash LIKE :written").bindparams(
            written=_no_password_pattern()
        )
    )
    op.execute(sa.text("UPDATE users SET totp_secret = NULL WHERE length(totp_secret) = 0"))


def downgrade() -> None:
    _rebuild_users_safely()
    # A password a reset cleared becomes one nobody can give (see
    # `_NO_PASSWORD_SALT`). The older version has no way to set a password for
    # somebody who cannot sign in, so this account stays shut there: the
    # declaration at the top says so, because it is what an operator deciding
    # whether to roll back most needs to know.
    op.execute(
        sa.text("UPDATE users SET password_hash = :unknown WHERE password_hash IS NULL").bindparams(
            unknown=_no_password_matches()
        )
    )
    # An empty secret rather than a made-up one: nothing opens it, so the
    # account stays as shut as the reset left it, and `keycheck` on the older
    # version reports it as a secret this key cannot open -- which is the truth
    # there. `upgrade` turns it back into NULL.
    op.execute(sa.text("UPDATE users SET totp_secret = x'' WHERE totp_secret IS NULL"))
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column("password_hash", existing_type=sa.String(length=255), nullable=False)
        batch_op.alter_column("totp_secret", existing_type=sa.LargeBinary(), nullable=False)

    with op.batch_alter_table("account_resets", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_account_resets_token_hash"))
    op.drop_table("account_resets")
