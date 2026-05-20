"""add supplier ref to items

Revision ID: 20260519_01
Revises: 20260508_01
Create Date: 2026-05-19 13:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260519_01"
down_revision = "20260508_01"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "items", "supplier_ref1c"):
        op.add_column("items", sa.Column("supplier_ref1c", sa.String(length=36), nullable=True))

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "items", "ix_items_supplier_ref1c"):
        op.create_index("ix_items_supplier_ref1c", "items", ["supplier_ref1c"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_index(inspector, "items", "ix_items_supplier_ref1c"):
        op.drop_index("ix_items_supplier_ref1c", table_name="items")

    inspector = sa.inspect(bind)
    if _has_column(inspector, "items", "supplier_ref1c"):
        op.drop_column("items", "supplier_ref1c")
