"""Drop per-warehouse stock breakdown mirror.

Plan rule (Следующие этапы #7): legacy per-warehouse breakdown is no longer needed
for source-warehouse decisions; those decisions now use Ledger StockBin state from the
accepted generation.

Revision ID: 20260726_12
Revises: 20260726_11
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_12"
down_revision = "20260726_11"
branch_labels = None
depends_on = None


def _has_table(inspector: sa.Inspector, table: str) -> bool:
    return table in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_table(inspector, "item_warehouse_stock"):
        return

    op.drop_index(
        "ix_item_warehouse_stock_warehouse_ref1c",
        table_name="item_warehouse_stock",
    )
    op.drop_table("item_warehouse_stock")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_table(inspector, "item_warehouse_stock"):
        return

    op.create_table(
        "item_warehouse_stock",
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_ref1c", sa.String(length=36), nullable=False),
        sa.Column("qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["item_id"], ["items.item_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id", "warehouse_ref1c"),
    )
    op.create_index(
        "ix_item_warehouse_stock_warehouse_ref1c",
        "item_warehouse_stock",
        ["warehouse_ref1c"],
    )
