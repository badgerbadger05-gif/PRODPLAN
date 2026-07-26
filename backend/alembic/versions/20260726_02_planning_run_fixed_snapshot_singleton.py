"""Enforce singleton FIXED_SNAPSHOT planning run per source plan.

Revision ID: 20260726_02
Revises: 20260726_01
"""

from alembic import op
import sqlalchemy as sa

revision = "20260726_02"
down_revision = "20260726_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            """
            SELECT source_plan_id, COUNT(*) AS cnt
            FROM planning_run
            WHERE status = 'FIXED_SNAPSHOT'
              AND source_plan_id IS NOT NULL
            GROUP BY source_plan_id
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()
    if duplicates:
        preview = ", ".join(f"{int(row[0])}:{int(row[1])}" for row in duplicates[:10])
        if len(duplicates) > 10:
            preview = f"{preview}, ... (+{len(duplicates) - 10} more)"
        raise RuntimeError(
            "cannot add FIXED_SNAPSHOT source_plan_id unique index because duplicates exist "
            f"for source_plan_id: {preview}"
        )

    inspector = sa.inspect(bind)
    indexes = {row["name"] for row in inspector.get_indexes("planning_run")}
    if "uq_planning_run_fixed_snapshot_source_plan" not in indexes:
        op.create_index(
            "uq_planning_run_fixed_snapshot_source_plan",
            "planning_run",
            ["source_plan_id"],
            unique=True,
            postgresql_where=sa.text(
                "status = 'FIXED_SNAPSHOT' AND source_plan_id IS NOT NULL"
            ),
            sqlite_where=sa.text(
                "status = 'FIXED_SNAPSHOT' AND source_plan_id IS NOT NULL"
            ),
        )


def downgrade() -> None:
    op.drop_index(
        "uq_planning_run_fixed_snapshot_source_plan",
        table_name="planning_run",
    )
