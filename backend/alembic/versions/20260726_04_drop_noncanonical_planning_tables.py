"""Drop retired planning-comparison and ledger-v2 projection tables.

Revision ID: 20260726_04
Revises: 20260726_03

These tables belonged to alternative planning/read-model contours that are not
part of the canonical planning truth. Their data is deliberately not migrated:
none of it owns accepted plan obligations or physical ledger facts.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_04"
down_revision = "20260726_03"
branch_labels = None
depends_on = None


TABLES_IN_DROP_ORDER = (
    "planning_comparison_diff",
    "planning_comparison_row",
    "planning_comparison_snapshot",
    "planning_comparison_event",
    "planning_comparison_batch",
    "mrp_execution_allocation",
    "mrp_requirement_carry",
    "mrp_drift_event",
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table_name in TABLES_IN_DROP_ORDER:
        if table_name in existing:
            op.drop_table(table_name)


def downgrade() -> None:
    raise RuntimeError(
        "20260726_04 is an intentional destructive canon cleanup and cannot be "
        "downgraded; restore a pre-migration database backup if rollback is required"
    )
