"""add production day close tables

Revision ID: 20260206_01
Revises: 20251222_01
Create Date: 2026-02-06

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260206_01"
down_revision = "20251222_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Optional global calendar for working / non-working days
    op.create_table(
        "work_calendar_day",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("is_workday", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("comment", sa.Text(), nullable=True),
    )

    # Day close header
    op.create_table(
        "production_day_close",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("close_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'OPEN'")),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("closed_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_production_day_close_status"),
        sa.UniqueConstraint("close_date", name="ux_production_day_close_close_date"),
    )
    op.create_index("ix_production_day_close_close_date", "production_day_close", ["close_date"], unique=True)
    op.create_index("ix_production_day_close_status", "production_day_close", ["status"], unique=False)

    # Day close per-item snapshot and applied carry
    op.create_table(
        "production_day_close_item",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "day_close_id",
            sa.Integer(),
            sa.ForeignKey("production_day_close.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id"), nullable=False),
        sa.Column("planned_qty_snapshot", sa.Numeric(15, 3), nullable=False, server_default=sa.text("0")),
        sa.Column("fact_qty_snapshot", sa.Numeric(15, 3), nullable=False, server_default=sa.text("0")),
        sa.Column("carry_qty", sa.Numeric(15, 3), nullable=False, server_default=sa.text("0")),
        sa.Column("applied_to_date", sa.Date(), nullable=True),
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


def downgrade() -> None:
    op.drop_index("ix_production_day_close_item_item_id", table_name="production_day_close_item")
    op.drop_index("ix_production_day_close_item_day_close_id", table_name="production_day_close_item")
    op.drop_table("production_day_close_item")

    op.drop_index("ix_production_day_close_status", table_name="production_day_close")
    op.drop_index("ix_production_day_close_close_date", table_name="production_day_close")
    op.drop_table("production_day_close")

    op.drop_table("work_calendar_day")

