"""an account takes one product of a statement

Revolut's account statement holds the checking account and every savings pocket
in one file, told apart by a `Product` column (`Current`, `Deposit`). Imported
into one account, every row landed there -- the savings pockets, and the day the
bank moved them to a new provider, as a deposit and an outflow of the whole
savings balance (issue #68).

`statement_product` says which of a file's products an account takes. Null for
every existing account: a file holding a single product is read whole, exactly
as before, and a checking account defaults to `Current` without being told.

Reversible: lossy -- downgrading drops each account's chosen product.

Revision ID: b7d2e5a91c63
Revises: a3f1c9d27e40
Create Date: 2026-09-23 21:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d2e5a91c63"
down_revision: str | Sequence[str] | None = "a3f1c9d27e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("statement_product", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "statement_product")
