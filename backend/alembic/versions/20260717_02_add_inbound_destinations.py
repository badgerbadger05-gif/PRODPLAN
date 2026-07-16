"""add exact destination warehouses to open inbound lines

Revision ID: 20260717_02
Revises: 20260717_01
Create Date: 2026-07-17 18:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260717_02"
down_revision = "20260717_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "production_products",
        sa.Column("destination_warehouse_ref1c", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_production_products_destination_warehouse_ref1c",
        "production_products",
        ["destination_warehouse_ref1c"],
    )
    op.add_column(
        "supplier_order_items",
        sa.Column("destination_warehouse_ref1c", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_supplier_order_items_destination_warehouse_ref1c",
        "supplier_order_items",
        ["destination_warehouse_ref1c"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_supplier_order_items_destination_warehouse_ref1c",
        table_name="supplier_order_items",
    )
    op.drop_column("supplier_order_items", "destination_warehouse_ref1c")
    op.drop_index(
        "ix_production_products_destination_warehouse_ref1c",
        table_name="production_products",
    )
    op.drop_column("production_products", "destination_warehouse_ref1c")
