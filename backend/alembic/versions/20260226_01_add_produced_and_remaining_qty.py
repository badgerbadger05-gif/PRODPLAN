"""Add produced_qty and remaining_qty to production_products

Revision ID: 20260226_01
Revises: 20260213_01
Create Date: 2026-02-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '20260226_01'
down_revision: Union[str, None] = '20260213_01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add produced_qty column with default 0
    op.add_column('production_products', sa.Column('produced_qty', sa.NUMERIC(precision=10, scale=3), nullable=False, server_default='0.0'))
    
    # Add remaining_qty column (will be updated below)
    op.add_column('production_products', sa.Column('remaining_qty', sa.NUMERIC(precision=10, scale=3), nullable=False, server_default='0.0'))
    
    # Initialize remaining_qty = quantity (since produced_qty = 0 for all existing records)
    op.execute("""
        UPDATE production_products 
        SET remaining_qty = quantity
    """)
    
    # Remove server defaults after initialization
    op.alter_column('production_products', 'produced_qty', server_default=None)
    op.alter_column('production_products', 'remaining_qty', server_default=None)


def downgrade() -> None:
    op.drop_column('production_products', 'remaining_qty')
    op.drop_column('production_products', 'produced_qty')
