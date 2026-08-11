"""Allow the executor-less, known rework reservation mode.

Revision ID: 20260802_01
Revises: 20260801_02
"""

from alembic import op


revision = "20260802_01"
down_revision = "20260801_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reservation_entry") as batch_op:
        batch_op.drop_constraint(
            "ck_reservation_entry_replenishment_flow", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_reservation_entry_replenishment_flow",
            "realization_mode IN ('make', 'buy', 'rework')",
        )


def downgrade() -> None:
    with op.batch_alter_table("reservation_entry") as batch_op:
        batch_op.drop_constraint(
            "ck_reservation_entry_replenishment_flow", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_reservation_entry_replenishment_flow",
            "realization_mode IN ('make', 'buy')",
        )
