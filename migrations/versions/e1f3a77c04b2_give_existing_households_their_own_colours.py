"""give existing households their own colours

**This revision moves data, not schema**, which is the exception the standing
rules call out: a migration that backfills has to say so in its own revision.

Why it is worth one: the whole point of a per-household palette is that two
ledgers open in one browser cannot be mistaken for each other. Leaving every
existing household on the default would mean the feature fails at its stated job
on the first day, for exactly the people who already have more than one.

Rows are ordered by `created_at` so the assignment is deterministic -- run it
twice on two copies of the same database and you get the same colours.

Reversible: lossy -- every household's chosen theme, rewritten to 'moss'. There is nothing to put back.

Revision ID: e1f3a77c04b2
Revises: cddca0dc2fc9
Create Date: 2026-09-18 23:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f3a77c04b2"
down_revision: str | Sequence[str] | None = "cddca0dc2fc9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Written out rather than imported from `app.theming`. A migration describes
#: the database at one moment in history; importing today's table would make an
#: old migration change meaning when the palettes do.
KEYS = ("moss", "slate", "clay", "indigo", "plum", "harbour")


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id FROM households ORDER BY created_at, id")
    ).fetchall()
    for index, (household_id,) in enumerate(rows):
        connection.execute(
            sa.text("UPDATE households SET theme = :theme WHERE id = :id"),
            {"theme": KEYS[index % len(KEYS)], "id": household_id},
        )


def downgrade() -> None:
    # There is nothing to put back: before this revision every household had the
    # default, and restoring that is what the column's own default already says.
    op.get_bind().execute(sa.text("UPDATE households SET theme = 'moss'"))
