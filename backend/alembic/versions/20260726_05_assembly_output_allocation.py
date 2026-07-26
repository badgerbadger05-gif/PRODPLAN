"""Add canonical physical assembly-output decisions and allocations.

Revision ID: 20260726_05
Revises: 20260726_04
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_05"
down_revision = "20260726_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assembly_output_fact_decision",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("stock_ledger_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("decision_status", sa.String(16), nullable=False),
        sa.Column("link_kind", sa.String(24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence_payload", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("source_content_hash", sa.String(64), nullable=False),
        sa.Column("surplus_qty", sa.DECIMAL(15, 3), server_default="0", nullable=False),
        sa.CheckConstraint(
            "decision_status IN ('allocatable','ambiguous','invalid')",
            name="ck_assembly_output_decision_status",
        ),
        sa.CheckConstraint(
            "link_kind IN ('exact_plan_line','planned_order','order_ref','none')",
            name="ck_assembly_output_decision_link_kind",
        ),
        sa.CheckConstraint("surplus_qty >= 0", name="ck_assembly_output_decision_surplus"),
        sa.CheckConstraint("length(source_content_hash) = 64", name="ck_assembly_output_decision_hash"),
        sa.ForeignKeyConstraint(["ledger_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stock_ledger_entry_id"], ["stock_ledger_entry.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ledger_generation_id", "stock_ledger_entry_id",
            name="uq_assembly_output_decision_generation_sle",
        ),
    )
    op.create_index(
        "ix_assembly_output_decision_generation_status",
        "assembly_output_fact_decision",
        ["ledger_generation_id", "decision_status"],
    )
    op.create_table(
        "assembly_output_allocation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("stock_ledger_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("plan_line_id", sa.Integer(), nullable=False),
        sa.Column("allocated_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("match_rule", sa.String(8), nullable=False),
        sa.Column("allocation_ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("allocated_qty > 0", name="ck_assembly_output_allocation_qty"),
        sa.CheckConstraint("match_rule IN ('exact','fifo')", name="ck_assembly_output_allocation_rule"),
        sa.CheckConstraint("allocation_ordinal >= 0", name="ck_assembly_output_allocation_ordinal"),
        sa.ForeignKeyConstraint(["ledger_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stock_ledger_entry_id"], ["stock_ledger_entry.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_id"], ["production_plan_header.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["plan_line_id"], ["production_plan_line.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ledger_generation_id", "stock_ledger_entry_id", "allocation_ordinal",
            name="uq_assembly_output_allocation_generation_sle_ordinal",
        ),
        sa.UniqueConstraint(
            "ledger_generation_id", "stock_ledger_entry_id", "plan_line_id",
            name="uq_assembly_output_allocation_generation_sle_line",
        ),
    )
    op.create_index(
        "ix_assembly_output_allocation_generation_line",
        "assembly_output_allocation",
        ["ledger_generation_id", "plan_line_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assembly_output_allocation_generation_line",
        table_name="assembly_output_allocation",
    )
    op.drop_table("assembly_output_allocation")
    op.drop_index(
        "ix_assembly_output_decision_generation_status",
        table_name="assembly_output_fact_decision",
    )
    op.drop_table("assembly_output_fact_decision")
