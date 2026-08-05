"""Repair legacy bucket archive tables when a stamped database lacks them.

Revision ID: 20260805_02
Revises: 20260805_01
"""

import sqlalchemy as sa
from alembic import op


revision = "20260805_02"
down_revision = "20260805_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("planning_run_bucket_modes"):
        op.create_table(
            "planning_run_bucket_modes",
            sa.Column(
                "run_id",
                sa.Integer(),
                sa.ForeignKey("planning_run.run_id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("use_weekly", sa.Boolean(), nullable=False),
            sa.Column("legacy_bucket_types", sa.JSON(), nullable=True),
            sa.Column("weekly_rows", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "captured_at",
                sa.TIMESTAMP(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
        )

    if not inspector.has_table("mrp_bucket_type_legacy"):
        op.create_table(
            "mrp_bucket_type_legacy",
            sa.Column("entity", sa.Text(), nullable=False),
            sa.Column("record_id", sa.Integer(), nullable=False),
            sa.Column(
                "run_id",
                sa.Integer(),
                sa.ForeignKey("planning_run.run_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("bucket_type", sa.Text(), nullable=False),
            sa.Column("bucket_date", sa.Date(), nullable=False),
            sa.PrimaryKeyConstraint(
                "entity", "record_id", name="pk_mrp_bucket_type_legacy"
            ),
        )
        op.create_index(
            "ix_mrp_bucket_legacy_run_entity",
            "mrp_bucket_type_legacy",
            ["run_id", "entity"],
            unique=False,
        )


def downgrade() -> None:
    # These are archival tables from 20251205_08. A repair migration cannot
    # distinguish tables it restored from tables that already existed.
    pass
