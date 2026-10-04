"""a rule can rewrite a rail instead of naming a payee

`SQ *`, `PAGO MOVIL` and `COMPRA INTERNET` are payment rails, not shops. Every
merchant that takes Square appears behind `SQ *`, so a rule of the only shape
this table could express -- pattern -> one fixed payee -- is wrong for them by
construction: there is no one payee to point at. Cleaning them up meant one
hand-written rule per merchant, forever, and a new one the first time you ate
anywhere new. Issue #58.

Three columns, and the interesting one is the `nullable` change:

- `action` says what the rule does once it has matched. Defaulted to `map`
  server-side as well as in the model, so every rule written before today is a
  `map` rule and behaves exactly as it did. Nothing is rewritten by this
  migration -- it is additive, and reversible with no loss unless a rewrite
  rule has been written in the meantime.
- `payee_id` becomes nullable, because a rewrite rule has nothing to point at.
  A `map` rule without one is refused in `payees.create_rule`, which is where
  the refusal can be a sentence. A CHECK constraint could say it in the
  database, and on SQLite every future `batch_alter_table` on this table would
  have to carry it forward by hand.
- `replacement` is what a rewrite puts in place of what it matched -- NULL
  meaning "put nothing there", which is the strip case and the common one.


Reversible: lossy -- every rewrite rule a household has written. No transaction and no payee is touched -- a rule is a recipe, never a row of money.

Revision ID: b4c9e1d70a25
Revises: f18b5c2a9e33
Create Date: 2026-09-23 10:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c9e1d70a25"
down_revision: str | Sequence[str] | None = "f18b5c2a9e33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("payee_rules", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "action",
                sa.String(length=16),
                nullable=False,
                server_default="map",
            )
        )
        batch_op.add_column(sa.Column("replacement", sa.String(length=300), nullable=True))
        batch_op.alter_column(
            "payee_id", existing_type=sa.String(length=32), nullable=True
        )


def downgrade() -> None:
    # A rewrite rule has no payee, so it cannot survive `payee_id NOT NULL`.
    # Deleting them is the only way down, and doing it here -- explicitly,
    # before the table is rebuilt -- is the difference between a documented
    # loss and a constraint violation halfway through a batch operation.
    op.execute(sa.text("DELETE FROM payee_rules WHERE action = 'rewrite'"))
    with op.batch_alter_table("payee_rules", schema=None) as batch_op:
        batch_op.alter_column(
            "payee_id", existing_type=sa.String(length=32), nullable=False
        )
        batch_op.drop_column("replacement")
        batch_op.drop_column("action")
