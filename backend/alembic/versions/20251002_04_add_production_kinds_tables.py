"""add production kinds tables

Revision ID: 20251002_04
Revises: 20250929_03
Create Date: 2025-10-02 08:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20251002_04'
down_revision = '20250929_03'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create production_kinds table
    op.create_table(
        'production_kinds',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ref_1c', sa.String(255), nullable=False, unique=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    )
    op.create_index('ix_production_kinds_ref_1c', 'production_kinds', ['ref_1c'])
    op.create_index('ix_production_kinds_name', 'production_kinds', ['name'])

    # 2. Create resource_production_kinds table
    op.create_table(
        'resource_production_kinds',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('resource_id', sa.Integer(), nullable=False),
        sa.Column('production_kind_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    )
    op.create_foreign_key(
        'fk_resource_production_kinds_resource_id',
        'resource_production_kinds',
        'production_resources',
        ['resource_id'],
        ['resource_id']
    )
    op.create_foreign_key(
        'fk_resource_production_kinds_production_kind_id',
        'resource_production_kinds',
        'production_kinds',
        ['production_kind_id'],
        ['id']
    )
    op.create_unique_constraint('uq_resource_production_kinds', 'resource_production_kinds', ['resource_id', 'production_kind_id'])
    op.create_index('ix_resource_production_kinds_resource', 'resource_production_kinds', ['resource_id'])
    op.create_index('ix_resource_production_kinds_kind', 'resource_production_kinds', ['production_kind_id'])

    # 3. Add production_kind_id column to specifications table
    op.add_column('specifications', sa.Column('production_kind_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_specifications_production_kind',
        'specifications',
        'production_kinds',
        ['production_kind_id'],
        ['id']
    )
    op.create_index('ix_specifications_production_kind_id', 'specifications', ['production_kind_id'])


def downgrade() -> None:
    # Reverse operations in proper order
    op.drop_constraint('fk_specifications_production_kind', 'specifications', type_='foreignkey')
    op.drop_index('ix_specifications_production_kind_id', table_name='specifications')
    op.drop_column('specifications', 'production_kind_id')

    op.drop_constraint('uq_resource_production_kinds', 'resource_production_kinds', type_='unique')
    op.drop_constraint('fk_resource_production_kinds_production_kind_id', 'resource_production_kinds', type_='foreignkey')
    op.drop_constraint('fk_resource_production_kinds_resource_id', 'resource_production_kinds', type_='foreignkey')
    op.drop_index('ix_resource_production_kinds_kind', table_name='resource_production_kinds')
    op.drop_index('ix_resource_production_kinds_resource', table_name='resource_production_kinds')
    op.drop_table('resource_production_kinds')

    op.drop_index('ix_production_kinds_name', table_name='production_kinds')
    op.drop_index('ix_production_kinds_ref_1c', table_name='production_kinds')
    op.drop_table('production_kinds')