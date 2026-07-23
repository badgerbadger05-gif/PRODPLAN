"""Add exact supplier receipt and purchase export lineage.

Revision ID: 20260723_11
Revises: 20260723_10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_11"
down_revision = "20260723_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table_name in ("planned_order", "planned_rework"):
        op.add_column(
            table_name,
            sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table_name}_ledger_generation",
            table_name,
            "ledger_generation",
            ["ledger_generation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            f"ix_{table_name}_ledger_generation_id",
            table_name,
            ["ledger_generation_id"],
            unique=False,
        )

    op.create_table(
        "purchase_export_line_allocation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("supplier_order_ref", sa.String(length=64), nullable=False),
        sa.Column("supplier_order_line_no", sa.String(length=32), nullable=False),
        sa.Column("planned_purchase_id", sa.Integer(), nullable=False),
        sa.Column("allocated_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "allocated_qty > 0",
            name="ck_purchase_export_line_allocation_qty_positive",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_generation_id"],
            ["ledger_generation.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["planned_purchase_id"],
            ["planned_purchase.purchase_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ledger_generation_id",
            "supplier_order_ref",
            "supplier_order_line_no",
            "planned_purchase_id",
            name="uq_purchase_export_line_allocation",
        ),
    )
    op.create_index(
        "ix_purchase_export_line_allocation_ledger_generation_id",
        "purchase_export_line_allocation",
        ["ledger_generation_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_export_line_allocation_planned_purchase_id",
        "purchase_export_line_allocation",
        ["planned_purchase_id"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_export_line_order",
        "purchase_export_line_allocation",
        ["supplier_order_ref", "supplier_order_line_no"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_export_line_generation_order",
        "purchase_export_line_allocation",
        ["ledger_generation_id", "supplier_order_ref", "supplier_order_line_no"],
        unique=False,
    )

    op.create_table(
        "stock_ledger_supplier_receipt_provenance",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("stock_ledger_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("receipt_doc_type", sa.String(length=64), nullable=False),
        sa.Column("receipt_doc_ref", sa.String(length=64), nullable=False),
        sa.Column("receipt_doc_line_no", sa.String(length=32), nullable=False),
        sa.Column("supplier_order_ref", sa.String(length=64), nullable=True),
        sa.Column("supplier_order_line_no", sa.String(length=32), nullable=True),
        sa.Column("match_rule", sa.String(length=64), nullable=False),
        sa.Column("match_status", sa.String(length=16), nullable=False),
        sa.Column(
            "ambiguity_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "match_status IN ('exact', 'ambiguous', 'unmatched')",
            name="ck_supplier_receipt_provenance_match_status",
        ),
        sa.CheckConstraint(
            "ambiguity_count >= 0",
            name="ck_supplier_receipt_provenance_ambiguity_count",
        ),
        sa.CheckConstraint(
            "(match_status = 'exact' AND supplier_order_ref IS NOT NULL "
            "AND supplier_order_line_no IS NOT NULL AND ambiguity_count = 0) "
            "OR (match_status = 'ambiguous' AND ambiguity_count > 1 "
            "AND reason IS NOT NULL) "
            "OR (match_status = 'unmatched' AND ambiguity_count = 0 "
            "AND reason IS NOT NULL)",
            name="ck_supplier_receipt_provenance_match_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_generation_id"],
            ["ledger_generation.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["stock_ledger_entry_id"],
            ["stock_ledger_entry.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ledger_generation_id",
            "stock_ledger_entry_id",
            name="uq_supplier_receipt_provenance_generation_sle",
        ),
    )
    op.create_index(
        "ix_sl_srp_ledger_gen_id",
        "stock_ledger_supplier_receipt_provenance",
        ["ledger_generation_id"],
        unique=False,
    )
    op.create_index(
        "ix_sl_srp_entry_id",
        "stock_ledger_supplier_receipt_provenance",
        ["stock_ledger_entry_id"],
        unique=False,
    )
    op.create_index(
        "ix_supplier_receipt_provenance_order",
        "stock_ledger_supplier_receipt_provenance",
        ["supplier_order_ref", "supplier_order_line_no"],
        unique=False,
    )
    op.create_index(
        "ix_supplier_receipt_provenance_generation_status",
        "stock_ledger_supplier_receipt_provenance",
        ["ledger_generation_id", "match_status"],
        unique=False,
    )
    op.create_index(
        "ix_supplier_receipt_provenance_receipt_line",
        "stock_ledger_supplier_receipt_provenance",
        [
            "ledger_generation_id",
            "receipt_doc_type",
            "receipt_doc_ref",
            "receipt_doc_line_no",
        ],
        unique=False,
    )

    op.add_column(
        "mrp_execution_allocation",
        sa.Column("stock_ledger_entry_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_mrp_execution_allocation_stock_ledger_entry",
        "mrp_execution_allocation",
        "stock_ledger_entry",
        ["stock_ledger_entry_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_mrp_execution_allocation_stock_ledger_entry_id",
        "mrp_execution_allocation",
        ["stock_ledger_entry_id"],
        unique=False,
    )
    op.create_index(
        "ix_mrp_execution_allocation_generation_sle",
        "mrp_execution_allocation",
        ["ledger_generation_id", "stock_ledger_entry_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mrp_execution_allocation_generation_sle",
        table_name="mrp_execution_allocation",
    )
    op.drop_index(
        "ix_mrp_execution_allocation_stock_ledger_entry_id",
        table_name="mrp_execution_allocation",
    )
    op.drop_constraint(
        "fk_mrp_execution_allocation_stock_ledger_entry",
        "mrp_execution_allocation",
        type_="foreignkey",
    )
    op.drop_column("mrp_execution_allocation", "stock_ledger_entry_id")
    op.drop_table("stock_ledger_supplier_receipt_provenance")
    op.drop_table("purchase_export_line_allocation")
    for table_name in ("planned_rework", "planned_order"):
        op.drop_index(
            f"ix_{table_name}_ledger_generation_id",
            table_name=table_name,
        )
        op.drop_constraint(
            f"fk_{table_name}_ledger_generation",
            table_name,
            type_="foreignkey",
        )
        op.drop_column(table_name, "ledger_generation_id")
