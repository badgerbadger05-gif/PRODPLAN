"""Add assembly readiness and split warehouse 3/4 kitting resources.

Revision ID: 20260903_01
Revises: 20260826_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260903_01"
down_revision = "20260826_01"
branch_labels = None
depends_on = None


RESOURCE_3 = "Комплектовка — склад №3"
RESOURCE_4 = "Комплектовка — склад №4"

KIND_3_REF = "0b2be078-8f25-11f1-8f09-9ee51454587f"
KIND_4_REF = "0659c61e-8f11-11f1-8f09-9ee51454587f"
WAREHOUSE_3_REF = "ab951594-75e8-11f1-8002-9ee51454587f"
WAREHOUSE_4_REF = "15377c4e-bf96-11f0-95ca-9ee51454587f"


def _bigint():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _seed_kitting_resource(bind, name: str, kind_ref: str, warehouse_ref: str) -> None:
    resource_id = bind.execute(
        sa.text("SELECT resource_id FROM production_resources WHERE resource_name = :name"),
        {"name": name},
    ).scalar()
    if resource_id is None:
        bind.execute(
            sa.text(
                """
                INSERT INTO production_resources
                    (resource_name, planning_offset, planning_horizon, capacity,
                     work_schedule, work_hours_per_day, buffer_days, created_at, updated_at)
                VALUES
                    (:name, 0, 30, 0, '5/2', 8, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            ),
            {"name": name},
        )
        resource_id = bind.execute(
            sa.text("SELECT resource_id FROM production_resources WHERE resource_name = :name"),
            {"name": name},
        ).scalar_one()

    kind_id = bind.execute(
        sa.text("SELECT id FROM production_kinds WHERE ref_1c = :ref"),
        {"ref": kind_ref},
    ).scalar()
    warehouse_exists = bind.execute(
        sa.text("SELECT 1 FROM stock_warehouses WHERE warehouse_ref1c = :ref"),
        {"ref": warehouse_ref},
    ).scalar()
    if kind_id is not None:
        existing = bind.execute(
            sa.text(
                "SELECT resource_id FROM resource_production_kinds "
                "WHERE production_kind_id = :kind_id"
            ),
            {"kind_id": int(kind_id)},
        ).scalar()
        if existing is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO resource_production_kinds
                        (resource_id, production_kind_id, created_at, updated_at)
                    VALUES (:resource_id, :kind_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {"resource_id": int(resource_id), "kind_id": int(kind_id)},
            )
        elif int(existing) != int(resource_id):
            raise RuntimeError(
                f"production kind {kind_ref} is already assigned to resource {existing}"
            )

    if warehouse_exists:
        binding = bind.execute(
            sa.text(
                "SELECT binding_id FROM workshop_warehouse_bindings "
                "WHERE workshop_id = :resource_id"
            ),
            {"resource_id": int(resource_id)},
        ).scalar()
        if binding is None:
            bind.execute(
                sa.text(
                    """
                    INSERT INTO workshop_warehouse_bindings
                        (workshop_id, warehouse_ref1c, production_warehouse_ref1c,
                         created_at, updated_at)
                    VALUES
                        (:resource_id, :warehouse_ref, :warehouse_ref,
                         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                ),
                {"resource_id": int(resource_id), "warehouse_ref": warehouse_ref},
            )


def upgrade() -> None:
    with op.batch_alter_table("ledger_build_batch") as batch:
        batch.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', 'execution_allocation', "
            "'assembly_output_allocation', 'replenishment_work_item', "
            "'future_supply_capture', 'snapshot_build', 'assembly_readiness', "
            "'drum_schedule', 'shelf_projection')",
        )
    op.create_table(
        "assembly_readiness",
        sa.Column("id", _bigint(), autoincrement=True, nullable=False),
        sa.Column(
            "ledger_generation_id",
            _bigint(),
            sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "assembly_queue_line_id",
            _bigint(),
            sa.ForeignKey("assembly_queue_line.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("open_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("ready_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("blocker_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocking_manifest", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("evidence_signature", sa.String(64), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("open_qty >= 0", name="ck_assembly_readiness_open_qty_nonnegative"),
        sa.CheckConstraint("ready_qty >= 0", name="ck_assembly_readiness_ready_qty_nonnegative"),
        sa.CheckConstraint("ready_qty <= open_qty", name="ck_assembly_readiness_ready_not_above_open"),
        sa.CheckConstraint(
            "status IN ('ready', 'partial', 'blocked', 'unavailable')",
            name="ck_assembly_readiness_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ledger_generation_id",
            "assembly_queue_line_id",
            name="uq_assembly_readiness_generation_queue_line",
        ),
    )
    op.create_index(
        "ix_assembly_readiness_generation_status",
        "assembly_readiness",
        ["ledger_generation_id", "status"],
    )
    op.create_index(
        "ix_assembly_readiness_ledger_generation_id",
        "assembly_readiness",
        ["ledger_generation_id"],
    )
    op.create_index(
        "ix_assembly_readiness_assembly_queue_line_id",
        "assembly_readiness",
        ["assembly_queue_line_id"],
    )

    bind = op.get_bind()
    _seed_kitting_resource(bind, RESOURCE_3, KIND_3_REF, WAREHOUSE_3_REF)
    _seed_kitting_resource(bind, RESOURCE_4, KIND_4_REF, WAREHOUSE_4_REF)


def downgrade() -> None:
    # Master-data rows are intentionally retained.  The resources may already
    # have existed before this revision, and a schema downgrade must not erase
    # operator-owned bindings merely because their names match the seed.
    op.drop_index("ix_assembly_readiness_assembly_queue_line_id", table_name="assembly_readiness")
    op.drop_index("ix_assembly_readiness_ledger_generation_id", table_name="assembly_readiness")
    op.drop_index("ix_assembly_readiness_generation_status", table_name="assembly_readiness")
    op.drop_table("assembly_readiness")
    with op.batch_alter_table("ledger_build_batch") as batch:
        batch.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', 'execution_allocation', "
            "'assembly_output_allocation', 'replenishment_work_item', "
            "'future_supply_capture', 'snapshot_build', 'drum_schedule', "
            "'shelf_projection')",
        )
