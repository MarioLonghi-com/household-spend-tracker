"""categories

Schema only. Existing households get an empty category list rather than the
starter tree: seeding is a decision the application makes when a household is
created, and a migration that invented four groups and twenty categories in
somebody's ledger would be putting data there they never asked for. The
categories screen offers to add the starter set instead.

`payees.categorisation` arrives with a server default so a NOT NULL column can
land on a table that already has rows; the default is then dropped, because the
model is what decides a new payee's behaviour.

Reversible: lossy -- every category, every group, and every transaction's category.

Revision ID: 9dc3906a0961
Revises: 89742b01b2cc
Create Date: 2026-09-19 01:00:27.805571

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9dc3906a0961'
down_revision: str | Sequence[str] | None = '89742b01b2cc'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    
    op.create_table(
        'category_groups',
    sa.Column('household_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['household_id'], ['households.id'], name=op.f('fk_category_groups_household_id_households'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_category_groups')),
    sa.UniqueConstraint('household_id', 'name', name='uq_category_groups_household_name')
    )
    with op.batch_alter_table('category_groups', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_category_groups_household_id'), ['household_id'], unique=False)

    op.create_table('categories',
    sa.Column('household_id', sa.String(length=32), nullable=False),
    sa.Column('group_id', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('sort_order', sa.Integer(), nullable=False),
    sa.Column('archived', sa.Boolean(), nullable=False),
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['group_id'], ['category_groups.id'], name=op.f('fk_categories_group_id_category_groups'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['household_id'], ['households.id'], name=op.f('fk_categories_household_id_households'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_categories')),
    sa.UniqueConstraint('household_id', 'name', name='uq_categories_household_name')
    )
    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_categories_group_id'), ['group_id'], unique=False)
        batch_op.create_index('ix_categories_household_group', ['household_id', 'group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_categories_household_id'), ['household_id'], unique=False)

    with op.batch_alter_table('payees', schema=None) as batch_op:
        batch_op.add_column(sa.Column('categorisation', sa.String(length=8), nullable=False, server_default='history'))
        batch_op.add_column(sa.Column('default_category_id', sa.String(length=32), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_payees_default_category_id_categories'), 'categories', ['default_category_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('category_id', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_transactions_category_id'), ['category_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_transactions_category_id_categories'), 'categories', ['category_id'], ['id'], ondelete='SET NULL')



    with op.batch_alter_table('payees', schema=None) as batch_op:
        batch_op.alter_column('categorisation', server_default=None)


def downgrade() -> None:
    
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_transactions_category_id_categories'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_transactions_category_id'))
        batch_op.drop_column('category_id')

    with op.batch_alter_table('payees', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_payees_default_category_id_categories'), type_='foreignkey')
        batch_op.drop_column('default_category_id')
        batch_op.drop_column('categorisation')

    with op.batch_alter_table('categories', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_categories_household_id'))
        batch_op.drop_index('ix_categories_household_group')
        batch_op.drop_index(batch_op.f('ix_categories_group_id'))

    op.drop_table('categories')
    with op.batch_alter_table('category_groups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_category_groups_household_id'))

    op.drop_table('category_groups')
