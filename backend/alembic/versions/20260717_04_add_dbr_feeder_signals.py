"""add advisory DBR feeder signals and safe chain default

Revision ID: 20260717_04
Revises: 20260717_03
"""

from alembic import op
import sqlalchemy as sa

from app.models import CrossPlatformJSON

revision = "20260717_04"
down_revision = "20260717_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)

    # A default-only change preserves every existing singleton value.
    if inspector is None or "dbr_settings" in inspector.get_table_names():
        op.alter_column(
            "dbr_settings",
            "feeder_chain_enabled",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.false(),
        )

    table_exists = bool(
        inspector is not None and "dbr_feeder_signal" in inspector.get_table_names()
    )
    if not table_exists:
        op.create_table(
            "dbr_feeder_signal",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("dedup_key", sa.String(66), nullable=False),
            sa.Column("signal_type", sa.String(30), server_default="Пополнение", nullable=False),
            sa.Column("supermarket_position_id", sa.Integer(), nullable=False),
            sa.Column("item_id", sa.Integer(), nullable=False),
            sa.Column("warehouse_ref1c", sa.String(36), nullable=False),
            sa.Column("status", sa.String(20), server_default="Open", nullable=False),
            sa.Column("suggested_qty", sa.Numeric(16, 3), server_default="0", nullable=False),
            sa.Column("priority", sa.Numeric(16, 6), server_default="0", nullable=False),
            sa.Column("zone", sa.String(20), nullable=True),
            sa.Column("nfp_snapshot", sa.Numeric(16, 3), nullable=True),
            sa.Column("target_qty_snapshot", sa.Numeric(16, 3), nullable=True),
            sa.Column("kit_force", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("kit_shortage_qty", sa.Numeric(16, 3), server_default="0", nullable=False),
            sa.Column("source_schedule_id", sa.Integer(), nullable=True),
            sa.Column("reason_json", CrossPlatformJSON(), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("refreshed_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("cancelled_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.CheckConstraint("signal_type = 'Пополнение'", name="ck_dbr_feeder_signal_type"),
            sa.CheckConstraint("status IN ('Open', 'Cancelled')", name="ck_dbr_feeder_signal_status"),
            sa.CheckConstraint("suggested_qty >= 0", name="ck_dbr_feeder_signal_qty_nonnegative"),
            sa.ForeignKeyConstraint(["item_id"], ["items.item_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_schedule_id"], ["dbr_drum_schedule.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["supermarket_position_id"], ["dbr_supermarket_position.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("dedup_key", name="ux_dbr_feeder_signal_dedup_key"),
        )
    else:
        columns = {row["name"] for row in inspector.get_columns("dbr_feeder_signal")}
        if "priority" not in columns:
            op.add_column(
                "dbr_feeder_signal",
                sa.Column("priority", sa.Numeric(16, 6), server_default="0", nullable=False),
            )

    inspector = None if offline else sa.inspect(bind)
    indexes = set() if inspector is None else {
        row["name"] for row in inspector.get_indexes("dbr_feeder_signal")
    }
    for column in (
        "id", "dedup_key", "supermarket_position_id", "item_id", "warehouse_ref1c",
        "status", "priority", "zone", "source_schedule_id",
    ):
        name = f"ix_dbr_feeder_signal_{column}"
        if name not in indexes:
            op.create_index(name, "dbr_feeder_signal", [column])


def downgrade() -> None:
    op.drop_table("dbr_feeder_signal")
    op.alter_column(
        "dbr_settings",
        "feeder_chain_enabled",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.true(),
    )
