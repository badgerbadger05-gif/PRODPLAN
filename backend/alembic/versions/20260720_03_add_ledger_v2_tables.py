"""add mrp execution ledger v2 tables (Increment 1, additive schema only)

Revision ID: 20260720_03
Revises: 20260720_02

Additive-only: new pool/freeze columns on mrp_requirement + planning_run, and
six new ledger tables (freeze_baseline / freeze_allocation / freeze_component /
execution_allocation / requirement_carry / drift_event). No logic reads them
yet — zero behavior change. server_defaults backfill existing NOT-NULL rows.
Inspector-guarded so re-running upgrade head is a no-op (idempotent).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260720_03"
down_revision = "20260720_02"
branch_labels = None
depends_on = None


def _json_type(bind):
    return JSONB() if bind.dialect.name == "postgresql" else sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)

    existing_tables = set() if inspector is None else set(inspector.get_table_names())

    # --- mrp_requirement: new additive columns ---
    mrp_columns = (
        set()
        if inspector is None
        else {row["name"] for row in inspector.get_columns("mrp_requirement")}
    )
    mrp_new_columns = (
        ("freeze_version", sa.Column("freeze_version", sa.Integer(), nullable=True)),
        (
            "drift_adjustment_qty",
            sa.Column("drift_adjustment_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
        ),
        ("characteristic_ref", sa.Column("characteristic_ref", sa.String(length=36), nullable=True)),
        ("organization_ref", sa.Column("organization_ref", sa.String(length=36), nullable=True)),
        ("planning_stock_pool", sa.Column("planning_stock_pool", sa.String(length=64), nullable=True)),
    )
    for name, column in mrp_new_columns:
        if inspector is None or name not in mrp_columns:
            op.add_column("mrp_requirement", column)

    # --- planning_run.active_freeze_version ---
    pr_columns = (
        set()
        if inspector is None
        else {row["name"] for row in inspector.get_columns("planning_run")}
    )
    if inspector is None or "active_freeze_version" not in pr_columns:
        op.add_column("planning_run", sa.Column("active_freeze_version", sa.Integer(), nullable=True))

    # --- mrp_freeze_baseline ---
    if inspector is None or "mrp_freeze_baseline" not in existing_tables:
        op.create_table(
            "mrp_freeze_baseline",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("freeze_version", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_ref", sa.String(length=36), nullable=True),
            sa.Column("organization_ref", sa.String(length=36), nullable=True),
            sa.Column("planning_stock_pool", sa.String(length=64), nullable=True),
            sa.Column("frozen_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.Column("stock_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("produced_total", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("received_total", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("unit_coef", sa.DECIMAL(15, 3), nullable=False, server_default="1"),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.UniqueConstraint(
                "run_id",
                "freeze_version",
                "item_id",
                "characteristic_ref",
                "organization_ref",
                "planning_stock_pool",
                name="ux_mrp_freeze_baseline_pool_version",
            ),
        )
        op.create_index("ix_mrp_freeze_baseline_run_id", "mrp_freeze_baseline", ["run_id"])
        op.create_index("ix_mrp_freeze_baseline_item_id", "mrp_freeze_baseline", ["item_id"])
        op.create_index("ix_mrp_freeze_baseline_run_version", "mrp_freeze_baseline", ["run_id", "freeze_version"])
        op.create_index(
            "ix_mrp_freeze_baseline_pool",
            "mrp_freeze_baseline",
            ["item_id", "characteristic_ref", "organization_ref", "planning_stock_pool"],
        )

    # --- mrp_freeze_allocation ---
    if inspector is None or "mrp_freeze_allocation" not in existing_tables:
        op.create_table(
            "mrp_freeze_allocation",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("freeze_version", sa.Integer(), nullable=False),
            sa.Column("requirement_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_ref", sa.String(length=36), nullable=True),
            sa.Column("organization_ref", sa.String(length=36), nullable=True),
            sa.Column("planning_stock_pool", sa.String(length=64), nullable=True),
            sa.Column("source_type", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("source_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("source_line_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("alloc_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("fact_at_freeze", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("realized_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("evaporated_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["requirement_id"], ["mrp_requirement.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.UniqueConstraint(
                "run_id",
                "freeze_version",
                "requirement_id",
                "source_type",
                "source_ref",
                "source_line_ref",
                name="ux_mrp_freeze_allocation_source",
            ),
        )
        op.create_index("ix_mrp_freeze_allocation_run_id", "mrp_freeze_allocation", ["run_id"])
        op.create_index("ix_mrp_freeze_allocation_item_id", "mrp_freeze_allocation", ["item_id"])
        op.create_index("ix_mrp_freeze_allocation_run_version", "mrp_freeze_allocation", ["run_id", "freeze_version"])
        op.create_index("ix_mrp_freeze_allocation_requirement", "mrp_freeze_allocation", ["requirement_id"])
        op.create_index(
            "ix_mrp_freeze_allocation_pool",
            "mrp_freeze_allocation",
            ["item_id", "characteristic_ref", "organization_ref", "planning_stock_pool"],
        )

    # --- mrp_freeze_component ---
    if inspector is None or "mrp_freeze_component" not in existing_tables:
        op.create_table(
            "mrp_freeze_component",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("freeze_version", sa.Integer(), nullable=False),
            sa.Column("parent_item_id", sa.Integer(), nullable=False),
            sa.Column("parent_characteristic_ref", sa.String(length=36), nullable=True),
            sa.Column("parent_organization_ref", sa.String(length=36), nullable=True),
            sa.Column("parent_planning_stock_pool", sa.String(length=64), nullable=True),
            sa.Column("component_item_id", sa.Integer(), nullable=False),
            sa.Column("component_characteristic_ref", sa.String(length=36), nullable=True),
            sa.Column("component_organization_ref", sa.String(length=36), nullable=True),
            sa.Column("component_planning_stock_pool", sa.String(length=64), nullable=True),
            sa.Column("spec_ref", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("spec_version", sa.String(length=50), nullable=True),
            sa.Column("norm_qty_per_unit", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("unit_coef", sa.DECIMAL(15, 3), nullable=False, server_default="1"),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["parent_item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["component_item_id"], ["items.item_id"]),
            sa.UniqueConstraint(
                "run_id",
                "freeze_version",
                "parent_item_id",
                "component_item_id",
                "spec_ref",
                name="ux_mrp_freeze_component_spec",
            ),
        )
        op.create_index("ix_mrp_freeze_component_run_id", "mrp_freeze_component", ["run_id"])
        op.create_index("ix_mrp_freeze_component_run_version", "mrp_freeze_component", ["run_id", "freeze_version"])
        op.create_index("ix_mrp_freeze_component_parent", "mrp_freeze_component", ["parent_item_id"])
        op.create_index("ix_mrp_freeze_component_component", "mrp_freeze_component", ["component_item_id"])

    # --- mrp_execution_allocation ---
    if inspector is None or "mrp_execution_allocation" not in existing_tables:
        op.create_table(
            "mrp_execution_allocation",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cycle_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("requirement_id", sa.Integer(), nullable=False),
            sa.Column("bucket_id", sa.Integer(), nullable=True),
            sa.Column("fact_type", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("allocation_kind", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("fact_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("fact_line_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("fact_date", sa.TIMESTAMP(), nullable=True),
            sa.Column("allocated_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("freeze_allocation_id", sa.Integer(), nullable=True),
            sa.Column("origin_requirement_id", sa.Integer(), nullable=True),
            sa.Column("calculated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["requirement_id"], ["mrp_requirement.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["bucket_id"], ["mrp_requirement_bucket.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["freeze_allocation_id"], ["mrp_freeze_allocation.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["origin_requirement_id"], ["mrp_requirement.id"], ondelete="SET NULL"),
            sa.UniqueConstraint(
                "requirement_id",
                "bucket_id",
                "fact_type",
                "fact_ref",
                "fact_line_ref",
                "allocation_kind",
                name="ux_mrp_execution_allocation_fact",
            ),
        )
        op.create_index("ix_mrp_execution_allocation_cycle", "mrp_execution_allocation", ["cycle_id"])
        op.create_index("ix_mrp_execution_allocation_requirement", "mrp_execution_allocation", ["requirement_id"])
        op.create_index("ix_mrp_execution_allocation_bucket", "mrp_execution_allocation", ["bucket_id"])
        op.create_index("ix_mrp_execution_allocation_freeze_allocation_id", "mrp_execution_allocation", ["freeze_allocation_id"])
        op.create_index("ix_mrp_execution_allocation_origin_requirement_id", "mrp_execution_allocation", ["origin_requirement_id"])

    # --- mrp_requirement_carry ---
    if inspector is None or "mrp_requirement_carry" not in existing_tables:
        op.create_table(
            "mrp_requirement_carry",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source_requirement_id", sa.Integer(), nullable=False),
            sa.Column("target_requirement_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_ref", sa.String(length=36), nullable=True),
            sa.Column("organization_ref", sa.String(length=36), nullable=True),
            sa.Column("planning_stock_pool", sa.String(length=64), nullable=True),
            sa.Column("carried_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("carried_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("operator", sa.String(length=100), nullable=True),
            sa.Column("source_run_id", sa.Integer(), nullable=True),
            sa.Column("target_run_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["source_requirement_id"], ["mrp_requirement.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["target_requirement_id"], ["mrp_requirement.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["source_run_id"], ["planning_run.run_id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["target_run_id"], ["planning_run.run_id"], ondelete="SET NULL"),
            sa.UniqueConstraint("source_requirement_id", name="ux_mrp_requirement_carry_source"),
        )
        op.create_index("ix_mrp_requirement_carry_target", "mrp_requirement_carry", ["target_requirement_id"])
        op.create_index("ix_mrp_requirement_carry_item_id", "mrp_requirement_carry", ["item_id"])
        op.create_index("ix_mrp_requirement_carry_source_run_id", "mrp_requirement_carry", ["source_run_id"])
        op.create_index("ix_mrp_requirement_carry_target_run_id", "mrp_requirement_carry", ["target_run_id"])
        op.create_index(
            "ix_mrp_requirement_carry_pool",
            "mrp_requirement_carry",
            ["item_id", "characteristic_ref", "organization_ref", "planning_stock_pool"],
        )

    # --- mrp_drift_event ---
    if inspector is None or "mrp_drift_event" not in existing_tables:
        op.create_table(
            "mrp_drift_event",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("cycle_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("characteristic_ref", sa.String(length=36), nullable=True),
            sa.Column("organization_ref", sa.String(length=36), nullable=True),
            sa.Column("planning_stock_pool", sa.String(length=64), nullable=True),
            sa.Column("kind", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("drift_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("expected_stock", sa.DECIMAL(15, 3), nullable=True),
            sa.Column("actual_stock", sa.DECIMAL(15, 3), nullable=True),
            sa.Column("matured", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("first_seen_cycle_id", sa.String(length=64), nullable=True),
            sa.Column("requirement_id", sa.Integer(), nullable=True),
            sa.Column("details", _json_type(bind), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["requirement_id"], ["mrp_requirement.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_mrp_drift_event_cycle", "mrp_drift_event", ["cycle_id"])
        op.create_index("ix_mrp_drift_event_item_id", "mrp_drift_event", ["item_id"])
        op.create_index("ix_mrp_drift_event_requirement", "mrp_drift_event", ["requirement_id"])
        op.create_index(
            "ix_mrp_drift_event_pool",
            "mrp_drift_event",
            ["item_id", "characteristic_ref", "organization_ref", "planning_stock_pool"],
        )


def downgrade() -> None:
    # Drop in reverse FK-dependency order.
    op.drop_table("mrp_drift_event")
    op.drop_table("mrp_requirement_carry")
    op.drop_table("mrp_execution_allocation")
    op.drop_table("mrp_freeze_component")
    op.drop_table("mrp_freeze_allocation")
    op.drop_table("mrp_freeze_baseline")

    op.drop_column("planning_run", "active_freeze_version")
    for name in (
        "planning_stock_pool",
        "organization_ref",
        "characteristic_ref",
        "drift_adjustment_qty",
        "freeze_version",
    ):
        op.drop_column("mrp_requirement", name)
