"""add 1C operation contract to supplier orders

Revision ID: 20260720_01
Revises: 20260719_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260720_01"
down_revision = "20260719_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)
    columns = (
        set()
        if inspector is None
        else {row["name"] for row in inspector.get_columns("supplier_orders")}
    )
    for name, length in (
        ("operation_key", 36),
        ("operation_name", 100),
    ):
        if inspector is None or name not in columns:
            op.add_column(
                "supplier_orders",
                sa.Column(name, sa.String(length=length), nullable=True),
            )
        indexes = (
            set()
            if inspector is None
            else {
                row["name"]
                for row in sa.inspect(bind).get_indexes("supplier_orders")
            }
        )
        index_name = f"ix_supplier_orders_{name}"
        if inspector is None or index_name not in indexes:
            op.create_index(index_name, "supplier_orders", [name])
    columns = (
        set()
        if inspector is None
        else {row["name"] for row in sa.inspect(bind).get_columns("supplier_orders")}
    )
    for name in ("processing_transfer_date", "processing_report_date"):
        if inspector is None or name not in columns:
            op.add_column(
                "supplier_orders",
                sa.Column(name, sa.DateTime(), nullable=True),
            )


def downgrade() -> None:
    for name in ("processing_report_date", "processing_transfer_date"):
        op.drop_column("supplier_orders", name)
    for name in ("operation_name", "operation_key"):
        op.drop_index(f"ix_supplier_orders_{name}", table_name="supplier_orders")
        op.drop_column("supplier_orders", name)
