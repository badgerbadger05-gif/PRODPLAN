"""add requested_qty and planned_qty to planned_order

Revision ID: 20251119_01
Revises: 20251009_07
Create Date: 2025-11-19 09:58:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20251119_01"
down_revision = "20251009_07"
branch_labels = None
depends_on = None


_TABLES = ("planned_order", "planned_purchase")
_COLUMNS = ("requested_qty", "planned_qty")
_QTY_TYPE = sa.Numeric(15, 3)


def _columns(table: str) -> set:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Раньше здесь был сырой PostgreSQL-DDL (ADD COLUMN IF NOT EXISTS /
    # ALTER COLUMN SET NOT NULL). Заменено на портируемые операции Alembic:
    # семантика на PostgreSQL прежняя, но цепочку теперь можно прогнать и на
    # SQLite (тест воспроизводимости схемы).
    for table in _TABLES:
        existing = _columns(table)
        for column in _COLUMNS:
            if column not in existing:
                op.add_column(table, sa.Column(column, _QTY_TYPE, nullable=True))

    for table in _TABLES:
        op.execute(
            sa.text(
                f"""
                UPDATE {table}
                   SET requested_qty = qty,
                       planned_qty = qty
                 WHERE requested_qty IS NULL
                    OR planned_qty IS NULL
                """
            )
        )

    # SQLite не поддерживает ALTER COLUMN ... SET NOT NULL вне batch-режима;
    # для проверки состава схемы это несущественно.
    if op.get_bind().dialect.name == "postgresql":
        for table in _TABLES:
            for column in _COLUMNS:
                op.alter_column(table, column, existing_type=_QTY_TYPE, nullable=False)


def downgrade() -> None:
    for table in reversed(_TABLES):
        existing = _columns(table)
        for column in reversed(_COLUMNS):
            if column in existing:
                op.drop_column(table, column)
