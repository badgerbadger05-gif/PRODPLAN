"""add DBR schedule activation and extension concurrency guards

Revision ID: 20260717_01
Revises: 20260716_02
Create Date: 2026-07-17 16:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260717_01"
down_revision = "20260716_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # Repair any pre-existing invariant violation deterministically before the
    # partial unique index is installed: keep the newest active schedule.
    bind.execute(
        sa.text(
            """
            UPDATE dbr_drum_schedule
               SET status = 'superseded'
             WHERE status = 'active'
               AND id <> (
                   SELECT MAX(id) FROM dbr_drum_schedule WHERE status = 'active'
               )
            """
        )
    )
    op.create_index(
        "ux_dbr_drum_schedule_one_active",
        "dbr_drum_schedule",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "dbr_drum_schedule_program",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("program_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["dbr_drum_schedule.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["program_id"], ["dbr_production_program.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "program_id", name="ux_dbr_drum_schedule_program"
        ),
    )
    op.create_index(
        "ix_dbr_drum_schedule_program_id",
        "dbr_drum_schedule_program",
        ["id"],
    )
    op.create_index(
        "ix_dbr_drum_schedule_program_schedule_id",
        "dbr_drum_schedule_program",
        ["schedule_id"],
    )
    op.create_index(
        "ix_dbr_drum_schedule_program_program_id",
        "dbr_drum_schedule_program",
        ["program_id"],
    )

    # Backfill build/extend provenance already represented by slots. This also
    # makes repeat extend calls idempotent immediately after deployment.
    bind.execute(
        sa.text(
            """
            INSERT INTO dbr_drum_schedule_program (schedule_id, program_id)
            SELECT schedule_id, source_program_id
              FROM dbr_drum_slot
             WHERE source_program_id IS NOT NULL
             GROUP BY schedule_id, source_program_id
            """
        )
    )


def downgrade() -> None:
    op.drop_table("dbr_drum_schedule_program")
    op.drop_index(
        "ux_dbr_drum_schedule_one_active", table_name="dbr_drum_schedule"
    )
