"""add forced order request/result tables

Revision ID: 20251222_01
Revises: 20251219_01
Create Date: 2025-12-22

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251222_01"
down_revision = "20251219_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # forced_order_request
    op.create_table(
        "forced_order_request",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("planning_run.run_id", ondelete="SET NULL"), nullable=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id"), nullable=False),
        sa.Column("need_date", sa.Date(), nullable=False),
        sa.Column("requested_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
    )
    op.create_index("ix_forced_order_request_run_id", "forced_order_request", ["run_id"], unique=False)
    op.create_index("ix_forced_order_request_item_id", "forced_order_request", ["item_id"], unique=False)
    op.create_index("ix_forced_order_request_need_date", "forced_order_request", ["need_date"], unique=False)

    # forced_order_result
    op.create_table(
        "forced_order_result",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Integer(),
            sa.ForeignKey("forced_order_request.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("planned_qty", sa.Numeric(15, 3), nullable=False),
        sa.Column("normalized_qty", sa.Numeric(15, 3), nullable=True),
        sa.Column("horizon_limit", sa.Numeric(15, 3), nullable=True),
        sa.Column("component_limit", sa.Numeric(15, 3), nullable=True),
        sa.Column("shortage", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=True),
    )
    op.create_index("ix_forced_order_result_request_id", "forced_order_result", ["request_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_forced_order_result_request_id", table_name="forced_order_result")
    op.drop_table("forced_order_result")

    op.drop_index("ix_forced_order_request_need_date", table_name="forced_order_request")
    op.drop_index("ix_forced_order_request_item_id", table_name="forced_order_request")
    op.drop_index("ix_forced_order_request_run_id", table_name="forced_order_request")
    op.drop_table("forced_order_request")

