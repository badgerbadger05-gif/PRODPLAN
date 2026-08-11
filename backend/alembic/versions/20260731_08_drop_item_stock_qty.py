"""Drop the retired aggregate physical-stock cache from items.

Revision ID: 20260731_08
Revises: 20260731_07
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_08"
down_revision = "20260731_07"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table_name: str, column_name: str) -> bool:
    return column_name in {row["name"] for row in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "items" in set(inspector.get_table_names()) and _has_column(
        inspector, "items", "stock_qty"
    ):
        op.drop_column("items", "stock_qty")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "items" in set(inspector.get_table_names()) and not _has_column(
        inspector, "items", "stock_qty"
    ):
        op.add_column(
            "items",
            sa.Column(
                "stock_qty",
                sa.Numeric(10, 3),
                nullable=True,
                server_default="0",
            ),
        )
