"""R4 current replenishment state, audit, and stable assignment marker."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_02"
down_revision = "20260910_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reservation_consumption_allocation",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "uq_res_consumption_current_sle_reservation",
        "reservation_consumption_allocation",
        ["sle_id", "reservation_id"],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
        sqlite_where=sa.text("is_current = 1"),
    )
    op.create_table(
        "current_replenishment_state",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("scope_key", sa.String(length=160), nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("writer_key", sa.String(length=64), nullable=False, server_default="current_replenishment"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="completed"),
        sa.Column("changed_pairs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["ledger_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope_key", name="uq_current_replenishment_state_scope"),
        sa.CheckConstraint("writer_key = 'current_replenishment'", name="ck_current_replenishment_state_writer"),
        sa.CheckConstraint("status IN ('applying', 'completed')", name="ck_current_replenishment_state_status"),
    )
    op.create_index("ix_current_replenishment_state_generation", "current_replenishment_state", ["ledger_generation_id"])
    op.create_table(
        "current_replenishment_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("state_id", sa.BigInteger(), nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("scope_key", sa.String(length=160), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("sle_id", sa.BigInteger(), nullable=False),
        sa.Column("reservation_id", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("before_qty", sa.Numeric(15, 3), nullable=True),
        sa.Column("after_qty", sa.Numeric(15, 3), nullable=True),
        sa.Column("before_match_rule", sa.String(length=16), nullable=True),
        sa.Column("after_match_rule", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["state_id"], ["current_replenishment_state.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ledger_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sle_id"], ["stock_ledger_entry.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservation_entry.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_key", "source_revision", "sle_id", "reservation_id", "operation",
            name="uq_current_replenishment_audit_change",
        ),
    )
    op.create_index("ix_current_replenishment_audit_state", "current_replenishment_audit", ["state_id"])
    op.create_index("ix_current_replenishment_audit_generation", "current_replenishment_audit", ["ledger_generation_id"])
    op.create_index("ix_current_replenishment_audit_reservation", "current_replenishment_audit", ["reservation_id"])


def downgrade() -> None:
    op.drop_index("ix_current_replenishment_audit_reservation", table_name="current_replenishment_audit")
    op.drop_index("ix_current_replenishment_audit_generation", table_name="current_replenishment_audit")
    op.drop_index("ix_current_replenishment_audit_state", table_name="current_replenishment_audit")
    op.drop_table("current_replenishment_audit")
    op.drop_index("ix_current_replenishment_state_generation", table_name="current_replenishment_state")
    op.drop_table("current_replenishment_state")
    op.drop_index("uq_res_consumption_current_sle_reservation", table_name="reservation_consumption_allocation")
    op.drop_column("reservation_consumption_allocation", "is_current")
