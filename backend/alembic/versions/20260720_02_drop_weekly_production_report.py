"""drop weekly production report tables

Revision ID: 20260720_02
Revises: 20260720_01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_02"
down_revision: Union[str, Sequence[str], None] = "20260720_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("production_day_close_item")
    op.drop_table("production_day_close")


def downgrade() -> None:
    op.create_table(
        "production_day_close",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("close_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("closed_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_production_day_close_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("close_date", name="ux_production_day_close_close_date"),
    )
    op.create_index("ix_production_day_close_close_date", "production_day_close", ["close_date"], unique=True)
    op.create_index("ix_production_day_close_status", "production_day_close", ["status"], unique=False)

    op.create_table(
        "production_day_close_item",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day_close_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("planned_qty_snapshot", sa.DECIMAL(precision=15, scale=3), nullable=False),
        sa.Column("fact_qty_snapshot", sa.DECIMAL(precision=15, scale=3), nullable=False),
        sa.Column("carry_qty", sa.DECIMAL(precision=15, scale=3), nullable=False),
        sa.Column("applied_to_date", sa.Date(), nullable=True),
        sa.Column("original_planned_qty_before_carry", sa.DECIMAL(precision=15, scale=3), nullable=True),
        sa.Column("planned_qty_after_carry", sa.DECIMAL(precision=15, scale=3), nullable=True),
        sa.Column("carry_status", sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(["day_close_id"], ["production_day_close.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day_close_id", "item_id", name="ux_production_day_close_item_day_item"),
    )
    op.create_index(
        "ix_production_day_close_item_day_close_id",
        "production_day_close_item",
        ["day_close_id"],
        unique=False,
    )
    op.create_index(
        "ix_production_day_close_item_item_id",
        "production_day_close_item",
        ["item_id"],
        unique=False,
    )
