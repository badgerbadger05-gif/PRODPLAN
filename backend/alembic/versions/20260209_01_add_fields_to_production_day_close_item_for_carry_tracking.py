"""add fields to production day close item for carry tracking

Revision ID: 20260209_01
Revises: 20260206_01
Create Date: 2026-02-09

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260209_01"
down_revision = "20260206_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add column to store original planned quantity before carry was applied
    op.add_column(
        "production_day_close_item",
        sa.Column("original_planned_qty_before_carry", sa.Numeric(15, 3), nullable=True, server_default=None)
    )

    # Add column to store planned quantity after carry was applied (for diagnostics / safer re-run)
    op.add_column(
        "production_day_close_item",
        sa.Column("planned_qty_after_carry", sa.Numeric(15, 3), nullable=True, server_default=None)
    )

    # Add column to track carry status for better diagnostics
    op.add_column(
        "production_day_close_item",
        sa.Column("carry_status", sa.String(20), nullable=True, server_default=None)
    )


def downgrade() -> None:
    op.drop_column("production_day_close_item", "carry_status")
    op.drop_column("production_day_close_item", "planned_qty_after_carry")
    op.drop_column("production_day_close_item", "original_planned_qty_before_carry")
