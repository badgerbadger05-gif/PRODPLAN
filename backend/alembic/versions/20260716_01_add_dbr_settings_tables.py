"""add DBR module settings tables

Parallel DBR (drum-buffer-rope) planning module — configuration storage.
Mirrors ERPNext prodflow settings (ProdFlow Planning Settings / Assembly Rate /
child category supply-risk). Only module-owned dbr_* tables are created here;
ignored warehouses / warehouse bindings continue to live in existing tables.

`create_all` is load-bearing in this project, so these DDL statements must
match backend/app/models.py (DbrSettings / DbrAssemblyRate /
DbrCategorySupplyRisk).

Revision ID: 20260716_01
Revises: 20260626_01
Create Date: 2026-07-16 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260716_01"
down_revision = "20260626_01"
branch_labels = None
depends_on = None


def _has_table(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "dbr_settings"):
        op.create_table(
            "dbr_settings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("frozen_days", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("gate_horizon_workdays", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("shelf_threshold_qty", sa.Numeric(12, 3), nullable=False, server_default="5"),
            sa.Column("rt_machining_days", sa.Integer(), nullable=False, server_default="7"),
            sa.Column("rt_welding_days", sa.Integer(), nullable=False, server_default="15"),
            sa.Column("rt_painting_days", sa.Integer(), nullable=False, server_default="21"),
            sa.Column("batch_days_turning", sa.Integer(), nullable=False, server_default="10"),
            sa.Column("batch_days_bending", sa.Integer(), nullable=False, server_default="7"),
            sa.Column("batch_days_welding", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("batch_days_paint_black", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("batch_days_paint_color", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("feeder_chain_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("feeder_load_horizon_weeks", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("w2_warehouse_ref1c", sa.String(length=36), nullable=True),
            sa.Column("w3_warehouse_ref1c", sa.String(length=36), nullable=True),
            sa.Column("w4_warehouse_ref1c", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_dbr_settings_id", "dbr_settings", ["id"])

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "dbr_assembly_rate"):
        op.create_table(
            "dbr_assembly_rate",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("resource_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("qty_per_capacity", sa.Numeric(12, 3), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["resource_id"], ["production_resources.resource_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("resource_id", "item_id", name="ux_dbr_assembly_rate_resource_item"),
        )
        op.create_index("ix_dbr_assembly_rate_id", "dbr_assembly_rate", ["id"])
        op.create_index("ix_dbr_assembly_rate_resource_id", "dbr_assembly_rate", ["resource_id"])
        op.create_index("ix_dbr_assembly_rate_item_id", "dbr_assembly_rate", ["item_id"])

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "dbr_category_supply_risk"):
        op.create_table(
            "dbr_category_supply_risk",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("item_group", sa.String(length=255), nullable=False),
            sa.Column("receipt_warehouse_ref1c", sa.String(length=36), nullable=True),
            sa.Column("supply_risk_pct", sa.Numeric(6, 2), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("item_group", name="ux_dbr_category_supply_risk_group"),
        )
        op.create_index("ix_dbr_category_supply_risk_id", "dbr_category_supply_risk", ["id"])
        op.create_index("ix_dbr_category_supply_risk_item_group", "dbr_category_supply_risk", ["item_group"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "dbr_category_supply_risk"):
        op.drop_table("dbr_category_supply_risk")
    inspector = sa.inspect(bind)
    if _has_table(inspector, "dbr_assembly_rate"):
        op.drop_table("dbr_assembly_rate")
    inspector = sa.inspect(bind)
    if _has_table(inspector, "dbr_settings"):
        op.drop_table("dbr_settings")
