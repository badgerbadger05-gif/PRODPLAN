"""merge spec-repair + workshop-source heads

Объединяет две параллельные ветки миграций, ответвившиеся от 20260611_01:
- 20260615_01 (add_workshop_source_to_line_states, ветка main)
- 20260624_01 (add_component_spec_ref1c, модуль ремонта спецификаций)

Пустая merge-ревизия: схему не меняет, только сводит alembic к одному head,
чтобы `alembic upgrade head` снова работал после слияния веток.

Revision ID: 20260626_01
Revises: 20260615_01, 20260624_01
Create Date: 2026-06-26 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260626_01"
down_revision = ("20260615_01", "20260624_01")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
