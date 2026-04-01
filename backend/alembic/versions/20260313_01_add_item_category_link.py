"""add item category link

Revision ID: 20260313_01
Revises: 20260312_01_add_planned_rework
Create Date: 2026-03-13 10:30:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260313_01"
down_revision = "20260312_01_add_planned_rework"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("category_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_items_category_id_item_categories",
        "items",
        "item_categories",
        ["category_id"],
        ["category_id"],
    )
    op.create_index("ix_items_category_id", "items", ["category_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_items_category_id", table_name="items")
    op.drop_constraint("fk_items_category_id_item_categories", "items", type_="foreignkey")
    op.drop_column("items", "category_id")
