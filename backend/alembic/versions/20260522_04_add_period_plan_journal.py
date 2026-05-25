"""add period plan journal

Revision ID: 20260522_04
Revises: 20260522_03
Create Date: 2026-05-22 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260522_04"
down_revision = "20260522_03"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "production_plan_header"):
        op.create_table(
            "production_plan_header",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("period_from", sa.Date(), nullable=False),
            sa.Column("period_to", sa.Date(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(length=100), nullable=True),
            sa.Column("fixed_by", sa.String(length=100), nullable=True),
            sa.Column("fixed_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("period_to >= period_from", name="ck_production_plan_header_period"),
            sa.CheckConstraint("status in ('draft', 'fixed', 'archived')", name="ck_production_plan_header_status"),
            sa.PrimaryKeyConstraint("id"),
        )

    inspector = sa.inspect(bind)
    for idx_name, cols in (
        ("ix_production_plan_header_id", ["id"]),
        ("ix_production_plan_header_name", ["name"]),
        ("ix_production_plan_header_period_from", ["period_from"]),
        ("ix_production_plan_header_period_to", ["period_to"]),
        ("ix_production_plan_header_status", ["status"]),
    ):
        if not _has_index(inspector, "production_plan_header", idx_name):
            op.create_index(idx_name, "production_plan_header", cols, unique=False)

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "production_plan_line"):
        op.create_table(
            "production_plan_line",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("plan_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("bucket_date", sa.Date(), nullable=False),
            sa.Column("qty", sa.Numeric(15, 3), nullable=False, server_default="0"),
            sa.Column("locked_by_run_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["locked_by_run_id"], ["planning_run.run_id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["plan_id"], ["production_plan_header.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("plan_id", "item_id", "bucket_date", name="ux_production_plan_line_plan_item_bucket"),
        )

    inspector = sa.inspect(bind)
    for idx_name, cols in (
        ("ix_production_plan_line_id", ["id"]),
        ("ix_production_plan_line_plan_id", ["plan_id"]),
        ("ix_production_plan_line_item_id", ["item_id"]),
        ("ix_production_plan_line_bucket_date", ["bucket_date"]),
        ("ix_production_plan_line_locked_by_run_id", ["locked_by_run_id"]),
    ):
        if not _has_index(inspector, "production_plan_line", idx_name):
            op.create_index(idx_name, "production_plan_line", cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "production_plan_line"):
        op.drop_table("production_plan_line")

    inspector = sa.inspect(bind)
    if _has_table(inspector, "production_plan_header"):
        op.drop_table("production_plan_header")
