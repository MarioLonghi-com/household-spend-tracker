"""the ledger is indexed by payee and by transfer account

`transactions.payee_id` had no index, so the category history an import
consults once per distinct payee -- on every commit and every preview -- was a
full scan of the ledger plus a sort: 7-11 ms a query at 50k rows, about 1.6 s
of a 300-line import. `(payee_id, date)` answers it from the index in date
order (issue #100).

`transfer_account_id` is read by the transfer matcher's "linked before" lanes
and by the RESTRICT check every account delete runs; it gets an index of its
own for the same reason.

Nothing but two indexes: no row is read or written.

Reversible: clean -- downgrading drops the two indexes and loses nothing.

Revision ID: c3e8a1f05d72
Revises: b7d2e5a91c63
Create Date: 2026-09-24 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3e8a1f05d72"
down_revision: str | Sequence[str] | None = "b7d2e5a91c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_transactions_payee_date", "transactions", ["payee_id", "date"])
    op.create_index(
        "ix_transactions_transfer_account_id", "transactions", ["transfer_account_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_transactions_transfer_account_id", table_name="transactions")
    op.drop_index("ix_transactions_payee_date", table_name="transactions")
