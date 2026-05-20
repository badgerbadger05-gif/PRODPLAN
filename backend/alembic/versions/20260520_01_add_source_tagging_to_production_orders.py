"""add source tagging to production orders and products

Marks orders created internally by PRODPLAN from MRP planned orders, so they can
be distinguished from 1C-synced ones and counted as already-planned in
subsequent MRP recalculations. Provides DB-level idempotency: at most one
production_products row may reference a given planned_order.

Revision ID: 20260520_01
Revises: 20260519_02
Create Date: 2026-05-20 16:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260520_01"
down_revision = "20260519_02"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "production_orders", "source"):
        op.add_column(
            "production_orders",
            sa.Column("source", sa.String(length=16), nullable=False, server_default="1c"),
        )
    inspector = sa.inspect(bind)
    if not _has_index(inspector, "production_orders", "ix_production_orders_source"):
        op.create_index("ix_production_orders_source", "production_orders", ["source"])

    if not _has_column(inspector, "production_orders", "source_run_id"):
        op.add_column(
            "production_orders",
            sa.Column("source_run_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_production_orders_source_run_id_planning_run",
            "production_orders",
            "planning_run",
            ["source_run_id"],
            ["run_id"],
            ondelete="SET NULL",
        )
    inspector = sa.inspect(bind)
    if not _has_index(inspector, "production_orders", "ix_production_orders_source_run_id"):
        op.create_index("ix_production_orders_source_run_id", "production_orders", ["source_run_id"])

    if not _has_column(inspector, "production_products", "source_planned_order_id"):
        op.add_column(
            "production_products",
            sa.Column("source_planned_order_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_production_products_source_planned_order_id_planned_order",
            "production_products",
            "planned_order",
            ["source_planned_order_id"],
            ["order_id"],
            ondelete="SET NULL",
        )

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "production_products", "ux_production_products_source_planned_order"):
        # Partial unique: a planned_order can back at most one internal production line.
        # NULL values are unconstrained, so 1C-synced rows (source_planned_order_id IS NULL)
        # are not affected.
        op.create_index(
            "ux_production_products_source_planned_order",
            "production_products",
            ["source_planned_order_id"],
            unique=True,
            postgresql_where=sa.text("source_planned_order_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_index(inspector, "production_products", "ux_production_products_source_planned_order"):
        op.drop_index("ux_production_products_source_planned_order", table_name="production_products")

    if _has_column(inspector, "production_products", "source_planned_order_id"):
        try:
            op.drop_constraint(
                "fk_production_products_source_planned_order_id_planned_order",
                "production_products",
                type_="foreignkey",
            )
        except Exception:
            pass
        op.drop_column("production_products", "source_planned_order_id")

    inspector = sa.inspect(bind)
    if _has_index(inspector, "production_orders", "ix_production_orders_source_run_id"):
        op.drop_index("ix_production_orders_source_run_id", table_name="production_orders")
    if _has_column(inspector, "production_orders", "source_run_id"):
        try:
            op.drop_constraint(
                "fk_production_orders_source_run_id_planning_run",
                "production_orders",
                type_="foreignkey",
            )
        except Exception:
            pass
        op.drop_column("production_orders", "source_run_id")

    inspector = sa.inspect(bind)
    if _has_index(inspector, "production_orders", "ix_production_orders_source"):
        op.drop_index("ix_production_orders_source", table_name="production_orders")
    if _has_column(inspector, "production_orders", "source"):
        op.drop_column("production_orders", "source")
