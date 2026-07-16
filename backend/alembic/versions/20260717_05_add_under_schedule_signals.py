"""add under-schedule advisory feeder signals

Revision ID: 20260717_05
Revises: 20260717_04
"""

from alembic import op
import sqlalchemy as sa

from app.models import CrossPlatformJSON

revision = "20260717_05"
down_revision = "20260717_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)
    columns = set() if inspector is None else {
        row["name"] for row in inspector.get_columns("dbr_feeder_signal")
    }
    additions = (
        ("drum_slot_id", sa.Column("drum_slot_id", sa.Integer(), nullable=True)),
        ("need_date", sa.Column("need_date", sa.Date(), nullable=True)),
        ("required_date", sa.Column("required_date", sa.Date(), nullable=True)),
        ("raw_demand_qty", sa.Column("raw_demand_qty", sa.Numeric(16, 3), nullable=True)),
        ("raw_shortage_qty", sa.Column("raw_shortage_qty", sa.Numeric(16, 3), nullable=True)),
        ("data_quality", sa.Column("data_quality", CrossPlatformJSON(), server_default=sa.text("'[]'"), nullable=False)),
        ("is_incomplete", sa.Column("is_incomplete", sa.Boolean(), server_default=sa.false(), nullable=False)),
    )
    for name, column in additions:
        if inspector is None or name not in columns:
            op.add_column("dbr_feeder_signal", column)
    checks = {} if inspector is None else {
        row.get("name"): row.get("sqltext", "")
        for row in inspector.get_check_constraints("dbr_feeder_signal")
    }
    type_sql = str(checks.get("ck_dbr_feeder_signal_type", ""))
    if inspector is None or "Под график" not in type_sql:
        op.drop_constraint("ck_dbr_feeder_signal_type", "dbr_feeder_signal", type_="check")
        op.create_check_constraint(
            "ck_dbr_feeder_signal_type", "dbr_feeder_signal",
            "signal_type IN ('Пополнение', 'Под график')",
        )
    foreign_columns = set() if inspector is None else {
        tuple(row.get("constrained_columns") or ())
        for row in inspector.get_foreign_keys("dbr_feeder_signal")
    }
    if inspector is None or ("drum_slot_id",) not in foreign_columns:
        op.create_foreign_key(
            "fk_dbr_feeder_signal_drum_slot", "dbr_feeder_signal", "dbr_drum_slot",
            ["drum_slot_id"], ["id"], ondelete="CASCADE",
        )
    indexes = set() if inspector is None else {
        row["name"] for row in inspector.get_indexes("dbr_feeder_signal")
    }
    for column in ("drum_slot_id", "need_date", "required_date", "is_incomplete"):
        name = f"ix_dbr_feeder_signal_{column}"
        if inspector is None or name not in indexes:
            op.create_index(name, "dbr_feeder_signal", [column])


def downgrade() -> None:
    for column in ("is_incomplete", "required_date", "need_date", "drum_slot_id"):
        op.drop_index(f"ix_dbr_feeder_signal_{column}", table_name="dbr_feeder_signal")
    op.drop_constraint("fk_dbr_feeder_signal_drum_slot", "dbr_feeder_signal", type_="foreignkey")
    op.drop_constraint("ck_dbr_feeder_signal_type", "dbr_feeder_signal", type_="check")
    op.create_check_constraint("ck_dbr_feeder_signal_type", "dbr_feeder_signal", "signal_type = 'Пополнение'")
    for column in ("is_incomplete", "data_quality", "raw_shortage_qty", "raw_demand_qty", "required_date", "need_date", "drum_slot_id"):
        op.drop_column("dbr_feeder_signal", column)
