"""Add append-only stable/shadow planning comparison tables.

Revision ID: 20260723_03
Revises: 20260723_02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260723_03"
down_revision = "20260723_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "planning_comparison_batch",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("capture_key", sa.String(128), nullable=False),
        sa.Column("stable_base_url", sa.String(512), nullable=False),
        sa.Column("cutoff_grade", sa.String(16), nullable=False),
        sa.Column("cutoff_reason", sa.Text()),
        sa.Column("stable_run_key", sa.String(128)),
        sa.Column("shadow_run_key", sa.String(128)),
        sa.Column("metrics", json_type, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("capture_key", name="uq_planning_comparison_batch_capture_key"),
    )
    op.create_table(
        "planning_comparison_event",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.BigInteger(), sa.ForeignKey("planning_comparison_batch.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_planning_comparison_event_batch_id", "planning_comparison_event", ["batch_id"])
    op.create_table(
        "planning_comparison_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.BigInteger(), sa.ForeignKey("planning_comparison_batch.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("contour", sa.String(16), nullable=False),
        sa.Column("snapshot_kind", sa.String(32), nullable=False),
        sa.Column("raw_payload_hash", sa.String(64), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("captured_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("batch_id", "contour", "snapshot_kind", name="uq_planning_comparison_snapshot_axis"),
    )
    op.create_index("ix_planning_comparison_snapshot_batch_id", "planning_comparison_snapshot", ["batch_id"])
    op.create_table(
        "planning_comparison_row",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.BigInteger(), sa.ForeignKey("planning_comparison_batch.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("contour", sa.String(16), nullable=False),
        sa.Column("result_kind", sa.String(16), nullable=False),
        sa.Column("canonical_key", sa.String(768), nullable=False),
        sa.Column("item_key", sa.String(128), nullable=False),
        sa.Column("bucket_date", sa.Date()),
        sa.Column("quantity", sa.DECIMAL(24, 6), nullable=False),
        sa.Column("raw_payload_hash", sa.String(64), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.UniqueConstraint("batch_id", "contour", "result_kind", "canonical_key", name="uq_planning_comparison_row_axis"),
    )
    op.create_index("ix_planning_comparison_row_batch_id", "planning_comparison_row", ["batch_id"])
    op.create_index("ix_planning_comparison_row_item_key", "planning_comparison_row", ["item_key"])
    op.create_table(
        "planning_comparison_diff",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.BigInteger(), sa.ForeignKey("planning_comparison_batch.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("result_kind", sa.String(16), nullable=False),
        sa.Column("canonical_key", sa.String(768), nullable=False),
        sa.Column("item_key", sa.String(128), nullable=False),
        sa.Column("stable_quantity", sa.DECIMAL(24, 6), nullable=False),
        sa.Column("shadow_quantity", sa.DECIMAL(24, 6), nullable=False),
        sa.Column("delta_quantity", sa.DECIMAL(24, 6), nullable=False),
        sa.Column("classification", sa.String(24), nullable=False),
        sa.UniqueConstraint("batch_id", "result_kind", "canonical_key", name="uq_planning_comparison_diff_axis"),
    )
    op.create_index("ix_planning_comparison_diff_batch_id", "planning_comparison_diff", ["batch_id"])
    op.create_index("ix_planning_comparison_diff_item_key", "planning_comparison_diff", ["item_key"])


def downgrade() -> None:
    for table in (
        "planning_comparison_diff",
        "planning_comparison_row",
        "planning_comparison_snapshot",
        "planning_comparison_event",
        "planning_comparison_batch",
    ):
        op.drop_table(table)
