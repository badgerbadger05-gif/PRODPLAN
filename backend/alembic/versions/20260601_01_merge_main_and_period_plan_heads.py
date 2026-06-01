"""merge main and period-plan migration heads

Revision ID: 20260601_01
Revises: 20260520_09, 20260522_07
Create Date: 2026-06-01 13:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260601_01"
down_revision = ("20260520_09", "20260522_07")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
