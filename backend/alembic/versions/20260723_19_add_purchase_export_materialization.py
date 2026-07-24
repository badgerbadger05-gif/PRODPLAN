"""Add durable purchase-export materialization tables.

Revision ID: 20260723_19
Revises: 20260723_18
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_19"
down_revision = "20260723_18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "purchase_export_batch",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("planning_read_snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="building"),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("request_payload", sa.JSON().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.Column("result_payload", sa.JSON().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('building', 'completed', 'failed', 'aborted')",
            name="ck_purchase_export_batch_status",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["planning_read_snapshot_id"], ["planning_read_snapshot.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_purchase_export_batch_idempotency_key"),
    )
    op.create_index(
        "ix_purchase_export_batch_ledger_generation_id",
        "purchase_export_batch",
        ["ledger_generation_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_export_batch_planning_read_snapshot_id",
        "purchase_export_batch",
        ["planning_read_snapshot_id"],
        unique=False,
    )

    op.create_table(
        "purchase_export_obligation_allocation",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("reservation_id", sa.BigInteger(), nullable=False),
        sa.Column("supplier_order_ref", sa.String(length=64), nullable=False),
        sa.Column("supplier_order_line_no", sa.String(length=32), nullable=False),
        sa.Column("line_token", sa.BigInteger(), nullable=True),
        sa.Column("line_hash", sa.String(length=64), nullable=True),
        sa.Column("allocated_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("planned_purchase_id", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "allocated_qty > 0",
            name="ck_purchase_export_obligation_allocation_qty_positive",
        ),
        sa.ForeignKeyConstraint([
            "batch_id"],
            ["purchase_export_batch.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint([
            "reservation_id"],
            ["reservation_entry.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["planned_purchase_id"], ["planned_purchase.purchase_id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "batch_id",
            "supplier_order_ref",
            "supplier_order_line_no",
            "reservation_id",
            name="uq_purchase_export_obligation_allocation",
        ),
    )
    op.create_index(
        "ix_purchase_export_obligation_allocation_batch_reservation",
        "purchase_export_obligation_allocation",
        ["batch_id", "reservation_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_export_obligation_allocation_batch_supplier_line",
        "purchase_export_obligation_allocation",
        ["batch_id", "supplier_order_ref", "supplier_order_line_no"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_export_obligation_allocation_planned_purchase_id",
        "purchase_export_obligation_allocation",
        ["planned_purchase_id", "batch_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_purchase_export_obligation_allocation_planned_purchase", table_name="purchase_export_obligation_allocation")
    op.drop_index("ix_purchase_export_obligation_allocation_batch_supplier_line", table_name="purchase_export_obligation_allocation")
    op.drop_index("ix_purchase_export_obligation_allocation_batch_reservation", table_name="purchase_export_obligation_allocation")
    op.drop_table("purchase_export_obligation_allocation")

    op.drop_index("ix_purchase_export_batch_planning_read_snapshot_id", table_name="purchase_export_batch")
    op.drop_index("ix_purchase_export_batch_ledger_generation_id", table_name="purchase_export_batch")
    op.drop_table("purchase_export_batch")
