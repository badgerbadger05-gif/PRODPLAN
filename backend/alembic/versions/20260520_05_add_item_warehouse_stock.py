"""add per-warehouse stock breakdown for items

Plan rule (Следующие этапы #7): "Добавить разрез остатков по складам в
локальную БД, чтобы автоматически выбирать склады источники и задавать вопрос
только при неоднозначности." Эта таблица — необходимая предпосылка для
- фильтра `ignored_warehouses` в coverage-расчётах (план: "Игнорируемые
  склады нужны, чтобы не задавать лишние вопросы по остаткам, например если
  компонент лежит в изоляторе брака");
- последующего автоматического выбора склада-источника.

Revision ID: 20260520_05
Revises: 20260520_04
Create Date: 2026-05-20 19:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260520_05"
down_revision = "20260520_04"
branch_labels = None
depends_on = None


def _has_table(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
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


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_table(inspector, "item_warehouse_stock"):
        return
    op.drop_index(
        "ix_item_warehouse_stock_warehouse_ref1c",
        table_name="item_warehouse_stock",
    )
    op.drop_table("item_warehouse_stock")
