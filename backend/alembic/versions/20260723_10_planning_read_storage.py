"""Add generation-scoped planning read storage.

Revision ID: 20260723_10
Revises: 20260723_09
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_10"
down_revision = "20260723_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "planning_run",
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "planning_run",
        sa.Column("ledger_cutoff", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_planning_run_ledger_generation",
        "planning_run",
        "ledger_generation",
        ["ledger_generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_planning_run_ledger_generation_id",
        "planning_run",
        ["ledger_generation_id"],
        unique=False,
    )

    with op.batch_alter_table("planning_read_snapshot") as batch:
        batch.drop_constraint(
            "uq_planning_read_snapshot_consumer_key",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_planning_read_snapshot_consumer_key_generation",
            ["consumer", "snapshot_key", "ledger_generation_id"],
        )

    op.create_table(
        "planning_read_row",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("row_key", sa.String(length=256), nullable=False),
        sa.Column("row_kind", sa.String(length=64), server_default="", nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=True),
        sa.Column("sort_key", sa.String(length=256), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["planning_read_snapshot.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.item_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id", "row_key",
            name="uq_planning_read_row_snapshot_key",
        ),
    )
    op.create_index(
        "ix_planning_read_row_snapshot_id",
        "planning_read_row",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index(
        "ix_planning_read_row_item_id",
        "planning_read_row",
        ["item_id"],
        unique=False,
    )
    op.create_index(
        "ix_planning_read_row_snapshot_kind",
        "planning_read_row",
        ["snapshot_id", "row_kind"],
        unique=False,
    )

    op.create_table(
        "planning_read_root_member",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("row_id", sa.BigInteger(), nullable=False),
        sa.Column("root_key", sa.String(length=256), nullable=False),
        sa.Column("root_item_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["planning_read_snapshot.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["row_id"], ["planning_read_row.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["root_item_id"], ["items.item_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id", "row_id", "root_key",
            name="uq_planning_read_root_member",
        ),
    )
    op.create_index(
        "ix_planning_read_root_member_snapshot_id",
        "planning_read_root_member",
        ["snapshot_id"],
        unique=False,
    )
    op.create_index(
        "ix_planning_read_root_member_row_id",
        "planning_read_root_member",
        ["row_id"],
        unique=False,
    )
    op.create_index(
        "ix_planning_read_root_member_root_item_id",
        "planning_read_root_member",
        ["root_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_planning_read_root_member_snapshot_root",
        "planning_read_root_member",
        ["snapshot_id", "root_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("planning_read_root_member")
    op.drop_table("planning_read_row")

    with op.batch_alter_table("planning_read_snapshot") as batch:
        batch.drop_constraint(
            "uq_planning_read_snapshot_consumer_key_generation",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_planning_read_snapshot_consumer_key",
            ["consumer", "snapshot_key"],
        )

    op.drop_index(
        "ix_planning_run_ledger_generation_id",
        table_name="planning_run",
    )
    op.drop_constraint(
        "fk_planning_run_ledger_generation",
        "planning_run",
        type_="foreignkey",
    )
    op.drop_column("planning_run", "ledger_cutoff")
    op.drop_column("planning_run", "ledger_generation_id")
