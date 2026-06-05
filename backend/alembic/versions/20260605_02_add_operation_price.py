"""add operation price

Revision ID: 20260605_02
Revises: 20260605_01
Create Date: 2026-06-05 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260605_02"
down_revision = "20260605_01"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_column(inspector, "operations", "operation_price"):
        op.add_column(
            "operations",
            sa.Column("operation_price", sa.DECIMAL(10, 2), nullable=True, server_default="0"),
        )
        op.alter_column("operations", "operation_price", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_column(inspector, "operations", "operation_price"):
        op.drop_column("operations", "operation_price")
