"""Add generation-scoped immutable future-supply snapshots.

Revision ID: 20260723_14
Revises: 20260723_13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_14"
down_revision = "20260723_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ledger_future_supply",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("supply_kind", sa.String(length=32), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("characteristic_ref", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("organization_ref", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("planning_stock_pool", sa.String(length=128), nullable=False),
        sa.Column("destination_warehouse_ref1c", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("source_ref", sa.String(length=64), nullable=True),
        sa.Column("source_line_ref", sa.String(length=64), nullable=True),
        sa.Column("source_local_id", sa.String(length=128), nullable=True),
        sa.Column("ordered_qty_at_cutoff", sa.DECIMAL(precision=15, scale=3), nullable=False),
        sa.Column("realized_qty_at_cutoff", sa.DECIMAL(precision=15, scale=3), nullable=False),
        sa.Column("open_qty_at_cutoff", sa.DECIMAL(precision=15, scale=3), nullable=False),
        sa.Column("eta_date", sa.Date(), nullable=True),
        sa.Column("source_state_key", sa.String(length=64), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capture_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("capture_batch_id", sa.BigInteger(), nullable=False),
        sa.Column("evidence_status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "supply_kind IN ('wip_order', 'supplier_order')",
            name="ck_ledger_future_supply_kind",
        ),
        sa.CheckConstraint(
            "evidence_status IN ('exact', 'ambiguous', 'unmatched', 'rejected')",
            name="ck_ledger_future_supply_evidence_status",
        ),
        sa.CheckConstraint(
            "ordered_qty_at_cutoff >= 0 AND realized_qty_at_cutoff >= 0 "
            "AND open_qty_at_cutoff >= 0",
            name="ck_ledger_future_supply_quantities_nonnegative",
        ),
        sa.CheckConstraint(
            "capture_cutoff IS NOT NULL",
            name="ck_ledger_future_supply_capture_cutoff",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
        sa.ForeignKeyConstraint(
            ["capture_batch_id"], ["ledger_build_batch.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ledger_generation_id", "supply_kind", "source_ref", "source_line_ref",
            name="uq_ledger_future_supply_generation_source_line",
        ),
    )
    op.create_index(
        "ix_ledger_future_supply_ledger_generation_id", "ledger_future_supply",
        ["ledger_generation_id"], unique=False,
    )
    op.create_index(
        "ix_ledger_future_supply_item_id", "ledger_future_supply", ["item_id"], unique=False,
    )
    op.create_index(
        "ix_ledger_future_supply_capture_batch_id", "ledger_future_supply",
        ["capture_batch_id"], unique=False,
    )
    op.create_index(
        "ix_ledger_future_supply_generation_kind_item_eta", "ledger_future_supply",
        ["ledger_generation_id", "supply_kind", "item_id", "eta_date"], unique=False,
    )
    op.create_index(
        "ix_ledger_future_supply_generation_item_eta", "ledger_future_supply",
        ["ledger_generation_id", "item_id", "eta_date"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ledger_future_supply_generation_item_eta", table_name="ledger_future_supply")
    op.drop_index("ix_ledger_future_supply_generation_kind_item_eta", table_name="ledger_future_supply")
    op.drop_index("ix_ledger_future_supply_capture_batch_id", table_name="ledger_future_supply")
    op.drop_index("ix_ledger_future_supply_item_id", table_name="ledger_future_supply")
    op.drop_index("ix_ledger_future_supply_ledger_generation_id", table_name="ledger_future_supply")
    op.drop_table("ledger_future_supply")
