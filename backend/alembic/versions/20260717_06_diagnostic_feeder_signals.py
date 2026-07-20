"""make incomplete feeder signals diagnostic-only

Revision ID: 20260717_06
Revises: 20260717_05
"""

from alembic import op
import sqlalchemy as sa

revision = "20260717_06"
down_revision = "20260717_05"
branch_labels = None
depends_on = None


def _swap_status_check(dialect: str, definition: str) -> None:
    """Recreate ck_dbr_feeder_signal_status. SQLite has no ALTER DROP CONSTRAINT,
    so it goes through batch_alter_table (table copy); Postgres keeps DDL."""
    if dialect == "postgresql":
        op.drop_constraint("ck_dbr_feeder_signal_status", "dbr_feeder_signal", type_="check")
        op.create_check_constraint(
            "ck_dbr_feeder_signal_status", "dbr_feeder_signal", definition
        )
    else:
        with op.batch_alter_table("dbr_feeder_signal", schema=None) as batch_op:
            batch_op.drop_constraint("ck_dbr_feeder_signal_status", type_="check")
            batch_op.create_check_constraint("ck_dbr_feeder_signal_status", definition)


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    dialect = bind.dialect.name
    inspector = None if offline else sa.inspect(bind)
    columns = set() if inspector is None else {
        row["name"] for row in inspector.get_columns("dbr_feeder_signal")
    }
    if inspector is None or "calculated_batch_qty" not in columns:
        op.add_column(
            "dbr_feeder_signal",
            sa.Column("calculated_batch_qty", sa.Numeric(16, 3), nullable=True),
        )
    checks = {} if inspector is None else {
        row.get("name"): row.get("sqltext", "")
        for row in inspector.get_check_constraints("dbr_feeder_signal")
    }
    status_sql = str(checks.get("ck_dbr_feeder_signal_status", ""))
    if inspector is None or "Diagnostic" not in status_sql:
        _swap_status_check(dialect, "status IN ('Open', 'Diagnostic', 'Cancelled')")
    # Eliminate the unsafe deployment window: rows materialized by revision 05
    # must not remain actionable until the first post-deploy refresh.
    op.execute(
        "UPDATE dbr_feeder_signal "
        "SET calculated_batch_qty = COALESCE(calculated_batch_qty, suggested_qty), "
        "suggested_qty = 0, status = 'Diagnostic' "
        "WHERE signal_type = 'Под график' "
        "AND is_incomplete = true AND status = 'Open'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE dbr_feeder_signal SET status = 'Cancelled', suggested_qty = 0 "
        "WHERE status = 'Diagnostic'"
    )
    _swap_status_check(op.get_bind().dialect.name, "status IN ('Open', 'Cancelled')")
    op.drop_column("dbr_feeder_signal", "calculated_batch_qty")
