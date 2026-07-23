"""Bind planning exports and 1C line keys to accepted Ledger truth.

Revision ID: 20260723_13
Revises: 20260723_12
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_13"
down_revision = "20260723_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sync_link", sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_sync_link_ledger_generation", "sync_link", "ledger_generation",
        ["ledger_generation_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index("ix_sync_link_ledger_generation_id", "sync_link", ["ledger_generation_id"], unique=False)

    table = "purchase_export_line_allocation"
    op.add_column(table, sa.Column("request_line_token", sa.BigInteger(), nullable=True))
    op.add_column(table, sa.Column("export_line_payload_hash", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_purchase_export_line_allocation_request_line_token", table,
        ["request_line_token"], unique=False,
    )
    op.create_unique_constraint(
        "uq_purchase_export_line_allocation_token", table,
        ["ledger_generation_id", "supplier_order_ref", "request_line_token", "planned_purchase_id"],
    )


def downgrade() -> None:
    table = "purchase_export_line_allocation"
    op.drop_constraint("uq_purchase_export_line_allocation_token", table, type_="unique")
    op.drop_index("ix_purchase_export_line_allocation_request_line_token", table_name=table)
    op.drop_column(table, "export_line_payload_hash")
    op.drop_column(table, "request_line_token")
    op.drop_index("ix_sync_link_ledger_generation_id", table_name="sync_link")
    op.drop_constraint("fk_sync_link_ledger_generation", "sync_link", type_="foreignkey")
    op.drop_column("sync_link", "ledger_generation_id")
