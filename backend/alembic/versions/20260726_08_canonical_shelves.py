"""Add canonical physical shelf policy and generation projection.

Revision ID: 20260726_08
Revises: 20260726_07
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_08"
down_revision = "20260726_07"
branch_labels = None
depends_on = None


def _bigint():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "shelf_policy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("warehouse_ref1c", sa.String(36), nullable=False),
        sa.Column("replenishment_time_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review_cycle_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("safety_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("batch_multiple", sa.DECIMAL(15, 3), nullable=False, server_default="1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("replenishment_time_days >= 0", name="ck_shelf_policy_replenishment_nonnegative"),
        sa.CheckConstraint("review_cycle_days >= 0", name="ck_shelf_policy_review_nonnegative"),
        sa.CheckConstraint("safety_days >= 0", name="ck_shelf_policy_safety_nonnegative"),
        sa.CheckConstraint("batch_multiple > 0", name="ck_shelf_policy_batch_positive"),
        sa.UniqueConstraint("item_id", "warehouse_ref1c", name="uq_shelf_policy_item_warehouse"),
    )
    op.create_index("ix_shelf_policy_active_item", "shelf_policy", ["active", "item_id"])
    op.create_index("ix_shelf_policy_item_id", "shelf_policy", ["item_id"])
    op.create_index("ix_shelf_policy_warehouse_ref1c", "shelf_policy", ["warehouse_ref1c"])

    op.create_table(
        "shelf_projection",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("ledger_generation_id", _bigint(), sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("shelf_policy_id", sa.Integer(), sa.ForeignKey("shelf_policy.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("warehouse_ref1c", sa.String(36), nullable=False),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("protection_until", sa.Date(), nullable=False),
        sa.Column("target_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("shelf_physical_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("other_stock_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("confirmed_open_production_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("projected_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("gap_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("transfer_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("unlaunched_mrp_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("pull_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("materialized_qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("first_shortage_date", sa.Date(), nullable=True),
        sa.Column("latest_start_date", sa.Date(), nullable=True),
        sa.Column("demand_manifest", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.CheckConstraint("target_qty >= 0", name="ck_shelf_projection_target_nonnegative"),
        sa.CheckConstraint("gap_qty >= 0", name="ck_shelf_projection_gap_nonnegative"),
        sa.CheckConstraint("transfer_qty >= 0", name="ck_shelf_projection_transfer_nonnegative"),
        sa.CheckConstraint("pull_qty >= 0", name="ck_shelf_projection_pull_nonnegative"),
        sa.CheckConstraint("materialized_qty >= 0", name="ck_shelf_projection_materialized_nonnegative"),
        sa.CheckConstraint("materialized_qty <= unlaunched_mrp_qty", name="ck_shelf_projection_materialized_within_mrp"),
        sa.UniqueConstraint("ledger_generation_id", "shelf_policy_id", name="uq_shelf_projection_generation_policy"),
    )
    op.create_index("ix_shelf_projection_ledger_generation_id", "shelf_projection", ["ledger_generation_id"])
    op.create_index("ix_shelf_projection_shelf_policy_id", "shelf_projection", ["shelf_policy_id"])
    op.create_index("ix_shelf_projection_item", "shelf_projection", ["item_id"])
    op.create_index(
        "ix_shelf_projection_generation_priority",
        "shelf_projection",
        ["ledger_generation_id", "latest_start_date"],
    )

    with op.batch_alter_table("ledger_build_batch") as batch:
        batch.drop_constraint("ck_ledger_build_batch_stage", type_="check")
        batch.create_check_constraint(
            "ck_ledger_build_batch_stage",
            "stage IN ('physical_import', 'reservation_replay', "
            "'reservation_materialize', 'execution_allocation', "
            "'assembly_output_allocation', 'snapshot_build', "
            "'drum_schedule', 'shelf_projection')",
        )


def downgrade() -> None:
    raise RuntimeError("20260726_08 is a forward-only canonical migration")
