"""an account with no bank says so one way

**This revision moves data, not schema.** `accounts.institution` and
`accounts.note` had two spellings for "none": the new-account panel sent null
for a blank field, and the edit panel sent whatever was in the box -- `""`
once it was emptied, and `"  Bank "` with its spaces (#20). Anything that asks
for accounts with no bank, a filter or an export, missed the `""` half. The
service now trims both and stores a blank one as null; this brings the rows
already written into line with it: every value is trimmed, and one that is
empty or only whitespace becomes NULL. No row is added or removed and no other
column changes.

The trim is Python's `str.strip()`, copied here rather than imported from
`app.services.accounts.free_text`, so this keeps its meaning when that moves
on; SQLite's `trim()` takes spaces only and would leave a tab or a no-break
space behind.

The audit log does not see this -- Alembic writes on a plain connection, so
there are no `changes` rows and History shows no act. Nothing is lost to undo
by that: no value that meant anything changes, only its surrounding blanks.

Reversible: lossy -- which empty bank or note was stored as '' rather than NULL, and the spaces around the rest. Neither meant anything and the older version reads NULL the same way, so the downgrade leaves the rows as they are.

Revision ID: 2bec6ce88f3d
Revises: d3887ad24c50
Create Date: 2026-10-05 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2bec6ce88f3d"
down_revision: str | Sequence[str] | None = "d3887ad24c50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("institution", "note")


def upgrade() -> None:
    conn = op.get_bind()
    for column in COLUMNS:
        rows = conn.execute(
            sa.text(f"SELECT id, {column} FROM accounts WHERE {column} IS NOT NULL")
        ).all()
        for account_id, value in rows:
            tidy = value.strip() or None
            if tidy != value:
                conn.execute(
                    sa.text(f"UPDATE accounts SET {column} = :value WHERE id = :id"),
                    {"value": tidy, "id": account_id},
                )


def downgrade() -> None:
    # Nothing to put back: which blank was '' and which was NULL, and the spaces
    # around a bank's name, were never recorded anywhere else. Both versions read
    # NULL as "no bank", so the rows stay as they are.
    pass
