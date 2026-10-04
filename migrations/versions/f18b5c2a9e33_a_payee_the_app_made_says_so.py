"""a payee the app made says so

`accounts._write_opening_balance` writes a real transaction whose only mark is
the payee *name* "Opening balance". Nothing in the schema said what it was, so
the only way a report could tell an opening balance from a salary payment was
to string-match a name a person is free to rename, merge or translate. Left
alone, every account's opening balance lands in month one as income: on a
freshly imported ledger the "money in" column for the first month is the
account balance, and the report opens on a lie.

Transfers had half of an answer already -- `payees.transfer_account_id` says
*which* account -- but that is a different question from *what kind*, and a
household is free to have a real payee called "Transfer" that moves no money.

So: one nullable column, two values, each with designed behaviour. Neither is
flow; both are counted in full by every balance query.

The backfill is honest about what it can know. `name = 'Opening balance'` is
exactly the string the old code wrote and nothing else wrote it, and
`transfer_account_id IS NOT NULL` is the column the old code set. A household
that renamed its opening-balance payee before this migration ran is not
recoverable from the schema, and this does not pretend otherwise -- it marks
what it can prove and leaves the rest null, which is the state a report reads
as "an ordinary payee".

Reversible: clean -- a flag re-applied by `get_or_create` the next time the payee is touched.

Revision ID: f18b5c2a9e33
Revises: f5a2c8e04b19
Create Date: 2026-09-21 15:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f18b5c2a9e33"
down_revision: str | Sequence[str] | None = "f5a2c8e04b19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Rendered as the sa.String it actually is, not as EnumStr: a migration
    # carries no application import, so an old revision still runs after the
    # enum it was written against has changed.
    op.add_column("payees", sa.Column("system", sa.String(length=16), nullable=True))

    # Marked before the transfer pass, so that a household which somehow has
    # both marks available gets the transfer one -- a payee with a
    # transfer_account_id is a transfer payee whatever it is called.
    op.execute(
        sa.text(
            "UPDATE payees SET system = 'opening_balance' "
            "WHERE name = 'Opening balance' AND transfer_account_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE payees SET system = 'transfer' WHERE transfer_account_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("payees", "system")
