"""reports may convert through a household's exchange rates

Issue #396, phase 1. Three new tables and four household settings:

- `fx_rates`: published rates (the ECB's), shared by every household and
  fetched only when a member presses Fetch now. Not audited.
- `fx_manual_rates`: a household's own rates, typed or imported. Audited.
- `fx_fetches`: one row per source per press of Fetch now. Not audited,
  swept after ninety days.
- `households.fx_chain`, `fx_stale_days`, `fx_monthly_policy`,
  `fx_show_own_rates`: where rates come from and how they are used. Every
  existing household gets the defaults: manual rates, then the ECB; stale
  after seven days; monthly average; own rates shown.

The tables start empty. Nothing in the ledger changes, and nothing converts
until a report asks to.

Reversible: lossy -- downgrading drops every exchange rate, the fetch history and the four rate settings; fetched rates can be fetched again, rates entered by hand are lost. Nothing in the ledger changes.

Revision ID: 3b6f0d2e9a41
Revises: d2b8e6f41c07
Create Date: 2026-11-09 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b6f0d2e9a41"
down_revision: str | Sequence[str] | None = "d2b8e6f41c07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The server defaults are what fill the existing households.
    with op.batch_alter_table("households", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("fx_chain", sa.String(length=64), nullable=False, server_default="manual,ecb")
        )
        batch_op.add_column(
            sa.Column("fx_stale_days", sa.Integer(), nullable=False, server_default="7")
        )
        batch_op.add_column(
            sa.Column(
                "fx_monthly_policy", sa.String(length=16), nullable=False, server_default="month_avg"
            )
        )
        batch_op.add_column(
            sa.Column("fx_show_own_rates", sa.Boolean(), nullable=False, server_default=sa.true())
        )

    # Enums rendered as the sa.String they are stored as: a migration carries
    # no application import.
    op.create_table(
        "fx_rates",
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("rate", sa.String(length=40), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fx_rates")),
        sa.UniqueConstraint(
            "source", "quote_currency", "currency", "date", name="uq_fx_rates_key"
        ),
    )
    with op.batch_alter_table("fx_rates", schema=None) as batch_op:
        batch_op.create_index("ix_fx_rates_currency_date", ["currency", "date"], unique=False)

    op.create_table(
        "fx_manual_rates",
        sa.Column("household_id", sa.String(length=32), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("rate", sa.String(length=40), nullable=False),
        sa.Column("created_by_id", sa.String(length=32), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_fx_manual_rates_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_fx_manual_rates_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fx_manual_rates")),
        sa.UniqueConstraint(
            "household_id",
            "quote_currency",
            "currency",
            "date",
            "period",
            name="uq_fx_manual_rates_key",
        ),
    )
    with op.batch_alter_table("fx_manual_rates", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_fx_manual_rates_household_id"), ["household_id"], unique=False
        )

    op.create_table(
        "fx_fetches",
        sa.Column("household_id", sa.String(length=32), nullable=False),
        sa.Column("started_by_id", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("requested_since", sa.Date(), nullable=False),
        sa.Column("requested_until", sa.Date(), nullable=False),
        sa.Column("currencies", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("rates_added", sa.Integer(), nullable=False),
        sa.Column("rates_unchanged", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name=op.f("fk_fx_fetches_household_id_households"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["started_by_id"],
            ["users.id"],
            name=op.f("fk_fx_fetches_started_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fx_fetches")),
    )
    with op.batch_alter_table("fx_fetches", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_fx_fetches_household_id"), ["household_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_fx_fetches_started_at"), ["started_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("fx_fetches", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_fx_fetches_started_at"))
        batch_op.drop_index(batch_op.f("ix_fx_fetches_household_id"))
    op.drop_table("fx_fetches")

    with op.batch_alter_table("fx_manual_rates", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_fx_manual_rates_household_id"))
    op.drop_table("fx_manual_rates")

    with op.batch_alter_table("fx_rates", schema=None) as batch_op:
        batch_op.drop_index("ix_fx_rates_currency_date")
    op.drop_table("fx_rates")

    with op.batch_alter_table("households", schema=None) as batch_op:
        batch_op.drop_column("fx_show_own_rates")
        batch_op.drop_column("fx_monthly_policy")
        batch_op.drop_column("fx_stale_days")
        batch_op.drop_column("fx_chain")
