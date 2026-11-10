"""Persist complete source verification for physical no-op refreshes.

A verified zero-delta source poll can advance freshness without changing the
accepted generation's business cutoff or creating a successor generation.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260921_01"
down_revision = "20261030_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planning_truth_verification",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ledger_generation_id",
            sa.BigInteger(),
            sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "physical_import_batch_id",
            sa.BigInteger(),
            nullable=False,
        ),
        sa.Column("physical_import_batch_key", sa.String(length=128), nullable=False),
        sa.Column("source_batch_hash", sa.String(length=64), nullable=False),
        sa.Column("verified_through", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("source_complete", sa.Boolean(), nullable=False),
        sa.Column("input_delta_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replayed_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("convergence_hash", sa.String(length=64), nullable=False),
        sa.Column("recorder_audit_checksum", sa.String(length=64), nullable=False),
        sa.UniqueConstraint(
            "ledger_generation_id",
            name="uq_planning_truth_verification_generation",
        ),
        sa.CheckConstraint(
            "input_delta_rows = 0 AND replayed_rows = 0",
            name="ck_planning_truth_verification_noop",
        ),
    )
    op.create_index(
        "ix_planning_truth_verification_generation",
        "planning_truth_verification",
        ["ledger_generation_id"],
    )
    op.create_index(
        "ix_planning_truth_verification_batch",
        "planning_truth_verification",
        ["physical_import_batch_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_planning_truth_verification_batch",
        table_name="planning_truth_verification",
    )
    op.drop_index(
        "ix_planning_truth_verification_generation",
        table_name="planning_truth_verification",
    )
    op.drop_table("planning_truth_verification")
