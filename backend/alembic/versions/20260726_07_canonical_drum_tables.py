"""Create canonical drum/assembly-queue tables replacing legacy dbr tables.

Revision ID: 20260726_07
Revises: 20260726_06
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_07"
down_revision = "20260726_06"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _bigint():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_table(bind, "assembly_queue_line"):
        op.create_table(
            "assembly_queue_line",
            sa.Column("id", _bigint(), autoincrement=True, nullable=False),
            sa.Column(
                "ledger_generation_id",
                _bigint(),
                sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "planning_run_id",
                sa.Integer(),
                sa.ForeignKey("planning_run.run_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "plan_id",
                sa.Integer(),
                sa.ForeignKey("production_plan_header.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "plan_line_id",
                sa.Integer(),
                sa.ForeignKey("production_plan_line.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "item_id",
                sa.Integer(),
                sa.ForeignKey("items.item_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column("bucket_date", sa.Date(), nullable=False),
            sa.Column("period_from", sa.Date(), nullable=False, index=True),
            sa.Column("period_to", sa.Date(), nullable=False, index=True),
            sa.Column("planned_output_qty", sa.DECIMAL(15, 3), nullable=False),
            sa.Column("accepted_plan_output_qty", sa.DECIMAL(15, 3), nullable=False),
            sa.Column("assembly_remaining_qty", sa.DECIMAL(15, 3), nullable=False),
            sa.Column(
                "original_priority",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column("sort_key", sa.String(128), nullable=False),
            sa.Column(
                "line_status",
                sa.String(20),
                nullable=False,
                server_default="open",
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.CheckConstraint(
                "period_to >= period_from",
                name="ck_assembly_queue_line_period",
            ),
            sa.CheckConstraint(
                "planned_output_qty >= 0",
                name="ck_assembly_queue_line_planned_qty_nonnegative",
            ),
            sa.CheckConstraint(
                "accepted_plan_output_qty >= 0",
                name="ck_assembly_queue_line_accepted_qty_nonnegative",
            ),
            sa.CheckConstraint(
                "assembly_remaining_qty >= 0",
                name="ck_assembly_queue_line_remaining_qty_nonnegative",
            ),
            sa.UniqueConstraint(
                "ledger_generation_id",
                "plan_line_id",
                name="uq_assembly_queue_line_generation_plan_line",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_assembly_queue_line_generation_status",
            "assembly_queue_line",
            ["ledger_generation_id", "line_status"],
        )
        op.create_index(
            "ix_assembly_queue_line_generation_sort",
            "assembly_queue_line",
            ["ledger_generation_id", "sort_key"],
        )
        op.create_index(
            "ix_assembly_queue_line_plan_line",
            "assembly_queue_line",
            ["plan_id", "plan_line_id"],
        )

    if not _has_table(bind, "drum_schedule"):
        op.create_table(
            "drum_schedule",
            sa.Column("id", _bigint(), autoincrement=True, nullable=False),
            sa.Column(
                "ledger_generation_id",
                _bigint(),
                sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
            sa.Column("algorithm_version", sa.String(64), nullable=False),
            sa.Column("schedule_from", sa.Date(), nullable=False),
            sa.Column("schedule_to", sa.Date(), nullable=False),
            sa.Column(
                "queue_signature",
                sa.String(64),
                nullable=False,
            ),
            sa.Column(
                "slot_signature",
                sa.String(64),
                nullable=False,
            ),
            sa.Column(
                "gap_signature",
                sa.String(64),
                nullable=False,
            ),
            sa.Column("slot_row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("gap_row_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "total_open_qty",
                sa.DECIMAL(15, 3),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "total_slot_qty",
                sa.DECIMAL(15, 3),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "total_gap_qty",
                sa.DECIMAL(15, 3),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "metrics",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.CheckConstraint(
                "slot_row_count >= 0",
                name="ck_drum_schedule_slot_row_count_nonnegative",
            ),
            sa.CheckConstraint(
                "gap_row_count >= 0",
                name="ck_drum_schedule_gap_row_count_nonnegative",
            ),
            sa.CheckConstraint(
                "total_open_qty >= 0",
                name="ck_drum_schedule_total_open_qty_nonnegative",
            ),
            sa.CheckConstraint(
                "total_slot_qty >= 0",
                name="ck_drum_schedule_total_slot_qty_nonnegative",
            ),
            sa.CheckConstraint(
                "total_gap_qty >= 0",
                name="ck_drum_schedule_total_gap_qty_nonnegative",
            ),
            sa.UniqueConstraint(
                "ledger_generation_id",
                name="uq_drum_schedule_generation",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_drum_schedule_generation",
            "drum_schedule",
            ["ledger_generation_id"],
        )
        op.create_index(
            "ix_drum_schedule_algorithm",
            "drum_schedule",
            ["algorithm_version"],
        )

    if not _has_table(bind, "drum_slot"):
        op.create_table(
            "drum_slot",
            sa.Column("id", _bigint(), autoincrement=True, nullable=False),
            sa.Column(
                "drum_schedule_id",
                _bigint(),
                sa.ForeignKey("drum_schedule.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "assembly_queue_line_id",
                _bigint(),
                sa.ForeignKey("assembly_queue_line.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "plan_id",
                sa.Integer(),
                sa.ForeignKey("production_plan_header.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "plan_line_id",
                sa.Integer(),
                sa.ForeignKey("production_plan_line.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "item_id",
                sa.Integer(),
                sa.ForeignKey("items.item_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "resource_id",
                sa.Integer(),
                sa.ForeignKey("production_resources.resource_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column("slot_date", sa.Date(), nullable=False),
            sa.Column("slot_qty", sa.DECIMAL(15, 3), nullable=False),
            sa.Column("planned_output_qty", sa.DECIMAL(15, 3), nullable=False),
            sa.Column("slot_ordinal", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "original_priority",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.CheckConstraint(
                "slot_qty > 0",
                name="ck_drum_slot_qty_positive",
            ),
            sa.CheckConstraint(
                "slot_ordinal >= 0",
                name="ck_drum_slot_ordinal_nonnegative",
            ),
            sa.UniqueConstraint(
                "drum_schedule_id",
                "assembly_queue_line_id",
                "slot_ordinal",
                name="uq_drum_slot_schedule_line_ordinal",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_drum_slot_schedule_date",
            "drum_slot",
            ["drum_schedule_id", "slot_date"],
        )
        op.create_index(
            "ix_drum_slot_resource_date",
            "drum_slot",
            ["resource_id", "slot_date"],
        )
        op.create_index("ix_drum_slot_item", "drum_slot", ["item_id"])
        op.create_index("ix_drum_slot_plan", "drum_slot", ["plan_id", "plan_line_id"])

    if not _has_table(bind, "drum_capacity_gap"):
        op.create_table(
            "drum_capacity_gap",
            sa.Column("id", _bigint(), autoincrement=True, nullable=False),
            sa.Column(
                "drum_schedule_id",
                _bigint(),
                sa.ForeignKey("drum_schedule.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "assembly_queue_line_id",
                _bigint(),
                sa.ForeignKey("assembly_queue_line.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "plan_id",
                sa.Integer(),
                sa.ForeignKey("production_plan_header.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "plan_line_id",
                sa.Integer(),
                sa.ForeignKey("production_plan_line.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "resource_id",
                sa.Integer(),
                sa.ForeignKey("production_resources.resource_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "item_id",
                sa.Integer(),
                sa.ForeignKey("items.item_id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column("gap_date", sa.Date(), nullable=False),
            sa.Column("required_qty", sa.DECIMAL(15, 3), nullable=False),
            sa.Column("available_capacity", sa.DECIMAL(15, 3), nullable=False),
            sa.Column("gap_qty", sa.DECIMAL(15, 3), nullable=False),
            sa.Column(
                "original_priority",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column(
                "created_at",
                sa.TIMESTAMP(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.CheckConstraint(
                "required_qty >= 0",
                name="ck_drum_gap_required_qty_nonnegative",
            ),
            sa.CheckConstraint(
                "available_capacity >= 0",
                name="ck_drum_gap_available_capacity_nonnegative",
            ),
            sa.CheckConstraint(
                "gap_qty >= 0",
                name="ck_drum_gap_qty_nonnegative",
            ),
            sa.UniqueConstraint(
                "drum_schedule_id",
                "assembly_queue_line_id",
                "resource_id",
                "gap_date",
                name="uq_drum_gap_schedule_line_resource_date",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_drum_gap_schedule_resource_date",
            "drum_capacity_gap",
            ["drum_schedule_id", "resource_id", "gap_date"],
        )
        op.create_index(
            "ix_drum_gap_schedule_item",
            "drum_capacity_gap",
            ["drum_schedule_id", "item_id"],
        )
        op.create_index(
            "ix_drum_gap_item_resource",
            "drum_capacity_gap",
            ["item_id", "resource_id"],
        )

    with op.batch_alter_table("ledger_build_batch") as batch:
        batch.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', 'execution_allocation', "
            "'assembly_output_allocation', 'snapshot_build', 'drum_schedule')",
        )


def downgrade() -> None:
    with op.batch_alter_table("ledger_build_batch") as batch:
        batch.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', 'execution_allocation', "
            "'assembly_output_allocation', 'snapshot_build')",
        )
    op.drop_table("drum_capacity_gap")
    op.drop_table("drum_slot")
    op.drop_table("drum_schedule")
    op.drop_table("assembly_queue_line")
