"""add stock warehouses table

Revision ID: 20260416_01
Revises: 20260313_01
Create Date: 2026-04-16 12:00:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260416_01"
down_revision = "20260313_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_warehouses",
        sa.Column("warehouse_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_ref1c", sa.String(length=36), nullable=False),
        sa.Column("warehouse_code", sa.String(length=50), nullable=True),
        sa.Column("warehouse_name", sa.String(length=255), nullable=False),
        sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint("warehouse_id"),
    )
    op.create_index("ix_stock_warehouses_warehouse_id", "stock_warehouses", ["warehouse_id"], unique=False)
    op.create_index("ix_stock_warehouses_warehouse_ref1c", "stock_warehouses", ["warehouse_ref1c"], unique=True)
    op.create_index("ix_stock_warehouses_warehouse_code", "stock_warehouses", ["warehouse_code"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_stock_warehouses_warehouse_code", table_name="stock_warehouses")
    op.drop_index("ix_stock_warehouses_warehouse_ref1c", table_name="stock_warehouses")
    op.drop_index("ix_stock_warehouses_warehouse_id", table_name="stock_warehouses")
    op.drop_table("stock_warehouses")
