"""Persist the exact work calendar used by the accepted drum.

Revision ID: 20260904_05
Revises: 20260904_04
"""

from alembic import op
import sqlalchemy as sa

from app.models import CrossPlatformJSON


revision = "20260904_05"
down_revision = "20260904_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("drum_schedule") as batch:
        batch.add_column(
            sa.Column(
                "working_days",
                CrossPlatformJSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(
            sa.Column(
                "resource_horizon_ends",
                CrossPlatformJSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("drum_schedule") as batch:
        batch.drop_column("resource_horizon_ends")
        batch.drop_column("working_days")
