"""Persist plan-level output facts and readiness context on the drum.

Revision ID: 20260904_03
Revises: 20260904_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260904_03"
down_revision = "20260904_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("drum_slot") as batch:
        batch.add_column(
            sa.Column("accepted_plan_output_qty", sa.Numeric(15, 3), nullable=True)
        )
        batch.add_column(
            sa.Column("assembly_remaining_qty", sa.Numeric(15, 3), nullable=True)
        )

    with op.batch_alter_table("drum_capacity_gap") as batch:
        batch.add_column(sa.Column("planned_output_qty", sa.Numeric(15, 3), nullable=True))
        batch.add_column(
            sa.Column("accepted_plan_output_qty", sa.Numeric(15, 3), nullable=True)
        )
        batch.add_column(
            sa.Column("assembly_remaining_qty", sa.Numeric(15, 3), nullable=True)
        )
        batch.add_column(sa.Column("readiness_date", sa.Date(), nullable=True))
        batch.add_column(
            sa.Column("readiness_curve", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("action_manifest", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("unavailable_reasons", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(
            sa.Column("blocking_manifest", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )


def downgrade() -> None:
    with op.batch_alter_table("drum_capacity_gap") as batch:
        batch.drop_column("blocking_manifest")
        batch.drop_column("unavailable_reasons")
        batch.drop_column("action_manifest")
        batch.drop_column("readiness_curve")
        batch.drop_column("readiness_date")
        batch.drop_column("assembly_remaining_qty")
        batch.drop_column("accepted_plan_output_qty")
        batch.drop_column("planned_output_qty")

    with op.batch_alter_table("drum_slot") as batch:
        batch.drop_column("assembly_remaining_qty")
        batch.drop_column("accepted_plan_output_qty")
