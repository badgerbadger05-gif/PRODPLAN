"""fix nulls in production_resources capacity and work_hours_per_day, enforce NOT NULL

Revision ID: 20251009_07
Revises: 20251007_06
Create Date: 2025-10-09 10:21:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20251009_07'
down_revision = '20251007_06'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Backfill NULLs with safe defaults to satisfy NOT NULL constraints
    op.execute(sa.text("""
        UPDATE production_resources
        SET capacity = 0
        WHERE capacity IS NULL
    """))
    op.execute(sa.text("""
        UPDATE production_resources
        SET work_hours_per_day = 8
        WHERE work_hours_per_day IS NULL
    """))

    # Enforce NOT NULL on numeric fields used by API responses
    op.alter_column(
        'production_resources',
        'capacity',
        existing_type=sa.Numeric(10, 2),
        nullable=False
    )
    op.alter_column(
        'production_resources',
        'work_hours_per_day',
        existing_type=sa.Numeric(4, 2),
        nullable=False
    )


def downgrade() -> None:
    # Relax NOT NULL constraints (values remain)
    op.alter_column(
        'production_resources',
        'capacity',
        existing_type=sa.Numeric(10, 2),
        nullable=True
    )
    op.alter_column(
        'production_resources',
        'work_hours_per_day',
        existing_type=sa.Numeric(4, 2),
        nullable=True
    )