"""Add Ledger generation ownership to DBR projections.

Revision ID: 20260723_08
Revises: 20260723_07
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_08"
down_revision = "20260723_07"
branch_labels = None
depends_on = None


def _add_lineage(table: str) -> None:
    op.add_column(
        table,
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True),
    )
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


def upgrade() -> None:
    _add_lineage("dbr_drum_schedule")
    _add_lineage("dbr_supermarket_position")
    _add_lineage("dbr_feeder_signal")

    op.drop_index(
        "ux_dbr_drum_schedule_one_active",
        table_name="dbr_drum_schedule",
    )
    op.create_index(
        "ux_dbr_drum_schedule_one_active",
        "dbr_drum_schedule",
        ["ledger_generation_id", "status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.drop_constraint(
        "ux_dbr_supermarket_position_item_warehouse",
        "dbr_supermarket_position",
        type_="unique",
    )
    op.create_unique_constraint(
        "ux_dbr_supermarket_position_generation_item_warehouse",
        "dbr_supermarket_position",
        ["ledger_generation_id", "item_id", "warehouse_ref1c"],
    )
    op.drop_constraint(
        "ux_dbr_feeder_signal_dedup_key",
        "dbr_feeder_signal",
        type_="unique",
    )
    op.create_unique_constraint(
        "ux_dbr_feeder_signal_generation_dedup_key",
        "dbr_feeder_signal",
        ["ledger_generation_id", "dedup_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "ux_dbr_feeder_signal_generation_dedup_key",
        "dbr_feeder_signal",
        type_="unique",
    )
    op.create_unique_constraint(
        "ux_dbr_feeder_signal_dedup_key",
        "dbr_feeder_signal",
        ["dedup_key"],
    )
    op.drop_constraint(
        "ux_dbr_supermarket_position_generation_item_warehouse",
        "dbr_supermarket_position",
        type_="unique",
    )
    op.create_unique_constraint(
        "ux_dbr_supermarket_position_item_warehouse",
        "dbr_supermarket_position",
        ["item_id", "warehouse_ref1c"],
    )
    op.drop_index(
        "ux_dbr_drum_schedule_one_active",
        table_name="dbr_drum_schedule",
    )
    op.create_index(
        "ux_dbr_drum_schedule_one_active",
        "dbr_drum_schedule",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    for table in (
        "dbr_feeder_signal",
        "dbr_supermarket_position",
        "dbr_drum_schedule",
    ):
        op.drop_index(f"ix_{table}_ledger_generation_id", table_name=table)
        op.drop_constraint(
            f"fk_{table}_ledger_generation",
            table,
            type_="foreignkey",
        )
        op.drop_column(table, "ledger_generation_id")
