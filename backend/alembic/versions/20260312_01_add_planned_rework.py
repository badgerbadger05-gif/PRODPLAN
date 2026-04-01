"""add planned rework table

Revision ID: 20260312_01_add_planned_rework
Revises: 20260226_01
Create Date: 2026-03-12 13:00:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260312_01_add_planned_rework"
down_revision = "20260226_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planned_rework",
        sa.Column("rework_id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("planning_run.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id"), nullable=False),
        sa.Column("spec_id", sa.Integer(), sa.ForeignKey("specifications.spec_id"), nullable=True),
        sa.Column("requested_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("planned_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("need_date", sa.Date(), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("lead_time_days", sa.Integer(), nullable=False),
        sa.Column("priority_index", sa.Numeric(10, 4), nullable=True),
        sa.Column("bucket_date", sa.Date(), nullable=False),
        sa.Column("component_limit", sa.Numeric(15, 3), nullable=True),
        sa.Column("component_blocked", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("component_partial", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("shortage", sa.JSON(), nullable=True),
    )

    op.create_index("ix_planned_rework_run_id", "planned_rework", ["run_id"])
    op.create_index("ix_planned_rework_item_id", "planned_rework", ["item_id"])
    op.create_index("ix_planned_rework_spec_id", "planned_rework", ["spec_id"])
    op.create_index("ix_planned_rework_need_date", "planned_rework", ["need_date"])
    op.create_index("ix_planned_rework_order_date", "planned_rework", ["order_date"])
    op.create_index("ix_planned_rework_bucket_date", "planned_rework", ["bucket_date"])
    op.create_index("ix_planned_rework_run_item_need", "planned_rework", ["run_id", "item_id", "need_date"])


def downgrade() -> None:
    op.drop_index("ix_planned_rework_run_item_need", table_name="planned_rework")
    op.drop_index("ix_planned_rework_bucket_date", table_name="planned_rework")
    op.drop_index("ix_planned_rework_order_date", table_name="planned_rework")
    op.drop_index("ix_planned_rework_need_date", table_name="planned_rework")
    op.drop_index("ix_planned_rework_spec_id", table_name="planned_rework")
    op.drop_index("ix_planned_rework_item_id", table_name="planned_rework")
    op.drop_index("ix_planned_rework_run_id", table_name="planned_rework")
    op.drop_table("planned_rework")
