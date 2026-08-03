"""Add future-supply-capture stage to build-batch check constraint.

Revision ID: 20260801_02
Revises: 20260801_01
"""

from alembic import op


revision = "20260801_02"
down_revision = "20260801_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ledger_build_batch") as batch_op:
        batch_op.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch_op.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', "
            "'execution_allocation', 'assembly_output_allocation', "
            "'replenishment_work_item', 'future_supply_capture', "
            "'snapshot_build', 'drum_schedule', 'shelf_projection')",
        )


def downgrade() -> None:
    with op.batch_alter_table("ledger_build_batch") as batch_op:
        batch_op.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch_op.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', "
            "'execution_allocation', 'assembly_output_allocation', "
            "'replenishment_work_item', 'snapshot_build', "
            "'drum_schedule', 'shelf_projection')",
        )
