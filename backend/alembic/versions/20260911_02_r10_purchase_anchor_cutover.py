"""Cut purchase export batches over to the stable current execution anchor."""

from alembic import op
import sqlalchemy as sa


revision = "20260911_02"
down_revision = "20260911_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    table_names = set(inspector.get_table_names())
    if "purchase_export_batch" not in table_names:
        raise RuntimeError("purchase export anchor table is missing")
    columns = {
        str(column["name"])
        for column in inspector.get_columns("purchase_export_batch")
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

    # Alembic's historical FK name is not stable across the original
    # PostgreSQL-created schema and isolated rehearsal schemas.  Resolve it
    # by its semantic shape, and fail closed on a missing/ambiguous relation
    # rather than guessing a generated identifier.
    legacy_fks = [
        fk for fk in inspector.get_foreign_keys("purchase_export_batch")
        if fk.get("constrained_columns") == ["planning_read_snapshot_id"]
        and fk.get("referred_table") == "planning_read_snapshot"
    ]
    if (
        connection.dialect.name != "sqlite"
        and (len(legacy_fks) != 1 or not legacy_fks[0].get("name"))
    ):
        raise RuntimeError(
            "purchase export legacy snapshot foreign key is missing or ambiguous"
        )

    changes = dict(
        check="ck_purchase_export_batch_exactly_one_source_anchor",
        index="ix_purchase_export_batch_planning_read_snapshot_id",
        foreign_key=(
            str(legacy_fks[0]["name"])
            if legacy_fks and legacy_fks[0].get("name")
            else None
        ),
    )
    if connection.dialect.name == "sqlite":
        # SQLite cannot alter constraints or drop a referenced column in place;
        # Alembic's batch implementation recreates the table while preserving
        # every unrelated column, index, and foreign key.
        with op.batch_alter_table("purchase_export_batch", recreate="always") as batch:
            batch.drop_constraint(changes["check"], type_="check")
            batch.drop_index(changes["index"])
            if changes["foreign_key"]:
                batch.drop_constraint(changes["foreign_key"], type_="foreignkey")
            batch.drop_column("planning_read_snapshot_id")
            batch.alter_column(
                "current_execution_scope_id",
                existing_type=sa.BigInteger(),
                nullable=False,
            )
            batch.alter_column(
                "current_execution_source_revision",
                existing_type=sa.String(length=256),
                nullable=False,
            )
    else:
        op.drop_constraint(changes["check"], "purchase_export_batch", type_="check")
        op.drop_index(changes["index"], table_name="purchase_export_batch")
        op.drop_constraint(
            changes["foreign_key"], "purchase_export_batch", type_="foreignkey"
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
