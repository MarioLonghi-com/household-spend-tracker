"""a category group and a category carry a role

Issue #372. Income vs Expense decides the side a row lands on by its category
group -- the Income group is income, every other category is an expense line
where refunds and charges net -- and shows the Reimbursement category in a
block of its own, out of income, expenses and net. Both were recognisable only
by name until now, and a name is the household's to rename or translate, so
each gets a marker: `category_groups.role` (`income`) and `categories.role`
(`reimbursement`), one of each per household -- the app's rule, kept where a
person moves it, rather than an index.

The backfill marks what it can prove from the name and nothing else:

- the group called `Income` -- exactly the seeded word, and the only one ever
  seeded, since no translated catalog has been reviewed -- or, failing that,
  one whose name is `income` in any case;
- a category called `Reimbursement` or `Reimbursements`, in any case,
  preferring one inside the group just marked.

A household that renamed either before this ran is left unmarked: its report
says it has no Income group, and Categories is where a person marks one.

Reversible: lossy -- downgrading drops both columns, so which group is Income and which category is Reimbursement (as moved on Categories) is forgotten; no transaction changes.

Revision ID: a7e3c1d95b20
Revises: c4d9e2a7b318
Create Date: 2026-11-09 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7e3c1d95b20"
down_revision: str | Sequence[str] | None = "c4d9e2a7b318"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Rendered as the sa.String it actually is, not as EnumStr: a migration
    # carries no application import.
    op.add_column("category_groups", sa.Column("role", sa.String(length=16), nullable=True))
    op.add_column("categories", sa.Column("role", sa.String(length=16), nullable=True))

    bind = op.get_bind()

    # One pick per household, chosen here rather than by an UPDATE ... WHERE
    # name, because two candidates in one household ("Income" and "income")
    # would both match, and one of each is the rule.
    income_group: dict[str, str] = {}
    for group_id, household_id, name in bind.execute(
        sa.text(
            "SELECT id, household_id, name FROM category_groups "
            "WHERE lower(name) = 'income' ORDER BY household_id, id"
        )
    ).all():
        held = income_group.get(household_id)
        if held is None or name == "Income":
            income_group[household_id] = group_id
    for group_id in income_group.values():
        bind.execute(
            sa.text("UPDATE category_groups SET role = 'income' WHERE id = :id"),
            {"id": group_id},
        )

    reimbursement: dict[str, tuple[bool, str]] = {}
    for category_id, household_id, group_id in bind.execute(
        sa.text(
            "SELECT id, household_id, group_id FROM categories "
            "WHERE lower(name) IN ('reimbursement', 'reimbursements') "
            "ORDER BY household_id, id"
        )
    ).all():
        inside = income_group.get(household_id) == group_id
        held = reimbursement.get(household_id)
        if held is None or (inside and not held[0]):
            reimbursement[household_id] = (inside, category_id)
    for _inside, category_id in reimbursement.values():
        bind.execute(
            sa.text("UPDATE categories SET role = 'reimbursement' WHERE id = :id"),
            {"id": category_id},
        )


def downgrade() -> None:
    op.drop_column("categories", "role")
    op.drop_column("category_groups", "role")
