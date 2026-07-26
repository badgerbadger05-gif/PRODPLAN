"""Drop mutable execution caches from frozen MRP requirements.

ReservationEntry and ReplenishmentWorkItem are the sole quantity owners.

Revision ID: 20260726_14
Revises: 20260726_13
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_14"
down_revision = "20260726_13"
branch_labels = None
depends_on = None

TABLE_NAME = "mrp_requirement"
CACHE_COLUMNS = (
    "covered_qty",
    "remaining_qty",
    "executed_qty",
    "carried_remaining",
    "initial_snapshot_stock",
    "drift_adjustment_qty",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    for name in CACHE_COLUMNS:
        if name in existing:
            op.drop_column(TABLE_NAME, name)


def downgrade() -> None:
    raise RuntimeError(
        "20260726_14 is destructive and cannot be downgraded; restore a database backup"
    )
