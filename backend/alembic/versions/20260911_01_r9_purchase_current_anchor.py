"""Anchor current purchase exports to the R9 current execution scope."""

from alembic import op
import sqlalchemy as sa


revision = "20260911_01"
down_revision = "20260910_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "purchase_export_batch",
        sa.Column("current_execution_scope_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "purchase_export_batch",
        sa.Column("current_execution_source_revision", sa.String(length=256), nullable=True),
    )
    op.alter_column(
        "purchase_export_batch",
        "planning_read_snapshot_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_purchase_export_batch_current_execution_scope",
        "purchase_export_batch",
        "current_execution_scope",
        ["current_execution_scope_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_purchase_export_batch_exactly_one_source_anchor",
        "purchase_export_batch",
        "(planning_read_snapshot_id IS NOT NULL AND current_execution_scope_id IS NULL) "
        "OR (planning_read_snapshot_id IS NULL AND current_execution_scope_id IS NOT NULL)",
    )
    op.create_index(
        "ix_purchase_export_batch_current_execution_scope_id",
        "purchase_export_batch",
        ["current_execution_scope_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    current_only = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM purchase_export_batch "
            "WHERE planning_read_snapshot_id IS NULL"
        )
    ).scalar_one()
    if int(current_only or 0):
        raise RuntimeError(
            "cannot downgrade R9 purchase anchor: current-only batches lack a legacy snapshot anchor"
        )
    op.drop_constraint(
        "ck_purchase_export_batch_exactly_one_source_anchor",
        "purchase_export_batch",
        type_="check",
    )
    op.drop_index(
        "ix_purchase_export_batch_current_execution_scope_id",
        table_name="purchase_export_batch",
    )
    op.drop_constraint(
        "fk_purchase_export_batch_current_execution_scope",
        "purchase_export_batch",
        type_="foreignkey",
    )
    # Keep the legacy FK nullable during downgrade: current batches have no
    # valid PlanningReadSnapshot identity and must not be relinked by guesswork.
    op.drop_column("purchase_export_batch", "current_execution_source_revision")
    op.drop_column("purchase_export_batch", "current_execution_scope_id")
    op.alter_column(
        "purchase_export_batch",
        "planning_read_snapshot_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
