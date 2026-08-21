"""Тип номенклатуры из 1С на карточке позиции.

Услуга (травление, гибка на стороне, пошив) не бывает на складе по своей
природе. Без этого признака проверка выпуска считала её закупной позицией,
которую нечем закрыть, и красила в блокеры каждую деталь, проходящую через
операцию.

Revision ID: 20260821_01
Revises: 20260805_02
"""
from alembic import op
import sqlalchemy as sa


revision = "20260821_01"
down_revision = "20260805_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("item_type", sa.String(length=50), nullable=True))
    op.create_index("ix_items_item_type", "items", ["item_type"])


def downgrade() -> None:
    op.drop_index("ix_items_item_type", table_name="items")
    op.drop_column("items", "item_type")
