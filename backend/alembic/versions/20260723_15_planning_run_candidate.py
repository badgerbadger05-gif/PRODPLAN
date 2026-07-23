"""Add immutable-generation candidate identity for planning runs.

Revision ID: 20260723_15
Revises: 20260723_14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_15"
down_revision = "20260723_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``prior_run_id`` was introduced in 20260720_02.  Keep this defensive so
    # an installation that received the column outside Alembic still gets the
    # lookup index required by candidate creation.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {row["name"] for row in inspector.get_indexes("planning_run")}
    if "ix_planning_run_prior_run_id" not in indexes:
        op.create_index(
            "ix_planning_run_prior_run_id", "planning_run", ["prior_run_id"], unique=False
        )

    # Historical FIXED/SUPERSEDED rows deliberately retain lineage. Enforce
    # identity only for an open BUILDING_SNAPSHOT candidate with both keys;
    # otherwise a legitimate historical record would block a refresh.
    op.create_index(
        "uq_planning_run_generation_source_plan",
        "planning_run",
        ["ledger_generation_id", "source_plan_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'BUILDING_SNAPSHOT' AND ledger_generation_id IS NOT NULL "
            "AND source_plan_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "status = 'BUILDING_SNAPSHOT' AND ledger_generation_id IS NOT NULL "
            "AND source_plan_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_planning_run_generation_source_plan", table_name="planning_run"
    )
