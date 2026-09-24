"""Per-run rebase failure state for the specification-rebase worker.

A run whose rebase keeps failing must not block the queue, and the counter
must belong to the run - a queue request can be shared by several runs, and a
run in scope may have no request at all.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260924_01"
down_revision = "20260914_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "specification_rebase_run_state",
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("planning_run.run_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("specification_rebase_run_state")
