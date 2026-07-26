"""Add immutable closed plan history snapshot.

Revision ID: 20260726_11
Revises: 20260726_10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_11"
down_revision = "20260726_10"
branch_labels = None
depends_on = None


def _bigint():
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _has_table(inspector: sa.Inspector, table: str) -> bool:
    return table in set(inspector.get_table_names())


def upgrade() -> None:
    op.create_table(
        "closed_plan_snapshot",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("production_plan_header.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("run_id", _bigint(), sa.ForeignKey("planning_run.run_id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("ledger_generation_id", _bigint(), sa.ForeignKey("ledger_generation.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("plan_id", "run_id", name="uq_closed_plan_snapshot_plan_run"),
    )
    op.create_index(
        "ix_closed_plan_snapshot_plan",
        "closed_plan_snapshot",
        ["plan_id"],
    )
    op.create_index(
        "ix_closed_plan_snapshot_run",
        "closed_plan_snapshot",
        ["run_id"],
    )
    op.create_index(
        "ix_closed_plan_snapshot_generation",
        "closed_plan_snapshot",
        ["ledger_generation_id"],
    )
    op.create_index(
        "ix_closed_plan_snapshot_closed_at",
        "closed_plan_snapshot",
        ["closed_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)
    if inspector is None or _has_table(inspector, "closed_plan_snapshot"):
        op.drop_table("closed_plan_snapshot")
