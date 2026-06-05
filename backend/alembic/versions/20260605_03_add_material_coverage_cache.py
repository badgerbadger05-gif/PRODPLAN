"""add material coverage cache to production line states

Revision ID: 20260605_03
Revises: 20260605_02
Create Date: 2026-06-05 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260605_03"
down_revision = "20260605_02"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def _has_index(inspector: sa.Inspector, table: str, index: str) -> bool:
    return index in {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "production_order_line_states"
    if not _has_column(inspector, table, "material_coverage_status"):
        op.add_column(table, sa.Column("material_coverage_status", sa.String(length=32), nullable=True))
    if not _has_column(inspector, table, "material_coverage_label"):
        op.add_column(table, sa.Column("material_coverage_label", sa.String(length=64), nullable=True))
    if not _has_column(inspector, table, "material_coverage_calculated_at"):
        op.add_column(table, sa.Column("material_coverage_calculated_at", sa.TIMESTAMP(), nullable=True))
    if not _has_index(inspector, table, "ix_production_order_line_states_material_coverage_status"):
        op.create_index(
            "ix_production_order_line_states_material_coverage_status",
            table,
            ["material_coverage_status"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "production_order_line_states"
    if _has_index(inspector, table, "ix_production_order_line_states_material_coverage_status"):
        op.drop_index("ix_production_order_line_states_material_coverage_status", table_name=table)
    if _has_column(inspector, table, "material_coverage_calculated_at"):
        op.drop_column(table, "material_coverage_calculated_at")
    if _has_column(inspector, table, "material_coverage_label"):
        op.drop_column(table, "material_coverage_label")
    if _has_column(inspector, table, "material_coverage_status"):
        op.drop_column(table, "material_coverage_status")
