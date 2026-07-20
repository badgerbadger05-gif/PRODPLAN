"""add mrp execution ledger primitives

Revision ID: 20260720_02
Revises: 20260720_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260720_02"
down_revision = "20260720_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)

    # --- mrp_requirement columns ---
    mrp_columns = (
        set()
        if inspector is None
        else {row["name"] for row in inspector.get_columns("mrp_requirement")}
    )
    new_columns = (
        ("executed_qty", sa.Column("executed_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0")),
        ("carried_remaining", sa.Column("carried_remaining", sa.DECIMAL(15, 3), nullable=False, server_default="0")),
        ("initial_snapshot_stock", sa.Column("initial_snapshot_stock", sa.DECIMAL(15, 3), nullable=True)),
        ("status", sa.Column("status", sa.String(length=20), nullable=False, server_default="open")),
        ("closed_at", sa.Column("closed_at", sa.TIMESTAMP(), nullable=True)),
    )
    for name, column in new_columns:
        if inspector is None or name not in mrp_columns:
            op.add_column("mrp_requirement", column)

    mrp_indexes = (
        set()
        if inspector is None
        else {row["name"] for row in sa.inspect(bind).get_indexes("mrp_requirement")}
    )
    if inspector is None or "ix_mrp_requirement_status" not in mrp_indexes:
        op.create_index("ix_mrp_requirement_status", "mrp_requirement", ["status"])

    # --- planning_run.prior_run_id (self-referential FK) ---
    pr_columns = (
        set()
        if inspector is None
        else {row["name"] for row in sa.inspect(bind).get_columns("planning_run")}
    )
    if inspector is None or "prior_run_id" not in pr_columns:
        # batch_alter_table keeps the self-FK creation portable across
        # Postgres (native ALTER) and SQLite (table rebuild).
        with op.batch_alter_table("planning_run", schema=None) as batch_op:
            batch_op.add_column(sa.Column("prior_run_id", sa.Integer(), nullable=True))
            batch_op.create_foreign_key(
                "fk_planning_run_prior_run_id",
                "planning_run",
                ["prior_run_id"],
                ["run_id"],
                ondelete="SET NULL",
            )

    pr_indexes = (
        set()
        if inspector is None
        else {row["name"] for row in sa.inspect(bind).get_indexes("planning_run")}
    )
    if inspector is None or "ix_planning_run_prior_run_id" not in pr_indexes:
        op.create_index("ix_planning_run_prior_run_id", "planning_run", ["prior_run_id"])


def downgrade() -> None:
    op.drop_index("ix_planning_run_prior_run_id", table_name="planning_run")
    with op.batch_alter_table("planning_run", schema=None) as batch_op:
        batch_op.drop_constraint("fk_planning_run_prior_run_id", type_="foreignkey")
        batch_op.drop_column("prior_run_id")

    op.drop_index("ix_mrp_requirement_status", table_name="mrp_requirement")
    for name in ("closed_at", "status", "initial_snapshot_stock", "carried_remaining", "executed_qty"):
        op.drop_column("mrp_requirement", name)
