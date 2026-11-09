"""forint and New Taiwan dollar amounts keep two decimals, as ISO 4217 has them

Issue #396 §8. `app/money.py` listed HUF and TWD with no minor unit; ISO 4217
gives both two. The exponent changes in code, and with it what a stored figure
means: 1000 in a HUF account read as 1,000 forint and would read as 10.00
forint. So every stored HUF and TWD figure is multiplied by 100 here, in the
same release, and keeps its meaning: `transactions.amount` and
`reconciliations.statement_balance` of every account in either currency.

`transactions.transfer_fx_rate` is untouched: it is a ratio of major units,
so it does not move when both legs' minor units are rescaled.

A ledger with no HUF or TWD account changes nothing at all.

The audit log is not rewritten, and that is the limit of this migration: an
undo of a batch from *before* the upgrade puts back the amount its image
recorded, in the old minor units. Undo such a batch before upgrading, or
correct the amount after.

Reversible: lossy -- downgrading divides the same figures by 100; a HUF or TWD amount entered after the upgrade with fillér or cents is rounded half-up to a whole unit on the way down.

Revision ID: 8e1c4a7f5d02
Revises: 3b6f0d2e9a41
Create Date: 2026-11-09 18:10:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8e1c4a7f5d02"
down_revision: str | Sequence[str] | None = "3b6f0d2e9a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The two codes whose exponent moved from 0 to 2.
_ACCOUNTS = "SELECT id FROM accounts WHERE upper(currency) IN ('HUF', 'TWD')"


def upgrade() -> None:
    op.execute(f"UPDATE transactions SET amount = amount * 100 WHERE account_id IN ({_ACCOUNTS})")
    op.execute(
        "UPDATE reconciliations SET statement_balance = statement_balance * 100 "
        f"WHERE account_id IN ({_ACCOUNTS})"
    )


def downgrade() -> None:
    # Half-up to a whole unit, the rounding `money.to_minor` uses: integer
    # arithmetic, so a negative figure rounds away from zero as well.
    for table, column in (("transactions", "amount"), ("reconciliations", "statement_balance")):
        op.execute(
            f"UPDATE {table} SET {column} = CASE WHEN {column} >= 0 "
            f"THEN ({column} + 50) / 100 ELSE -((-{column} + 50) / 100) END "
            f"WHERE account_id IN ({_ACCOUNTS})"
        )
