"""a grant that proves it is still you, spent on one act

Minting an agent key is the first thing in this app whose effect outlives the
session that performed it. A password change ends every other session; an
authenticator re-enrolment revokes every trusted device. Both are loud, and
somebody who gets their laptop back finds out. A key is quiet: it keeps working
from somewhere else for ninety days and nothing about the browser it was made
in says so afterwards.

So it asks for both factors again, now, and what that buys is a row here.

A row rather than a sealed token, for the reason `pending_sign_ins` was moved
out of a cookie: a sealed token is a bearer token, replayable for the whole of
its window and portable to any browser. Deleting the row on use is what makes
this single-use, and single-use is the property worth having -- one grant is
worth one key.

Unaudited, like every other table that records what happened at the door rather
than what happened to the ledger, and therefore in `EXPECTED_EXCLUDED` and in
`PURGE` for the `/db` snapshot.

Reversible: clean -- short-lived step-up grants. Anyone affected confirms again.

Revision ID: a4c81e26f9d0
Revises: d7e2b410c8a6
Create Date: 2026-09-20 11:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c81e26f9d0"
down_revision: str | Sequence[str] | None = "d7e2b410c8a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "step_up_grants",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_step_up_grants_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_step_up_grants")),
    )
    op.create_index(op.f("ix_step_up_grants_user_id"), "step_up_grants", ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_step_up_grants_user_id"), table_name="step_up_grants")
    op.drop_table("step_up_grants")
