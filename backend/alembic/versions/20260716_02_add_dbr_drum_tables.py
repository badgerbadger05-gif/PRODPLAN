"""add DBR Phase 1 drum tables (program / schedule / slot / capacity gap)

Parallel DBR (drum-buffer-rope) planning module — Phase 1 storage. Adds the
production program, drum schedule, slots and capacity gaps, plus the
``fastener_categories`` JSON column on ``dbr_settings`` (kit classifier).

`create_all` is load-bearing in this project, so this DDL must match
backend/app/models.py (DbrProductionProgram / DbrProductionProgramItem /
DbrDrumSchedule / DbrDrumSlot / DbrDrumCapacityGap and the new DbrSettings
column).

Revision ID: 20260716_02
Revises: 20260716_01
Create Date: 2026-07-16 12:00:00
"""

from alembic import op
import sqlalchemy as sa

from app.models import CrossPlatformJSON


revision = "20260716_02"
down_revision = "20260716_01"
branch_labels = None
depends_on = None


def _has_table(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # dbr_settings.fastener_categories --------------------------------------
    if _has_table(inspector, "dbr_settings") and not _has_column(
        inspector, "dbr_settings", "fastener_categories"
    ):
        op.add_column(
            "dbr_settings",
            sa.Column(
                "fastener_categories",
                CrossPlatformJSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
        )

    # dbr_production_program -------------------------------------------------
    if not _has_table(inspector, "dbr_production_program"):
        op.create_table(
            "dbr_production_program",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("company", sa.String(length=255), nullable=True),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("from_date", sa.Date(), nullable=False),
            sa.Column("to_date", sa.Date(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("created_by", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_dbr_production_program_id", "dbr_production_program", ["id"])
        op.create_index("ix_dbr_production_program_status", "dbr_production_program", ["status"])

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "dbr_production_program_item"):
        op.create_table(
            "dbr_production_program_item",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("program_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("program_date", sa.Date(), nullable=False),
            sa.Column("qty", sa.Numeric(14, 3), nullable=False),
            sa.Column("comment", sa.TEXT(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["program_id"], ["dbr_production_program.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_dbr_production_program_item_id", "dbr_production_program_item", ["id"])
        op.create_index("ix_dbr_production_program_item_program_id", "dbr_production_program_item", ["program_id"])
        op.create_index("ix_dbr_production_program_item_item_id", "dbr_production_program_item", ["item_id"])
        op.create_index("ix_dbr_production_program_item_program_date", "dbr_production_program_item", ["program_date"])

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "dbr_drum_schedule"):
        op.create_table(
            "dbr_drum_schedule",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("period_from", sa.Date(), nullable=False),
            sa.Column("period_to", sa.Date(), nullable=False),
            sa.Column("source_program_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("config_snapshot", CrossPlatformJSON(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["source_program_id"], ["dbr_production_program.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_dbr_drum_schedule_id", "dbr_drum_schedule", ["id"])
        op.create_index("ix_dbr_drum_schedule_status", "dbr_drum_schedule", ["status"])
        op.create_index("ix_dbr_drum_schedule_source_program_id", "dbr_drum_schedule", ["source_program_id"])

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "dbr_drum_slot"):
        op.create_table(
            "dbr_drum_slot",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("schedule_id", sa.Integer(), nullable=False),
            sa.Column("slot_date", sa.Date(), nullable=False),
            sa.Column("planned_date", sa.Date(), nullable=False),
            sa.Column("resource_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("qty", sa.Numeric(14, 3), nullable=False),
            sa.Column("produced_qty", sa.Numeric(14, 3), nullable=False, server_default="0"),
            sa.Column("kit_status", sa.String(length=10), nullable=False, server_default="unknown"),
            sa.Column("shortage_json", CrossPlatformJSON(), nullable=True),
            sa.Column("release_status", sa.String(length=12), nullable=False, server_default="pending"),
            sa.Column("source_program_id", sa.Integer(), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["schedule_id"], ["dbr_drum_schedule.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["resource_id"], ["production_resources.resource_id"]),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["source_program_id"], ["dbr_production_program.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_dbr_drum_slot_id", "dbr_drum_slot", ["id"])
        op.create_index("ix_dbr_drum_slot_schedule_id", "dbr_drum_slot", ["schedule_id"])
        op.create_index("ix_dbr_drum_slot_slot_date", "dbr_drum_slot", ["slot_date"])
        op.create_index("ix_dbr_drum_slot_resource_id", "dbr_drum_slot", ["resource_id"])
        op.create_index("ix_dbr_drum_slot_item_id", "dbr_drum_slot", ["item_id"])
        op.create_index("ix_dbr_drum_slot_kit_status", "dbr_drum_slot", ["kit_status"])
        op.create_index("ix_dbr_drum_slot_release_status", "dbr_drum_slot", ["release_status"])
        op.create_index("ix_dbr_drum_slot_source_program_id", "dbr_drum_slot", ["source_program_id"])

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "dbr_drum_capacity_gap"):
        op.create_table(
            "dbr_drum_capacity_gap",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("schedule_id", sa.Integer(), nullable=False),
            sa.Column("gap_date", sa.Date(), nullable=True),
            sa.Column("resource_id", sa.Integer(), nullable=True),
            sa.Column("item_id", sa.Integer(), nullable=True),
            sa.Column("required_qty", sa.Numeric(14, 3), nullable=False),
            sa.Column("takt_qty", sa.Numeric(14, 3), nullable=False),
            sa.Column("gap_qty", sa.Numeric(14, 3), nullable=False),
            sa.Column("resolution", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["schedule_id"], ["dbr_drum_schedule.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["resource_id"], ["production_resources.resource_id"]),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_dbr_drum_capacity_gap_id", "dbr_drum_capacity_gap", ["id"])
        op.create_index("ix_dbr_drum_capacity_gap_schedule_id", "dbr_drum_capacity_gap", ["schedule_id"])
        op.create_index("ix_dbr_drum_capacity_gap_gap_date", "dbr_drum_capacity_gap", ["gap_date"])
        op.create_index("ix_dbr_drum_capacity_gap_resource_id", "dbr_drum_capacity_gap", ["resource_id"])
        op.create_index("ix_dbr_drum_capacity_gap_item_id", "dbr_drum_capacity_gap", ["item_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table in (
        "dbr_drum_capacity_gap",
        "dbr_drum_slot",
        "dbr_drum_schedule",
        "dbr_production_program_item",
        "dbr_production_program",
    ):
        inspector = sa.inspect(bind)
        if _has_table(inspector, table):
            op.drop_table(table)

    inspector = sa.inspect(bind)
    if _has_table(inspector, "dbr_settings") and _has_column(
        inspector, "dbr_settings", "fastener_categories"
    ):
        op.drop_column("dbr_settings", "fastener_categories")
