"""Persist explicit known-time and R5 correction audit provenance."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_06"
down_revision = "20260910_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stock_ledger_entry",
        sa.Column("known_at", sa.TIMESTAMP(), nullable=True),
    )
    op.execute(
        "UPDATE stock_ledger_entry SET known_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
        "WHERE known_at IS NULL"
    )
    op.alter_column("stock_ledger_entry", "known_at", nullable=False)
    op.add_column(
        "current_replenishment_audit",
        sa.Column("reason", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "current_replenishment_audit",
        sa.Column("basis_fact_ids", sa.JSON(), nullable=True),
    )
    op.execute(
        "UPDATE current_replenishment_audit SET reason = 'current_replay' "
        "WHERE reason IS NULL"
    )
    op.execute(
        "UPDATE current_replenishment_audit SET basis_fact_ids = '[]' "
        "WHERE basis_fact_ids IS NULL"
    )
    op.alter_column("current_replenishment_audit", "reason", nullable=False)
    op.alter_column("current_replenishment_audit", "basis_fact_ids", nullable=False)


def downgrade() -> None:
    op.drop_column("current_replenishment_audit", "basis_fact_ids")
    op.drop_column("current_replenishment_audit", "reason")
    op.drop_column("stock_ledger_entry", "known_at")

