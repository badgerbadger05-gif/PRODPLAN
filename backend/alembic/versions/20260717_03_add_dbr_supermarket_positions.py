"""add DBR Phase-2 static supermarket positions

Revision ID: 20260717_03
Revises: 20260717_02
"""

from alembic import op
import sqlalchemy as sa
from app.models import CrossPlatformJSON

revision = "20260717_03"
down_revision = "20260717_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)
    table_exists = bool(
        inspector is not None
        and "dbr_supermarket_position" in inspector.get_table_names()
    )
    if not table_exists:
        op.create_table(
        "dbr_supermarket_position",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("warehouse_ref1c", sa.String(36), nullable=False),
        sa.Column("supply_type", sa.String(20), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_stale", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("adu", sa.Numeric(16, 4), nullable=False),
        sa.Column("commonality", sa.Integer(), nullable=False),
        sa.Column("route_class", sa.String(40), nullable=True),
        sa.Column("rt_days", sa.Numeric(10, 3), nullable=False),
        sa.Column("rt_source", sa.String(20), server_default="class", nullable=False),
        sa.Column("batch_days", sa.Numeric(10, 3), nullable=False),
        sa.Column("q_batch", sa.Numeric(16, 3), nullable=False),
        sa.Column("k_var", sa.Numeric(6, 3), nullable=False),
        sa.Column("supply_risk_pct", sa.Numeric(8, 3), server_default="0", nullable=False),
        sa.Column("red_qty", sa.Numeric(16, 3), nullable=False),
        sa.Column("yellow_qty", sa.Numeric(16, 3), nullable=False),
        sa.Column("green_qty", sa.Numeric(16, 3), nullable=False),
        sa.Column("target_qty", sa.Numeric(16, 3), nullable=False),
        sa.Column("source_schedule_id", sa.Integer(), nullable=True),
        sa.Column("data_quality", CrossPlatformJSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("calculation_snapshot", CrossPlatformJSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("calculated_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("adu >= 0", name="ck_dbr_supermarket_position_adu_nonnegative"),
        sa.CheckConstraint("commonality >= 0", name="ck_dbr_supermarket_position_commonality_nonnegative"),
        sa.CheckConstraint("rt_days >= 0", name="ck_dbr_supermarket_position_rt_nonnegative"),
        sa.CheckConstraint("batch_days >= 0", name="ck_dbr_supermarket_position_batch_nonnegative"),
        sa.CheckConstraint("q_batch >= 0", name="ck_dbr_supermarket_position_q_batch_nonnegative"),
        sa.CheckConstraint("k_var >= 0", name="ck_dbr_supermarket_position_k_var_nonnegative"),
        sa.CheckConstraint("k_var <= 1", name="ck_dbr_supermarket_position_k_var_bounded"),
        sa.CheckConstraint("supply_risk_pct >= 0", name="ck_dbr_supermarket_position_supply_risk_nonnegative"),
        sa.CheckConstraint("supply_type IN ('manufacture', 'purchase')", name="ck_dbr_supermarket_position_supply_type_allowed"),
        sa.CheckConstraint("mode IN ('shelf', 'under_schedule')", name="ck_dbr_supermarket_position_mode_allowed"),
        sa.CheckConstraint("rt_source IN ('class', 'lead_time')", name="ck_dbr_supermarket_position_rt_source_allowed"),
        sa.CheckConstraint("red_qty >= 0 AND yellow_qty >= 0 AND green_qty >= 0 AND target_qty >= 0", name="ck_dbr_supermarket_position_zones_nonnegative"),
        sa.ForeignKeyConstraint(["item_id"], ["items.item_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_schedule_id"], ["dbr_drum_schedule.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("item_id", "warehouse_ref1c", name="ux_dbr_supermarket_position_item_warehouse"),
        )
    else:
        columns = {row["name"] for row in inspector.get_columns("dbr_supermarket_position")}
        additions = {
            "supply_risk_pct": sa.Column("supply_risk_pct", sa.Numeric(8, 3), server_default="0", nullable=False),
            "calculation_snapshot": sa.Column("calculation_snapshot", CrossPlatformJSON(), server_default=sa.text("'{}'"), nullable=False),
            "calculated_at": sa.Column("calculated_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        }
        for name, column in additions.items():
            if name not in columns:
                op.add_column("dbr_supermarket_position", column)

    inspector = None if offline else sa.inspect(bind)
    existing_indexes = (
        set()
        if inspector is None
        else {
            row["name"]
            for row in inspector.get_indexes("dbr_supermarket_position")
        }
    )
    for column in ("id", "item_id", "warehouse_ref1c", "is_active", "is_stale", "source_schedule_id"):
        name = f"ix_dbr_supermarket_position_{column}"
        if name not in existing_indexes:
            op.create_index(name, "dbr_supermarket_position", [column])


def downgrade() -> None:
    op.drop_table("dbr_supermarket_position")
