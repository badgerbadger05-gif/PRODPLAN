"""Add durable multi-generation assembly-output repair workflow.

Revision ID: 20260904_04
Revises: 20260904_03
"""

from alembic import op
import sqlalchemy as sa

from app.models import CrossPlatformJSON


revision = "20260904_04"
down_revision = "20260904_03"
branch_labels = None
depends_on = None


def _bigint() -> sa.types.TypeEngine:
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "assembly_output_repair_job",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("audit_checksum", sa.String(length=64), nullable=False),
        sa.Column("audit_algorithm_version", sa.String(length=128), nullable=False),
        sa.Column("source_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("phase1_generation_key", sa.String(length=128), nullable=False),
        sa.Column("phase1_generation_id", sa.BigInteger(), nullable=True),
        sa.Column("audit_payload", CrossPlatformJSON(), nullable=False),
        sa.Column("expected_fact_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("expected_allocated_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("expected_surplus_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "result",
            CrossPlatformJSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "audit_checksum", name="uq_assembly_output_repair_job_checksum"
        ),
        sa.UniqueConstraint(
            "phase1_generation_key", name="uq_assembly_output_repair_job_phase1_key"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'phase1_published', 'rebasing', "
            "'completed', 'blocked', 'failed')",
            name="ck_assembly_output_repair_job_status",
        ),
        sa.CheckConstraint(
            "expected_fact_qty >= 0 AND expected_allocated_qty >= 0 "
            "AND expected_surplus_qty >= 0",
            name="ck_assembly_output_repair_job_qty",
        ),
    )
    op.create_index(
        "ix_assembly_output_repair_job_checksum",
        "assembly_output_repair_job",
        ["audit_checksum"],
    )
    op.create_index(
        "ix_assembly_output_repair_job_source_generation",
        "assembly_output_repair_job",
        ["source_generation_id"],
    )
    op.create_index(
        "ix_assembly_output_repair_job_phase1_generation",
        "assembly_output_repair_job",
        ["phase1_generation_id"],
    )

    op.create_table(
        "assembly_output_repair_fact",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            _bigint(),
            sa.ForeignKey("assembly_output_repair_job.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stock_ledger_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("posting_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fact_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("expected_surplus_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("decision_status", sa.String(length=32), nullable=False),
        sa.UniqueConstraint(
            "job_id", "stock_ledger_entry_id", name="uq_output_repair_fact_job_sle"
        ),
        sa.CheckConstraint(
            "fact_qty > 0 AND expected_surplus_qty >= 0",
            name="ck_output_repair_fact_qty",
        ),
    )
    op.create_index(
        "ix_output_repair_fact_job", "assembly_output_repair_fact", ["job_id"]
    )
    op.create_index(
        "ix_output_repair_fact_sle",
        "assembly_output_repair_fact",
        ["stock_ledger_entry_id"],
    )
    op.create_index(
        "ix_output_repair_fact_item", "assembly_output_repair_fact", ["item_id"]
    )

    op.create_table(
        "assembly_output_repair_allocation",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            _bigint(),
            sa.ForeignKey("assembly_output_repair_job.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stock_ledger_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("allocation_ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("plan_line_id", sa.Integer(), nullable=False),
        sa.Column("audited_run_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("allocated_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("match_rule", sa.String(length=16), nullable=False),
        sa.Column(
            "requires_mrp_replacement",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.UniqueConstraint(
            "job_id",
            "stock_ledger_entry_id",
            "plan_line_id",
            name="uq_output_repair_allocation_job_sle_line",
        ),
        sa.CheckConstraint(
            "allocated_qty > 0", name="ck_output_repair_allocation_qty"
        ),
    )
    for name, columns in (
        ("ix_output_repair_allocation_job", ["job_id"]),
        ("ix_output_repair_allocation_sle", ["stock_ledger_entry_id"]),
        ("ix_output_repair_allocation_plan", ["plan_id"]),
        ("ix_output_repair_allocation_line", ["plan_line_id"]),
        ("ix_output_repair_allocation_run", ["audited_run_id"]),
        ("ix_output_repair_allocation_item", ["item_id"]),
    ):
        op.create_index(name, "assembly_output_repair_allocation", columns)

    op.create_table(
        "assembly_output_repair_target",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            _bigint(),
            sa.ForeignKey("assembly_output_repair_job.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("predecessor_run_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("expected_roots", CrossPlatformJSON(), nullable=False),
        sa.Column("expected_roots_checksum", sa.String(length=64), nullable=False),
        sa.Column("successor_run_id", sa.Integer(), nullable=True),
        sa.Column("published_generation_id", sa.BigInteger(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "result",
            CrossPlatformJSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("job_id", "plan_id", name="uq_output_repair_target_job_plan"),
        sa.UniqueConstraint(
            "job_id", "predecessor_run_id", name="uq_output_repair_target_job_run"
        ),
        sa.UniqueConstraint("job_id", "sequence", name="uq_output_repair_target_job_seq"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'blocked', 'failed')",
            name="ck_output_repair_target_status",
        ),
    )
    for name, columns in (
        ("ix_output_repair_target_job", ["job_id"]),
        ("ix_output_repair_target_plan", ["plan_id"]),
        ("ix_output_repair_target_predecessor", ["predecessor_run_id"]),
        ("ix_output_repair_target_successor", ["successor_run_id"]),
        ("ix_output_repair_target_generation", ["published_generation_id"]),
    ):
        op.create_index(name, "assembly_output_repair_target", columns)


def downgrade() -> None:
    op.drop_table("assembly_output_repair_target")
    op.drop_table("assembly_output_repair_allocation")
    op.drop_table("assembly_output_repair_fact")
    op.drop_table("assembly_output_repair_job")
