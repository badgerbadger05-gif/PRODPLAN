"""add pinned flag to planning_run for retention policy

Revision ID: 20250929_03
Revises: 20250925_02
Create Date: 2025-09-29 09:45:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20250929_03'
down_revision = '20250925_02'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add 'pinned' boolean column to planning_run with default FALSE (not nullable)
    op.add_column(
        'planning_run',
        sa.Column('pinned', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')),
    )
    # Optional: if you wish to remove server_default after backfilling existing rows uncomment:
    # with op.get_bind() as conn:
    #     conn.execute(sa.text("ALTER TABLE planning_run ALTER COLUMN pinned DROP DEFAULT"))


def downgrade() -> None:
    op.drop_column('planning_run', 'pinned')