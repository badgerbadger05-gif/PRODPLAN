"""Nullable accounting price synchronized from the 1C price register.

Revision ID: 20260826_01
Revises: 20260821_01
"""
from alembic import op
import sqlalchemy as sa


revision = "20260826_01"
down_revision = "20260821_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "items",
        sa.Column("accounting_price", sa.DECIMAL(precision=15, scale=2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("items", "accounting_price")
