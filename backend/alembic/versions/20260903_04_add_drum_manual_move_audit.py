"""Add audit provenance for manual drum tile moves.

Revision ID: 20260903_04
Revises: 20260903_03
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_04"
down_revision = "20260903_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("drum_slot") as batch:
        batch.add_column(sa.Column("auto_slot_date", sa.Date(), nullable=True))
        batch.add_column(sa.Column("auto_resource_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("manual_moved_at", sa.TIMESTAMP(), nullable=True))
        batch.add_column(sa.Column("manual_moved_by", sa.String(100), nullable=True))
    op.execute("UPDATE drum_slot SET auto_slot_date = slot_date WHERE auto_slot_date IS NULL")
    op.execute("UPDATE drum_slot SET auto_resource_id = resource_id WHERE auto_resource_id IS NULL")


def downgrade() -> None:
    with op.batch_alter_table("drum_slot") as batch:
        batch.drop_column("manual_moved_by")
        batch.drop_column("manual_moved_at")
        batch.drop_column("auto_resource_id")
        batch.drop_column("auto_slot_date")
