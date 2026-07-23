"""Add exact 1C processing-stock snapshot and sync health.

Revision ID: 20260723_02
Revises: 20260723_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_02"
down_revision = "20260723_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("processing_contractor_stock"):
        op.create_table(
            "processing_contractor_stock",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("contractor_ref1c", sa.String(36), nullable=False, server_default=""),
            sa.Column("order_ref1c", sa.String(36), nullable=False, server_default=""),
            sa.Column("order_type", sa.String(255), nullable=False, server_default=""),
            sa.Column("transfer_type", sa.String(255), nullable=False, server_default=""),
            sa.Column("qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("synced_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"], ondelete="CASCADE"),
            sa.UniqueConstraint(
                "item_id",
                "contractor_ref1c",
                "order_ref1c",
                "order_type",
                "transfer_type",
                name="uq_processing_contractor_stock_axis",
            ),
            sa.CheckConstraint("qty >= 0", name="ck_processing_contractor_stock_qty_nonnegative"),
        )

    inspector = sa.inspect(op.get_bind())
    index_names = {
        row["name"] for row in inspector.get_indexes("processing_contractor_stock")
    }
    for name, columns in (
        ("ix_processing_contractor_stock_item_id", ["item_id"]),
        ("ix_processing_contractor_stock_contractor_ref1c", ["contractor_ref1c"]),
        ("ix_processing_contractor_stock_order_ref1c", ["order_ref1c"]),
    ):
        if name not in index_names:
            op.create_index(name, "processing_contractor_stock", columns)

    if not inspector.has_table("processing_stock_sync_state"):
        op.create_table(
            "processing_stock_sync_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="never"),
            sa.Column("last_attempt_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("last_success_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("rows_seen", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rows_stored", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unmatched_items", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.TEXT(), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("processing_stock_sync_state"):
        op.drop_table("processing_stock_sync_state")
    if inspector.has_table("processing_contractor_stock"):
        op.drop_table("processing_contractor_stock")
