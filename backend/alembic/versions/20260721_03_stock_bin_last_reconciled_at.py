"""stock_bin.last_reconciled_at (Increment 3, additive)

Revision ID: 20260721_03
Revises: 20260721_02

Adds the Balance-reconcile debounce marker the inc3 after-step needs on
``stock_bin``: ``last_reconciled_at`` (when the ledger key was last confirmed
against the 1С ``/Balance`` snapshot — either matched within EPS or folded by an
applied adjustment-SLE). The other debounce field, ``reconcile_pending_qty``,
already exists from inc1. Inspector-guarded so a re-run of ``upgrade head`` is a
no-op. Shadow-only: no reader consults the column; the sole production-path
change is the guarded reconcile after-step at the tail of the stock sweep.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260721_03"
down_revision = "20260721_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)

    columns = (
        set()
        if inspector is None
        else {row["name"] for row in inspector.get_columns("stock_bin")}
    )

    if inspector is None or "last_reconciled_at" not in columns:
        op.add_column(
            "stock_bin",
            sa.Column("last_reconciled_at", sa.TIMESTAMP(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("stock_bin", "last_reconciled_at")
