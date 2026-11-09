"""a key may be allowed to edit categories

Issue #366. An agent key may add categories (and a group for them) and delete
an unused one through the agent API -- but only a key a person chose to allow,
because `write` should not carry it: categorising a row and changing the
household's category tree are different kinds of act. One boolean on
`agent_keys`, like `may_commit`, false for every existing key: nothing an
existing key could do changes.

Reversible: lossy -- downgrading drops the permission from every key that had it; nothing in the ledger changes, and those keys go back to categorising only.

Revision ID: d2b8e6f41c07
Revises: a7e3c1d95b20
Create Date: 2026-11-09 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2b8e6f41c07"
down_revision: str | Sequence[str] | None = "a7e3c1d95b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The server default is what fills the existing rows: false, so no key
    # gains a permission by being upgraded.
    op.add_column(
        "agent_keys",
        sa.Column(
            "may_edit_categories", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_keys", "may_edit_categories")
