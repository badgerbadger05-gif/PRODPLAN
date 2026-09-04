"""Persist capacity inputs used by the accepted drum.

Revision ID: 20260904_06
Revises: 20260904_05
"""

from alembic import op
import sqlalchemy as sa

from app.models import CrossPlatformJSON


revision = "20260904_06"
down_revision = "20260904_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("drum_schedule") as batch:
        batch.add_column(
            sa.Column(
                "resource_daily_capacities",
                CrossPlatformJSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
    with op.batch_alter_table("drum_slot") as batch:
        batch.add_column(sa.Column("capacity_load", sa.DECIMAL(24, 12), nullable=True))
        batch.create_check_constraint(
            "ck_drum_slot_capacity_load_positive",
            "capacity_load IS NULL OR capacity_load > 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("drum_slot") as batch:
        batch.drop_constraint(
            "ck_drum_slot_capacity_load_positive",
            type_="check",
        )
        batch.drop_column("capacity_load")
    with op.batch_alter_table("drum_schedule") as batch:
        batch.drop_column("resource_daily_capacities")
