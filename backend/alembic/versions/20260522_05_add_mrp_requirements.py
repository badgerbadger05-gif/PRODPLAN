"""add fixed MRP requirement snapshot tables

Revision ID: 20260522_05
Revises: 20260522_04
Create Date: 2026-05-22 13:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260522_05"
down_revision = "20260522_04"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for col_name, column in (
        ("source_plan_id", sa.Column("source_plan_id", sa.Integer(), nullable=True)),
        ("period_from", sa.Column("period_from", sa.Date(), nullable=True)),
        ("period_to", sa.Column("period_to", sa.Date(), nullable=True)),
        ("fixed_at", sa.Column("fixed_at", sa.TIMESTAMP(), nullable=True)),
    ):
        if not _has_column(inspector, "planning_run", col_name):
            op.add_column("planning_run", column)

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "planning_run", "ix_planning_run_source_plan_id"):
        op.create_index("ix_planning_run_source_plan_id", "planning_run", ["source_plan_id"], unique=False)
    if not _has_index(inspector, "planning_run", "ix_planning_run_period_from"):
        op.create_index("ix_planning_run_period_from", "planning_run", ["period_from"], unique=False)
    if not _has_index(inspector, "planning_run", "ix_planning_run_period_to"):
        op.create_index("ix_planning_run_period_to", "planning_run", ["period_to"], unique=False)

    inspector = sa.inspect(bind)
    existing_fks = {fk.get("name") for fk in inspector.get_foreign_keys("planning_run")}
    if "fk_planning_run_source_plan_id" not in existing_fks:
        op.create_foreign_key(
            "fk_planning_run_source_plan_id",
            "planning_run",
            "production_plan_header",
            ["source_plan_id"],
            ["id"],
            ondelete="SET NULL",
        )

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "mrp_requirement"):
        op.create_table(
            "mrp_requirement",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("total_required_qty", sa.Numeric(15, 3), nullable=False, server_default="0"),
            sa.Column("net_required_qty", sa.Numeric(15, 3), nullable=False, server_default="0"),
            sa.Column("covered_qty", sa.Numeric(15, 3), nullable=False, server_default="0"),
            sa.Column("remaining_qty", sa.Numeric(15, 3), nullable=False, server_default="0"),
            sa.Column("period_from", sa.Date(), nullable=False),
            sa.Column("period_to", sa.Date(), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", "item_id", name="ux_mrp_requirement_run_item"),
        )

    inspector = sa.inspect(bind)
    for idx_name, cols in (
        ("ix_mrp_requirement_id", ["id"]),
        ("ix_mrp_requirement_run_id", ["run_id"]),
        ("ix_mrp_requirement_item_id", ["item_id"]),
        ("ix_mrp_requirement_period_from", ["period_from"]),
        ("ix_mrp_requirement_period_to", ["period_to"]),
    ):
        if not _has_index(inspector, "mrp_requirement", idx_name):
            op.create_index(idx_name, "mrp_requirement", cols, unique=False)

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "mrp_requirement_bucket"):
        op.create_table(
            "mrp_requirement_bucket",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("requirement_id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("bucket_date", sa.Date(), nullable=False),
            sa.Column("gross_qty", sa.Numeric(15, 3), nullable=False, server_default="0"),
            sa.Column("net_qty", sa.Numeric(15, 3), nullable=False, server_default="0"),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["requirement_id"], ["mrp_requirement.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("requirement_id", "bucket_date", name="ux_mrp_requirement_bucket_req_date"),
        )

    inspector = sa.inspect(bind)
    for idx_name, cols in (
        ("ix_mrp_requirement_bucket_id", ["id"]),
        ("ix_mrp_requirement_bucket_requirement_id", ["requirement_id"]),
        ("ix_mrp_requirement_bucket_run_id", ["run_id"]),
        ("ix_mrp_requirement_bucket_item_id", ["item_id"]),
        ("ix_mrp_requirement_bucket_bucket_date", ["bucket_date"]),
    ):
        if not _has_index(inspector, "mrp_requirement_bucket", idx_name):
            op.create_index(idx_name, "mrp_requirement_bucket", cols, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "mrp_requirement_bucket"):
        op.drop_table("mrp_requirement_bucket")
    inspector = sa.inspect(bind)
    if _has_table(inspector, "mrp_requirement"):
        op.drop_table("mrp_requirement")

    inspector = sa.inspect(bind)
    for idx_name in ("ix_planning_run_period_to", "ix_planning_run_period_from", "ix_planning_run_source_plan_id"):
        if _has_index(inspector, "planning_run", idx_name):
            op.drop_index(idx_name, table_name="planning_run")

    inspector = sa.inspect(bind)
    existing_fks = {fk.get("name") for fk in inspector.get_foreign_keys("planning_run")}
    if "fk_planning_run_source_plan_id" in existing_fks:
        op.drop_constraint("fk_planning_run_source_plan_id", "planning_run", type_="foreignkey")

    inspector = sa.inspect(bind)
    for col_name in ("fixed_at", "period_to", "period_from", "source_plan_id"):
        if _has_column(inspector, "planning_run", col_name):
            op.drop_column("planning_run", col_name)
