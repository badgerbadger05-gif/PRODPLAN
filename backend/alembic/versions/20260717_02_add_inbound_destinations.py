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
    bind = op.get_bind()
    offline = op.get_context().as_sql
    for table in ("production_products", "supplier_order_items"):
        inspector = None if offline else sa.inspect(bind)
        columns = (
            set()
            if inspector is None
            else {row["name"] for row in inspector.get_columns(table)}
        )
        if "destination_warehouse_ref1c" not in columns:
            op.add_column(
                table,
                sa.Column(
                    "destination_warehouse_ref1c",
                    sa.String(length=36),
                    nullable=True,
                ),
            )
        inspector = None if offline else sa.inspect(bind)
        indexes = (
            set()
            if inspector is None
            else {row["name"] for row in inspector.get_indexes(table)}
        )
        name = f"ix_{table}_destination_warehouse_ref1c"
        if name not in indexes:
            op.create_index(name, table, ["destination_warehouse_ref1c"])


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
