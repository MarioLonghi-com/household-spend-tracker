"""a transaction keeps the other keys its bank line had

`transactions.import_alt_ids`, a nullable JSON list (#264). A row has one
`import_id`, and the unique constraint is about that one. Two things need a
row to be known by a second key as well:

- A statement line absorbing a One-time Import row takes its `import_id`, and
  the row's `ynab:<ref>` key is what a repeat One-time Import recognises it by.
  It is kept here instead of being overwritten.
- YNAB's own id for a bank line it imported, `YNAB:<milliunits>:<date>:<n>`,
  is our statement key's shape in other units. Translated and kept here, it
  lets the statement holding that line match the row exactly rather than by
  the four-day twin rule.

No existing column could hold either: `import_lines.parsed` only exists for a
line a statement staged, and a One-time Import has none. No backfill: a row
imported before this keeps matching by the twin rule, as it did.

`ALTER TABLE ... ADD COLUMN` of a nullable column with no default changes only
SQLite's schema record; the table is not copied.

Reversible: lossy -- the column and the keys in it. After a downgrade a repeat One-time Import offers a row a statement absorbed as a possible duplicate instead of skipping it, and a statement line that would have matched exactly falls back to the four-day twin rule.

Revision ID: ef3af4e09c4a
Revises: 25e73951a565
Create Date: 2026-10-01 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ef3af4e09c4a"
down_revision: str | Sequence[str] | None = "25e73951a565"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("import_alt_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_column("import_alt_ids")
