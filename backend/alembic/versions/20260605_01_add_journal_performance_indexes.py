"""add journal performance indexes

Revision ID: 20260605_01
Revises: 20260529_01
Create Date: 2026-06-05 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260605_01"
down_revision = "20260529_01"
branch_labels = None
depends_on = None


INDEXES = (
    (
        "ix_production_orders_journal_active_date",
        "production_orders",
        ["deletion_mark", "order_date", "order_number", "order_id"],
    ),
    (
        "ix_production_products_item_order",
        "production_products",
        ["item_id", "order_id"],
    ),
    (
        "ix_planned_order_run_item_dates",
        "planned_order",
        ["run_id", "item_id", "start_date", "finish_date"],
    ),
    (
        "ix_planned_order_run_demand_ref",
        "planned_order",
        ["run_id", "demand_ref"],
    ),
    (
        "ix_planned_purchase_run_item_bucket",
        "planned_purchase",
        ["run_id", "item_id", "bucket_date"],
    ),
    (
        "ix_planned_purchase_run_source_req",
        "planned_purchase",
        ["run_id", "source_mrp_requirement_id"],
    ),
    (
        "ix_planned_rework_run_item_bucket",
        "planned_rework",
        ["run_id", "item_id", "bucket_date"],
    ),
    (
        "ix_sync_link_doctype_source_entity",
        "sync_link",
        ["source_doctype", "source_id", "target_entity"],
    ),
)


def _has_index(inspector: sa.Inspector, table: str, index: str) -> bool:
    return index in {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for index_name, table_name, columns in INDEXES:
        if not _has_index(inspector, table_name, index_name):
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for index_name, table_name, _columns in reversed(INDEXES):
        if _has_index(inspector, table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
