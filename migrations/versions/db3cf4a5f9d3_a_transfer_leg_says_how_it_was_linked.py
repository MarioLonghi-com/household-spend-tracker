"""a transfer leg says how it was linked, and a pair can be said not to be one

Issue #131. Auto-linking made wrong links on a real ledger, and each one made
the next: "these two accounts have had transfers linked between them before"
counted every past link, including ones made only because the accounts had
been linked before. So each leg now says how it was linked
(`transactions.link_source`: named, person, history, import), and only links
something other than history vouched for count as history.

And an unlink was not remembered: the next sweep offered every pair a person
had just unlinked straight back as "Sure of these". `transfer_rejections`
holds the pairs a person has said are not one transfer, and the matcher never
offers them again.

**The backfill is honest about what it can know.** Nothing recorded how an
existing link was made:

- both legs with no `import_id` -- rows typed into the register, which only a
  person could have made a transfer of -- become `person`;
- every other existing leg becomes `import`: linked at an import or on the
  Transfers screen, which the ledger cannot now tell apart. An `import` link
  counts as history only while one of its rows names the other account, which
  the matcher checks as it reads it; one that does not is listed under
  "Linked by history only" on the Transfers screen for a person to keep or
  unlink.

No rejection is invented: the table starts empty.

Reversible: lossy -- downgrading drops how each link was made and every pair marked not a transfer; the links themselves stay.

Revision ID: db3cf4a5f9d3
Revises: c3e8a1f05d72
Create Date: 2026-09-24 20:28:15.226924
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "db3cf4a5f9d3"
down_revision: str | Sequence[str] | None = "c3e8a1f05d72"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transfer_rejections",
        sa.Column("household_id", sa.String(length=32), nullable=False),
        sa.Column("out_transaction_id", sa.String(length=32), nullable=False),
        sa.Column("in_transaction_id", sa.String(length=32), nullable=False),
        sa.Column("rejected_by_id", sa.String(length=32), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_transfer_rejections_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by_id"],
            ["users.id"],
            name=op.f("fk_transfer_rejections_rejected_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transfer_rejections")),
        sa.UniqueConstraint(
            "household_id",
            "out_transaction_id",
            "in_transaction_id",
            name="uq_transfer_rejections_pair",
        ),
    )
    with op.batch_alter_table("transfer_rejections", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_transfer_rejections_household_id"), ["household_id"], unique=False
        )

    # Rendered as the sa.String it actually is, not as EnumStr: a migration
    # carries no application import.
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("link_source", sa.String(length=8), nullable=True))

    # Typed in by hand on both sides: only a person makes those a transfer.
    op.execute(
        sa.text(
            "UPDATE transactions SET link_source = 'person' "
            "WHERE (transfer_transaction_id IS NOT NULL OR transfer_account_id IS NOT NULL) "
            "AND import_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM transactions AS other "
            "WHERE other.id = transactions.transfer_transaction_id "
            "AND other.import_id IS NOT NULL)"
        )
    )
    # Everything else: made at an import or on the Transfers screen, how is
    # not recorded anywhere. See the module docstring.
    op.execute(
        sa.text(
            "UPDATE transactions SET link_source = 'import' "
            "WHERE (transfer_transaction_id IS NOT NULL OR transfer_account_id IS NOT NULL) "
            "AND link_source IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_column("link_source")

    with op.batch_alter_table("transfer_rejections", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_transfer_rejections_household_id"))

    op.drop_table("transfer_rejections")
