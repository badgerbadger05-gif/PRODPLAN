"""Cut purchase export batches over to the stable current execution anchor."""

from alembic import op
import sqlalchemy as sa


revision = "20260911_02"
down_revision = "20260911_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {
        str(row[0])
        for row in connection.execute(sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='purchase_export_batch'"
        ))
    }
    if "planning_read_snapshot_id" not in columns:
        # A repeated deployment is safe only when the current-only contract is
        # already complete.  Do not silently recreate or infer the old anchor.
        if {"current_execution_scope_id", "current_execution_source_revision"} - columns:
            raise RuntimeError("purchase export anchor schema is incomplete")
        missing = connection.execute(sa.text(
            "SELECT COUNT(*) FROM purchase_export_batch "
            "WHERE current_execution_scope_id IS NULL OR current_execution_source_revision IS NULL"
        )).scalar_one()
        if int(missing or 0):
            raise RuntimeError("current-only purchase export schema has incomplete anchors")
        return

    legacy = connection.execute(sa.text(
        "SELECT COUNT(*) FROM purchase_export_batch WHERE planning_read_snapshot_id IS NOT NULL"
    )).scalar_one()
    missing_current = connection.execute(sa.text(
        "SELECT COUNT(*) FROM purchase_export_batch "
        "WHERE current_execution_scope_id IS NULL OR current_execution_source_revision IS NULL"
    )).scalar_one()
    if int(legacy or 0):
        raise RuntimeError(
            f"cannot cut over purchase export anchors: {int(legacy)} legacy snapshot anchors remain"
        )
    if int(missing_current or 0):
        raise RuntimeError(
            f"cannot cut over purchase export anchors: {int(missing_current)} current anchors are incomplete"
        )

    op.drop_constraint(
        "ck_purchase_export_batch_exactly_one_source_anchor",
        "purchase_export_batch",
        type_="check",
    )
    op.drop_index(
        "ix_purchase_export_batch_planning_read_snapshot_id",
        table_name="purchase_export_batch",
    )
    op.drop_constraint(
        "fk_purchase_export_batch_planning_read_snapshot",
        "purchase_export_batch",
        type_="foreignkey",
    )
    op.drop_column("purchase_export_batch", "planning_read_snapshot_id")
    op.alter_column(
        "purchase_export_batch",
        "current_execution_scope_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "purchase_export_batch",
        "current_execution_source_revision",
        existing_type=sa.String(length=256),
        nullable=False,
    )


def downgrade() -> None:
    raise RuntimeError(
        "20260911_02 purchase anchor cutover is irreversible without an explicit backup/mapping restore"
    )
