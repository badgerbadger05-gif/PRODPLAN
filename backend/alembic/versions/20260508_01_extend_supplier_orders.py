"""extend supplier orders for MRP coverage

Revision ID: 20260508_01
Revises: 20260416_01
Create Date: 2026-05-08 12:00:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260508_01"
down_revision = "20260416_01"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    if not _has_table(inspector, table_name):
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "suppliers"):
        op.create_table(
            "suppliers",
            sa.Column("supplier_id", sa.Integer(), nullable=False),
            sa.Column("supplier_ref1c", sa.String(length=36), nullable=True),
            sa.Column("supplier_name", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
            sa.PrimaryKeyConstraint("supplier_id"),
        )
        op.create_index("ix_suppliers_supplier_id", "suppliers", ["supplier_id"], unique=False)
        op.create_index("ix_suppliers_supplier_ref1c", "suppliers", ["supplier_ref1c"], unique=True)

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "supplier_orders"):
        op.create_table(
            "supplier_orders",
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("order_number", sa.String(length=50), nullable=True),
            sa.Column("order_date", sa.DateTime(), nullable=False),
            sa.Column("order_ref1c", sa.String(length=36), nullable=True),
            sa.Column("supplier_id", sa.Integer(), nullable=True),
            sa.Column("document_amount", sa.DECIMAL(10, 2), nullable=True),
            sa.Column("is_posted", sa.Boolean(), nullable=True),
            sa.Column("order_state_key", sa.String(length=36), nullable=True),
            sa.Column("order_state_name", sa.String(length=255), nullable=True),
            sa.Column("deletion_mark", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
            sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.supplier_id"]),
            sa.PrimaryKeyConstraint("order_id"),
        )
        op.create_index("ix_supplier_orders_order_id", "supplier_orders", ["order_id"], unique=False)
        op.create_index("ix_supplier_orders_order_number", "supplier_orders", ["order_number"], unique=False)
        op.create_index("ix_supplier_orders_order_ref1c", "supplier_orders", ["order_ref1c"], unique=True)
        op.create_index("ix_supplier_orders_order_state_key", "supplier_orders", ["order_state_key"], unique=False)
        op.create_index("ix_supplier_orders_deletion_mark", "supplier_orders", ["deletion_mark"], unique=False)
    else:
        if not _has_column(inspector, "supplier_orders", "order_state_key"):
            op.add_column("supplier_orders", sa.Column("order_state_key", sa.String(length=36), nullable=True))
            op.create_index("ix_supplier_orders_order_state_key", "supplier_orders", ["order_state_key"], unique=False)
        if not _has_column(inspector, "supplier_orders", "order_state_name"):
            op.add_column("supplier_orders", sa.Column("order_state_name", sa.String(length=255), nullable=True))
        if not _has_column(inspector, "supplier_orders", "deletion_mark"):
            op.add_column(
                "supplier_orders",
                sa.Column("deletion_mark", sa.Boolean(), nullable=False, server_default=sa.false()),
            )
            op.create_index("ix_supplier_orders_deletion_mark", "supplier_orders", ["deletion_mark"], unique=False)

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "supplier_order_items"):
        op.create_table(
            "supplier_order_items",
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("item_id_ref", sa.Integer(), nullable=False),
            sa.Column("line_number", sa.Integer(), nullable=True),
            sa.Column("characteristic_ref1c", sa.String(length=36), nullable=True),
            sa.Column("quantity", sa.DECIMAL(10, 3), nullable=False),
            sa.Column("received_qty", sa.DECIMAL(10, 3), nullable=False, server_default="0"),
            sa.Column("remaining_qty", sa.DECIMAL(10, 3), nullable=False, server_default="0"),
            sa.Column("price", sa.DECIMAL(10, 2), nullable=True),
            sa.Column("amount", sa.DECIMAL(10, 2), nullable=True),
            sa.Column("delivery_date", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
            sa.ForeignKeyConstraint(["item_id_ref"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["order_id"], ["supplier_orders.order_id"]),
            sa.PrimaryKeyConstraint("item_id"),
        )
        op.create_index("ix_supplier_order_items_item_id", "supplier_order_items", ["item_id"], unique=False)
        op.create_index("ix_supplier_order_items_line_number", "supplier_order_items", ["line_number"], unique=False)
    else:
        if not _has_column(inspector, "supplier_order_items", "line_number"):
            op.add_column("supplier_order_items", sa.Column("line_number", sa.Integer(), nullable=True))
            op.create_index("ix_supplier_order_items_line_number", "supplier_order_items", ["line_number"], unique=False)
        if not _has_column(inspector, "supplier_order_items", "characteristic_ref1c"):
            op.add_column("supplier_order_items", sa.Column("characteristic_ref1c", sa.String(length=36), nullable=True))
        if not _has_column(inspector, "supplier_order_items", "received_qty"):
            op.add_column(
                "supplier_order_items",
                sa.Column("received_qty", sa.DECIMAL(10, 3), nullable=False, server_default="0"),
            )
        if not _has_column(inspector, "supplier_order_items", "remaining_qty"):
            op.add_column(
                "supplier_order_items",
                sa.Column("remaining_qty", sa.DECIMAL(10, 3), nullable=True),
            )
            op.execute("UPDATE supplier_order_items SET remaining_qty = COALESCE(quantity, 0)")
            op.alter_column("supplier_order_items", "remaining_qty", nullable=False, server_default="0")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "supplier_order_items"):
        for index_name in ("ix_supplier_order_items_line_number",):
            try:
                op.drop_index(index_name, table_name="supplier_order_items")
            except Exception:
                pass
        for column_name in ("remaining_qty", "received_qty", "characteristic_ref1c", "line_number"):
            if _has_column(inspector, "supplier_order_items", column_name):
                op.drop_column("supplier_order_items", column_name)

    inspector = sa.inspect(bind)
    if _has_table(inspector, "supplier_orders"):
        for index_name in ("ix_supplier_orders_deletion_mark", "ix_supplier_orders_order_state_key"):
            try:
                op.drop_index(index_name, table_name="supplier_orders")
            except Exception:
                pass
        for column_name in ("deletion_mark", "order_state_name", "order_state_key"):
            if _has_column(inspector, "supplier_orders", column_name):
                op.drop_column("supplier_orders", column_name)
