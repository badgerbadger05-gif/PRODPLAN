"""materialize DBR decisions into 1С (slot/signal order refs + signal lifecycle)

Revision ID: 20260717_08
Revises: 20260717_07

Adds the columns that stamp the 1С Document_ЗаказНаПроизводство created when a
drum slot is released or a feeder signal is launched, and widens the feeder
signal status check to carry the materialized lifecycle
(Order Created / In Work / Done) fed back from the production-order sync.

Guard pattern (mirrors 20260717_06/07): every step is inspected first so a
re-run or a schema already built by create_all() is a no-op. SQLite has no
ALTER DROP CONSTRAINT, so the check swap goes through batch_alter_table.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260717_08"
down_revision = "20260717_07"
branch_labels = None
depends_on = None

_STATUS_WITH_LIFECYCLE = (
    "status IN ('Open', 'Diagnostic', 'Order Created', 'In Work', 'Done', 'Cancelled')"
)
_STATUS_LEGACY = "status IN ('Open', 'Diagnostic', 'Cancelled')"


def _swap_status_check(dialect: str, definition: str) -> None:
    if dialect == "postgresql":
        op.drop_constraint("ck_dbr_feeder_signal_status", "dbr_feeder_signal", type_="check")
        op.create_check_constraint(
            "ck_dbr_feeder_signal_status", "dbr_feeder_signal", definition
        )
    else:
        with op.batch_alter_table("dbr_feeder_signal", schema=None) as batch_op:
            batch_op.drop_constraint("ck_dbr_feeder_signal_status", type_="check")
            batch_op.create_check_constraint("ck_dbr_feeder_signal_status", definition)


def _add_order_columns(table: str, inspector) -> None:
    columns = set() if inspector is None else {
        row["name"] for row in inspector.get_columns(table)
    }
    if inspector is None or "one_c_order_ref" not in columns:
        op.add_column(table, sa.Column("one_c_order_ref", sa.String(length=36), nullable=True))
        op.create_index(f"ix_{table}_one_c_order_ref", table, ["one_c_order_ref"])
    if inspector is None or "one_c_order_number" not in columns:
        op.add_column(table, sa.Column("one_c_order_number", sa.String(length=50), nullable=True))


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    dialect = bind.dialect.name
    inspector = None if offline else sa.inspect(bind)

    _add_order_columns("dbr_drum_slot", inspector)
    _add_order_columns("dbr_feeder_signal", inspector)

    checks = {} if inspector is None else {
        row.get("name"): str(row.get("sqltext", ""))
        for row in inspector.get_check_constraints("dbr_feeder_signal")
    }
    status_sql = checks.get("ck_dbr_feeder_signal_status", "")
    if inspector is None or "Order Created" not in status_sql:
        _swap_status_check(dialect, _STATUS_WITH_LIFECYCLE)


def downgrade() -> None:
    # Collapse the materialized lifecycle back into legacy statuses before the
    # narrower check is restored, otherwise the constraint would be violated.
    op.execute(
        "UPDATE dbr_feeder_signal SET status = 'Cancelled' "
        "WHERE status IN ('Order Created', 'In Work', 'Done')"
    )
    _swap_status_check(op.get_bind().dialect.name, _STATUS_LEGACY)
    op.drop_index("ix_dbr_feeder_signal_one_c_order_ref", table_name="dbr_feeder_signal")
    op.drop_column("dbr_feeder_signal", "one_c_order_number")
    op.drop_column("dbr_feeder_signal", "one_c_order_ref")
    op.drop_index("ix_dbr_drum_slot_one_c_order_ref", table_name="dbr_drum_slot")
    op.drop_column("dbr_drum_slot", "one_c_order_number")
    op.drop_column("dbr_drum_slot", "one_c_order_ref")
