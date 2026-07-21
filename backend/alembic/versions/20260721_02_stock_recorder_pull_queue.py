"""stock_recorder_pull queue columns (Increment 2, additive)

Revision ID: 20260721_02
Revises: 20260721_01

Adds the queue/retry bookkeeping columns the pull-by-document ingest needs on
``stock_recorder_pull`` (design §2.3 / §3а): ``source`` (who enqueued),
``attempts`` (retry cap for process_pending_pulls), ``last_error`` (diagnostic),
and ``updated_at``. Inspector-guarded so a re-run of ``upgrade head`` is a no-op.
No behavior change beyond the two guarded enqueue lines in the export services.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_02"
down_revision = "20260721_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)

    columns = (
        set()
        if inspector is None
        else {row["name"] for row in inspector.get_columns("stock_recorder_pull")}
    )

    if inspector is None or "source" not in columns:
        op.add_column(
            "stock_recorder_pull",
            sa.Column("source", sa.String(length=64), nullable=False, server_default=""),
        )
    if inspector is None or "attempts" not in columns:
        op.add_column(
            "stock_recorder_pull",
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        )
    if inspector is None or "last_error" not in columns:
        op.add_column(
            "stock_recorder_pull",
            sa.Column("last_error", sa.Text(), nullable=True),
        )
    if inspector is None or "updated_at" not in columns:
        op.add_column(
            "stock_recorder_pull",
            sa.Column(
                "updated_at",
                sa.TIMESTAMP(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    op.drop_column("stock_recorder_pull", "updated_at")
    op.drop_column("stock_recorder_pull", "last_error")
    op.drop_column("stock_recorder_pull", "attempts")
    op.drop_column("stock_recorder_pull", "source")
