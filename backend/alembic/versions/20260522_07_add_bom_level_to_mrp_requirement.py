"""add bom_level to mrp_requirement for multi-level BOM explosion

Revision ID: 20260522_07
Revises: 20260522_06
Create Date: 2026-05-22 22:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260522_07"
down_revision = "20260522_06"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "mrp_requirement", "bom_level"):
        op.add_column(
            "mrp_requirement",
            sa.Column("bom_level", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "mrp_requirement", "bom_level"):
        op.drop_column("mrp_requirement", "bom_level")
