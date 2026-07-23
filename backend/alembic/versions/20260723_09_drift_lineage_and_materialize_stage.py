"""Add drift lineage and a distinct reservation materialization stage.

Revision ID: 20260723_09
Revises: 20260723_08
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_09"
down_revision = "20260723_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mrp_drift_event",
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_mrp_drift_event_ledger_generation",
        "mrp_drift_event",
        "ledger_generation",
        ["ledger_generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_mrp_drift_event_ledger_generation_id",
        "mrp_drift_event",
        ["ledger_generation_id"],
        unique=False,
    )

    with op.batch_alter_table("ledger_build_batch") as batch:
        batch.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', 'execution_allocation', 'snapshot_build')",
        )


def downgrade() -> None:
    with op.batch_alter_table("ledger_build_batch") as batch:
        batch.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'execution_allocation', 'snapshot_build')",
        )

    op.drop_index(
        "ix_mrp_drift_event_ledger_generation_id",
        table_name="mrp_drift_event",
    )
    op.drop_constraint(
        "fk_mrp_drift_event_ledger_generation",
        "mrp_drift_event",
        type_="foreignkey",
    )
    op.drop_column("mrp_drift_event", "ledger_generation_id")
