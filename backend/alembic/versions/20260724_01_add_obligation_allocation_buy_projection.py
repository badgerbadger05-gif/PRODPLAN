"""Add immutable BUY projection fields for export obligation allocations.

Revision ID: 20260724_01
Revises: 20260723_19
"""

from alembic import op
import sqlalchemy as sa


revision = "20260724_01"
down_revision = "20260723_19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "purchase_export_obligation_allocation",
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_purchase_export_obligation_allocation_ledger_generation_id",
        "purchase_export_obligation_allocation",
        ["ledger_generation_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_purchase_export_obligation_allocation_ledger_generation_id",
        "purchase_export_obligation_allocation",
        "ledger_generation",
        ["ledger_generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "purchase_export_obligation_allocation",
        sa.Column("item_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_purchase_export_obligation_allocation_item_id",
        "purchase_export_obligation_allocation",
        ["item_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_purchase_export_obligation_allocation_item_id",
        "purchase_export_obligation_allocation",
        "items",
        ["item_id"],
        ["item_id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "purchase_export_obligation_allocation",
        sa.Column("planning_stock_pool", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_purchase_export_obligation_allocation_planning_stock_pool",
        "purchase_export_obligation_allocation",
        ["planning_stock_pool"],
        unique=False,
    )

    op.add_column(
        "purchase_export_obligation_allocation",
        sa.Column("destination_warehouse_ref1c", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_purchase_export_obligation_alloc_destination_wh",
        "purchase_export_obligation_allocation",
        ["destination_warehouse_ref1c"],
        unique=False,
    )

    op.add_column(
        "purchase_export_obligation_allocation",
        sa.Column("eta_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("purchase_export_obligation_allocation", "eta_date")
    op.drop_index(
        "ix_purchase_export_obligation_alloc_destination_wh",
        table_name="purchase_export_obligation_allocation",
    )
    op.drop_column(
        "purchase_export_obligation_allocation",
        "destination_warehouse_ref1c",
    )
    op.drop_index(
        "ix_purchase_export_obligation_allocation_planning_stock_pool",
        table_name="purchase_export_obligation_allocation",
    )
    op.drop_column(
        "purchase_export_obligation_allocation",
        "planning_stock_pool",
    )
    op.drop_constraint(
        "fk_purchase_export_obligation_allocation_item_id",
        "purchase_export_obligation_allocation",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_purchase_export_obligation_allocation_item_id",
        table_name="purchase_export_obligation_allocation",
    )
    op.drop_column(
        "purchase_export_obligation_allocation",
        "item_id",
    )
    op.drop_constraint(
        "fk_purchase_export_obligation_allocation_ledger_generation_id",
        "purchase_export_obligation_allocation",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_purchase_export_obligation_allocation_ledger_generation_id",
        table_name="purchase_export_obligation_allocation",
    )
    op.drop_column("purchase_export_obligation_allocation", "ledger_generation_id")
