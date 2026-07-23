"""Add generation lineage to physical and derived Ledger layers.

Revision ID: 20260723_06
Revises: 20260723_05
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260723_06"
down_revision = "20260723_05"
branch_labels = None
depends_on = None

_LEGACY_BATCH_KEY = "legacy-backfill-20260723-06"
_LEGACY_GENERATION_KEY = "legacy-rejected-20260723-06"


def _inserted_id(bind, table, values) -> int:
    return int(bind.execute(table.insert().values(**values).returning(table.c.id)).scalar_one())


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc)

    op.create_table(
        "physical_import_batch",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("batch_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="building", nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_watermarks", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('building', 'completed', 'rejected')",
            name="ck_physical_import_batch_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_key", name="uq_physical_import_batch_key"),
    )
    physical_batch = sa.table(
        "physical_import_batch",
        sa.column("id", sa.BigInteger()),
        sa.column("batch_key", sa.String()),
        sa.column("status", sa.String()),
        sa.column("cutoff", sa.DateTime(timezone=True)),
        sa.column("source_watermarks", sa.JSON()),
        sa.column("reason", sa.Text()),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    legacy_batch_id = _inserted_id(bind, physical_batch, {
        "batch_key": _LEGACY_BATCH_KEY,
        "status": "completed",
        "cutoff": now,
        "source_watermarks": {"source": "pre-lineage-schema"},
        "reason": "Backfill boundary; contents are diagnostic and not accepted truth",
        "completed_at": now,
    })

    op.add_column(
        "ledger_generation",
        sa.Column("physical_import_batch_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ledger_generation_physical_import_batch",
        "ledger_generation", "physical_import_batch",
        ["physical_import_batch_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index(
        "ix_ledger_generation_physical_import_batch_id",
        "ledger_generation", ["physical_import_batch_id"], unique=False,
    )
    bind.execute(
        sa.text(
            "UPDATE ledger_generation SET physical_import_batch_id = :batch_id "
            "WHERE physical_import_batch_id IS NULL"
        ),
        {"batch_id": legacy_batch_id},
    )

    ledger_generation = sa.table(
        "ledger_generation",
        sa.column("id", sa.BigInteger()),
        sa.column("generation_key", sa.String()),
        sa.column("status", sa.String()),
        sa.column("cutoff", sa.DateTime(timezone=True)),
        sa.column("source_watermarks", sa.JSON()),
        sa.column("capabilities", sa.JSON()),
        sa.column("physical_import_batch_id", sa.BigInteger()),
        sa.column("algorithm_version", sa.String()),
        sa.column("replay_version", sa.String()),
        sa.column("reason", sa.Text()),
    )
    legacy_generation_id = _inserted_id(bind, ledger_generation, {
        "generation_key": _LEGACY_GENERATION_KEY,
        "status": "rejected",
        "cutoff": now,
        "source_watermarks": {"source": "pre-lineage-schema"},
        "capabilities": {},
        "physical_import_batch_id": legacy_batch_id,
        "algorithm_version": "legacy/backfill",
        "replay_version": "none",
        "reason": "Pre-lineage rows are untrusted and must be rebuilt",
    })
    op.alter_column(
        "ledger_generation", "physical_import_batch_id",
        existing_type=sa.BigInteger(), nullable=False,
    )

    op.create_table(
        "ledger_build_batch",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("batch_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="building", nullable=False),
        sa.Column("algorithm_version", sa.String(length=128), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "stage IN ('physical_import', 'reservation_replay', "
            "'execution_allocation', 'snapshot_build')",
            name="ck_ledger_build_batch_stage",
        ),
        sa.CheckConstraint(
            "status IN ('building', 'completed', 'rejected')",
            name="ck_ledger_build_batch_status",
        ),
        sa.ForeignKeyConstraint(
            ["ledger_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ledger_generation_id", "stage", "batch_key",
            name="uq_ledger_build_batch_generation_stage_key",
        ),
    )
    op.create_index(
        "ix_ledger_build_batch_ledger_generation_id",
        "ledger_build_batch", ["ledger_generation_id"], unique=False,
    )

    # Add nullable first, backfill every pre-lineage row to rejected lineage,
    # then enforce NOT NULL. This prevents migration from blessing legacy data.
    column_specs = (
        ("stock_ledger_entry", "ingest_batch_id", legacy_batch_id, "physical_import_batch"),
        ("stock_ledger_anchor", "ingest_batch_id", legacy_batch_id, "physical_import_batch"),
        ("stock_bin", "ledger_generation_id", legacy_generation_id, "ledger_generation"),
        ("reservation_entry", "ledger_generation_id", legacy_generation_id, "ledger_generation"),
        ("reservation_event", "ledger_generation_id", legacy_generation_id, "ledger_generation"),
        ("mrp_execution_allocation", "ledger_generation_id", legacy_generation_id, "ledger_generation"),
    )
    for table_name, column_name, backfill_id, target_table in column_specs:
        op.add_column(table_name, sa.Column(column_name, sa.BigInteger(), nullable=True))
        bind.execute(
            sa.text(
                f"UPDATE {table_name} SET {column_name} = :backfill_id "
                f"WHERE {column_name} IS NULL"
            ),
            {"backfill_id": backfill_id},
        )
        op.alter_column(
            table_name, column_name,
            existing_type=sa.BigInteger(), nullable=False,
        )
        op.create_foreign_key(
            f"fk_{table_name}_{column_name}",
            table_name, target_table, [column_name], ["id"], ondelete="RESTRICT",
        )
        op.create_index(
            f"ix_{table_name}_{column_name}",
            table_name, [column_name], unique=False,
        )

    op.add_column(
        "stock_ledger_entry",
        sa.Column("source_content_hash", sa.String(length=64), nullable=True),
    )
    sle = sa.table(
        "stock_ledger_entry",
        sa.column("id", sa.BigInteger()),
        sa.column("source_content_hash", sa.String()),
    )
    for (sle_id,) in bind.execute(sa.select(sle.c.id)):
        bind.execute(
            sle.update().where(sle.c.id == sle_id).values(
                source_content_hash=f"legacy-{int(sle_id)}",
            )
        )
    op.alter_column(
        "stock_ledger_entry", "source_content_hash",
        existing_type=sa.String(length=64), nullable=False,
    )

    op.drop_constraint(
        "ux_stock_ledger_entry_recorder_line",
        "stock_ledger_entry", type_="unique",
    )
    op.create_unique_constraint(
        "ux_stock_ledger_entry_recorder_line",
        "stock_ledger_entry",
        [
            "recorder_type", "recorder_ref", "line_no",
            "source_content_hash", "ingest_batch_id",
        ],
    )
    op.drop_constraint(
        "ux_stock_bin_ledger_key", "stock_bin", type_="unique",
    )
    op.create_unique_constraint(
        "ux_stock_bin_ledger_key", "stock_bin",
        [
            "ledger_generation_id", "item_id", "characteristic_ref",
            "organization_ref", "warehouse_ref1c",
        ],
    )
    op.drop_constraint(
        "ux_reservation_entry_req_mode", "reservation_entry", type_="unique",
    )
    op.create_unique_constraint(
        "ux_reservation_entry_req_mode", "reservation_entry",
        ["ledger_generation_id", "requirement_id", "realization_mode"],
    )
    op.drop_constraint(
        "ux_reservation_event_idempotency", "reservation_event", type_="unique",
    )
    op.create_unique_constraint(
        "ux_reservation_event_idempotency", "reservation_event",
        ["ledger_generation_id", "idempotency_key"],
    )
    op.drop_constraint(
        "ux_mrp_execution_allocation_fact",
        "mrp_execution_allocation", type_="unique",
    )
    op.create_unique_constraint(
        "ux_mrp_execution_allocation_fact",
        "mrp_execution_allocation",
        [
            "ledger_generation_id", "requirement_id", "bucket_id", "fact_type",
            "fact_ref", "fact_line_ref", "allocation_kind",
        ],
    )

    op.create_table(
        "stock_ledger_fact_supersession",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("old_sle_id", sa.BigInteger(), nullable=False),
        sa.Column("new_sle_id", sa.BigInteger(), nullable=True),
        sa.Column("import_batch_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["old_sle_id"], ["stock_ledger_entry.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["new_sle_id"], ["stock_ledger_entry.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["import_batch_id"], ["physical_import_batch.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "import_batch_id", "old_sle_id",
            name="uq_stock_ledger_supersession_transition",
        ),
    )
    op.create_index(
        "ix_stock_ledger_fact_supersession_import_batch_id",
        "stock_ledger_fact_supersession", ["import_batch_id"], unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index(
        "ix_stock_ledger_fact_supersession_import_batch_id",
        table_name="stock_ledger_fact_supersession",
    )
    op.drop_table("stock_ledger_fact_supersession")

    op.drop_constraint(
        "ux_mrp_execution_allocation_fact",
        "mrp_execution_allocation", type_="unique",
    )
    op.create_unique_constraint(
        "ux_mrp_execution_allocation_fact",
        "mrp_execution_allocation",
        [
            "requirement_id", "bucket_id", "fact_type", "fact_ref",
            "fact_line_ref", "allocation_kind",
        ],
    )
    op.drop_constraint(
        "ux_reservation_event_idempotency", "reservation_event", type_="unique",
    )
    op.create_unique_constraint(
        "ux_reservation_event_idempotency",
        "reservation_event", ["idempotency_key"],
    )
    op.drop_constraint(
        "ux_reservation_entry_req_mode", "reservation_entry", type_="unique",
    )
    op.create_unique_constraint(
        "ux_reservation_entry_req_mode",
        "reservation_entry", ["requirement_id", "realization_mode"],
    )
    op.drop_constraint("ux_stock_bin_ledger_key", "stock_bin", type_="unique")
    op.create_unique_constraint(
        "ux_stock_bin_ledger_key", "stock_bin",
        ["item_id", "characteristic_ref", "organization_ref", "warehouse_ref1c"],
    )
    op.drop_constraint(
        "ux_stock_ledger_entry_recorder_line",
        "stock_ledger_entry", type_="unique",
    )
    op.create_unique_constraint(
        "ux_stock_ledger_entry_recorder_line",
        "stock_ledger_entry", ["recorder_type", "recorder_ref", "line_no"],
    )
    op.drop_column("stock_ledger_entry", "source_content_hash")

    for table_name, column_name, _, _ in reversed((
        ("stock_ledger_entry", "ingest_batch_id", None, None),
        ("stock_ledger_anchor", "ingest_batch_id", None, None),
        ("stock_bin", "ledger_generation_id", None, None),
        ("reservation_entry", "ledger_generation_id", None, None),
        ("reservation_event", "ledger_generation_id", None, None),
        ("mrp_execution_allocation", "ledger_generation_id", None, None),
    )):
        op.drop_index(f"ix_{table_name}_{column_name}", table_name=table_name)
        op.drop_constraint(
            f"fk_{table_name}_{column_name}", table_name, type_="foreignkey",
        )
        op.drop_column(table_name, column_name)

    op.drop_index(
        "ix_ledger_build_batch_ledger_generation_id",
        table_name="ledger_build_batch",
    )
    op.drop_table("ledger_build_batch")
    op.drop_index(
        "ix_ledger_generation_physical_import_batch_id",
        table_name="ledger_generation",
    )
    op.drop_constraint(
        "fk_ledger_generation_physical_import_batch",
        "ledger_generation", type_="foreignkey",
    )
    op.drop_column("ledger_generation", "physical_import_batch_id")
    bind.execute(
        sa.text("DELETE FROM ledger_generation WHERE generation_key = :key"),
        {"key": _LEGACY_GENERATION_KEY},
    )
    bind.execute(
        sa.text("DELETE FROM physical_import_batch WHERE batch_key = :key"),
        {"key": _LEGACY_BATCH_KEY},
    )
    op.drop_table("physical_import_batch")
