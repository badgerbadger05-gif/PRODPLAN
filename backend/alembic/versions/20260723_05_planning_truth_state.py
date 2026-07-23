"""Add versioned Item Ledger planning-truth state.

Revision ID: 20260723_05
Revises: 20260723_04
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_05"
down_revision = "20260723_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ledger_generation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("generation_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="building", nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_watermarks", sa.JSON(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=128), nullable=False),
        sa.Column("replay_version", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('uninitialized', 'building', 'accepted', 'stale', 'rejected')",
            name="ck_ledger_generation_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_key", name="uq_ledger_generation_key"),
    )
    op.create_table(
        "planning_truth_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("current_generation_id", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_planning_truth_state_singleton"),
        sa.ForeignKeyConstraint(
            ["current_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("current_generation_id"),
    )
    op.create_table(
        "planning_read_snapshot",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("consumer", sa.String(length=128), nullable=False),
        sa.Column("snapshot_key", sa.String(length=256), nullable=False),
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=False),
        sa.Column("cutoff", sa.DateTime(timezone=True), nullable=False),
        sa.Column("truth_status", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ledger_generation_id"], ["ledger_generation.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "consumer", "snapshot_key",
            name="uq_planning_read_snapshot_consumer_key",
        ),
    )
    op.create_index(
        "ix_planning_read_snapshot_latest",
        "planning_read_snapshot",
        ["consumer", "ledger_generation_id", "published_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_planning_read_snapshot_latest",
        table_name="planning_read_snapshot",
    )
    op.drop_table("planning_read_snapshot")
    op.drop_table("planning_truth_state")
    op.drop_table("ledger_generation")
