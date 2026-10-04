"""a household decides whether receipts keep the original upload

`SPENDTRACKER_RECEIPTS_KEEP_ORIGINAL` was read from the process environment,
which is the wrong altitude twice over: it is a per-household judgement about
*their* documents, and an env var cannot be seen by the person whose receipts
they are, changed by them, or audited. Issue #62.

Defaulted **false**, which is what every existing install already does -- the
env var was off unless somebody set it -- so this migration changes nobody's
behaviour. The env var stays as a floor: household OR environment, never AND.

Reversible: clean -- only which households had turned it on. No receipt and no
blob is touched, and the env var goes back to being the only answer.

Revision ID: c8a1f6b30d47
Revises: b4c9e1d70a25
Create Date: 2026-09-23 12:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8a1f6b30d47"
down_revision: str | Sequence[str] | None = "b4c9e1d70a25"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("households", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "receipts_keep_original",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("households", schema=None) as batch_op:
        batch_op.drop_column("receipts_keep_original")
