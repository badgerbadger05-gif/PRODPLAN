"""Add unified replenishment work-item stage and table.

Revision ID: 20260726_10
Revises: 20260726_09
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_10"
down_revision = "20260726_09"
branch_labels = None
depends_on = None


def _bigint():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _has_table(inspector: sa.Inspector, table: str) -> bool:
    return table in set(inspector.get_table_names())


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    return column in {row["name"] for row in inspector.get_columns(table)}


def upgrade() -> None:
    op.create_table(
        "replenishment_work_item",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("ledger_generation_id", _bigint(), sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("reservation_id", _bigint(), sa.ForeignKey("reservation_entry.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("production_plan_header.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("planning_run.run_id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("requirement_id", sa.Integer(), sa.ForeignKey("mrp_requirement.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("replenishment_method", sa.String(10), nullable=False),
        sa.Column("replenishment_required_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("replenishment_fulfilled_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("replenishment_remaining_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("execution_document_kind", sa.String(24), nullable=False, server_default=""),
        sa.Column("execution_document_id", _bigint(), nullable=True),
        sa.Column("execution_document_state", sa.String(24), nullable=False, server_default=""),
        sa.Column(
            "execution_document_payload",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint(
            "replenishment_method IN ('make', 'buy')",
            name="ck_replenishment_work_item_method",
        ),
        sa.CheckConstraint(
            "replenishment_required_qty >= 0",
            name="ck_replenishment_work_item_required_nonnegative",
        ),
        sa.CheckConstraint(
            "replenishment_fulfilled_qty >= 0",
            name="ck_replenishment_work_item_fulfilled_nonnegative",
        ),
        sa.CheckConstraint(
            "replenishment_remaining_qty >= 0",
            name="ck_replenishment_work_item_remaining_nonnegative",
        ),
        sa.CheckConstraint(
            "replenishment_required_qty >= replenishment_fulfilled_qty",
            name="ck_replenishment_work_item_fulfilled_le_required",
        ),
        sa.CheckConstraint(
            "replenishment_required_qty >= replenishment_remaining_qty",
            name="ck_replenishment_work_item_remaining_le_required",
        ),
        sa.CheckConstraint(
            "replenishment_remaining_qty = "
            "replenishment_required_qty - replenishment_fulfilled_qty",
            name="ck_replenishment_work_item_remaining_exact",
        ),
        sa.UniqueConstraint(
            "ledger_generation_id",
            "reservation_id",
            name="uq_replenishment_work_item_generation_reservation",
        ),
    )
    op.create_index(
        "ix_replenishment_work_item_generation",
        "replenishment_work_item",
        ["ledger_generation_id"],
    )
    op.create_index(
        "ix_replenishment_work_item_plan",
        "replenishment_work_item",
        ["plan_id"],
    )
    op.create_index(
        "ix_replenishment_work_item_run",
        "replenishment_work_item",
        ["run_id"],
    )

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


def downgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)
    if inspector is None or _has_table(inspector, "replenishment_work_item"):
        op.drop_table("replenishment_work_item")

    with op.batch_alter_table("ledger_build_batch") as batch_op:
        batch_op.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch_op.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', "
            "'execution_allocation', 'assembly_output_allocation', "
            "'snapshot_build', 'drum_schedule', 'shelf_projection')",
        )
