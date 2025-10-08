"""add buffer_days to production_resources and optimal_batch to items

Revision ID: 20251007_06
Revises: 20251002_05
Create Date: 2025-10-07 06:00:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20251007_06'
down_revision = '20251002_05'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # items.optimal_batch NUMERIC(15,3) NULL
    op.add_column(
        'items',
        sa.Column('optimal_batch', sa.Numeric(15, 3), nullable=True)
    )
    # production_resources.buffer_days INT NOT NULL DEFAULT 0
    op.add_column(
        'production_resources',
        sa.Column('buffer_days', sa.Integer(), nullable=False, server_default='0')
    )
    # Optionally drop server_default to keep explicit values only
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE production_resources ALTER COLUMN buffer_days DROP DEFAULT")


def downgrade() -> None:
    op.drop_column('production_resources', 'buffer_days')
    op.drop_column('items', 'optimal_batch')