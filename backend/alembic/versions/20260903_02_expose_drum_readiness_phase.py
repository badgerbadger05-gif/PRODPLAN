"""Persist readiness provenance on canonical drum rows.

Revision ID: 20260903_02
Revises: 20260903_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_02"
down_revision = "20260903_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("drum_slot") as batch:
        batch.add_column(
            sa.Column(
                "readiness_phase",
                sa.String(20),
                nullable=False,
                server_default="unavailable",
            )
        )
        batch.create_check_constraint(
            "ck_drum_slot_readiness_phase",
            "readiness_phase IN ('ready', 'blocked', 'unavailable')",
        )
    with op.batch_alter_table("drum_capacity_gap") as batch:
        batch.add_column(
            sa.Column(
                "readiness_phase",
                sa.String(20),
                nullable=False,
                server_default="unavailable",
            )
        )
        batch.create_check_constraint(
            "ck_drum_gap_readiness_phase",
            "readiness_phase IN ('ready', 'blocked', 'unavailable', 'mixed')",
        )


def downgrade() -> None:
    with op.batch_alter_table("drum_capacity_gap") as batch:
        batch.drop_constraint("ck_drum_gap_readiness_phase", type_="check")
        batch.drop_column("readiness_phase")
    with op.batch_alter_table("drum_slot") as batch:
        batch.drop_constraint("ck_drum_slot_readiness_phase", type_="check")
        batch.drop_column("readiness_phase")
