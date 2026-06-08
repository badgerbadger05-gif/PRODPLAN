"""add material coverage snapshot

Revision ID: 20260605_04
Revises: 20260605_03
Create Date: 2026-06-05 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260605_04"
down_revision = "20260605_03"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "production_order_line_states"
    if not _has_column(inspector, table, "material_coverage_snapshot"):
        op.add_column(table, sa.Column("material_coverage_snapshot", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table = "production_order_line_states"
    if _has_column(inspector, table, "material_coverage_snapshot"):
        op.drop_column(table, "material_coverage_snapshot")
