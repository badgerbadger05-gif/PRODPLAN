"""Bind DBR decisions to immutable PlanningRun freeze snapshots.

Revision ID: 20260723_16
Revises: 20260723_15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_16"
down_revision = "20260723_15"
branch_labels = None
depends_on = None


def _add_run_lineage(table: str, *, add_generation: bool) -> None:
    op.add_column(table, sa.Column("source_run_id", sa.Integer(), nullable=True))
    if add_generation:
        op.add_column(table, sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True))
    op.add_column(table, sa.Column("freeze_version", sa.Integer(), nullable=True))
    op.create_foreign_key(
        f"fk_{table}_source_run",
        table,
        "planning_run",
        ["source_run_id"],
        ["run_id"],
        ondelete="RESTRICT",
    )
    if add_generation:
        op.create_foreign_key(
            f"fk_{table}_ledger_generation",
            table,
            "ledger_generation",
            ["ledger_generation_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            f"ix_{table}_ledger_generation_id",
            table,
            ["ledger_generation_id"],
            unique=False,
        )
    op.create_index(
        f"ix_{table}_source_run_id", table, ["source_run_id"], unique=False
    )


def upgrade() -> None:
    _add_run_lineage("dbr_production_program", add_generation=True)
    _add_run_lineage("dbr_drum_schedule_program", add_generation=True)
    _add_run_lineage("dbr_drum_slot", add_generation=True)
    _add_run_lineage("dbr_feeder_signal", add_generation=False)

    op.create_check_constraint(
        "ck_dbr_program_lineage_tuple",
        "dbr_production_program",
        "(source_run_id IS NULL AND ledger_generation_id IS NULL AND freeze_version IS NULL) OR "
        "(source_run_id IS NOT NULL AND ledger_generation_id IS NOT NULL AND freeze_version IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_dbr_schedule_program_lineage_tuple",
        "dbr_drum_schedule_program",
        "(source_run_id IS NULL AND ledger_generation_id IS NULL AND freeze_version IS NULL) OR "
        "(source_run_id IS NOT NULL AND ledger_generation_id IS NOT NULL AND freeze_version IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_dbr_drum_slot_lineage_tuple",
        "dbr_drum_slot",
        "(source_run_id IS NULL AND ledger_generation_id IS NULL AND freeze_version IS NULL) OR "
        "(source_run_id IS NOT NULL AND ledger_generation_id IS NOT NULL AND freeze_version IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_dbr_feeder_signal_run_freeze_pair",
        "dbr_feeder_signal",
        "(source_run_id IS NULL AND freeze_version IS NULL) OR "
        "(source_run_id IS NOT NULL AND freeze_version IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_dbr_feeder_signal_run_freeze_pair",
        "dbr_feeder_signal",
        type_="check",
    )
    op.drop_constraint(
        "ck_dbr_drum_slot_lineage_tuple", "dbr_drum_slot", type_="check"
    )
    op.drop_constraint(
        "ck_dbr_schedule_program_lineage_tuple",
        "dbr_drum_schedule_program",
        type_="check",
    )
    op.drop_constraint(
        "ck_dbr_program_lineage_tuple", "dbr_production_program", type_="check"
    )

    for table, has_generation in (
        ("dbr_feeder_signal", False),
        ("dbr_drum_slot", True),
        ("dbr_drum_schedule_program", True),
        ("dbr_production_program", True),
    ):
        op.drop_index(f"ix_{table}_source_run_id", table_name=table)
        op.drop_constraint(f"fk_{table}_source_run", table, type_="foreignkey")
        op.drop_column(table, "freeze_version")
        if has_generation:
            op.drop_index(f"ix_{table}_ledger_generation_id", table_name=table)
            op.drop_constraint(
                f"fk_{table}_ledger_generation", table, type_="foreignkey"
            )
            op.drop_column(table, "ledger_generation_id")
        op.drop_column(table, "source_run_id")
