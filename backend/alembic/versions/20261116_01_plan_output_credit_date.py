"""Add an opt-in immutable plan output credit date.

Revision ID: 20261116_01
Revises: 20260921_01 (verified deployed migration branch)
"""
from alembic import op
import sqlalchemy as sa

revision = "20261116_01"
down_revision = "20260921_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("production_plan_header", sa.Column("output_credit_from", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("production_plan_header", "output_credit_from")
