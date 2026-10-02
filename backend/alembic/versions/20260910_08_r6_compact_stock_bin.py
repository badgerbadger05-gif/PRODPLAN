"""Compact StockBin to one current row per complete physical key."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_08"
down_revision = "20260910_07"
branch_labels = None
depends_on = None


def _deduplicate(bind) -> None:
    # Validate provenance copies set-wise. This function never chooses a
    # newest/latest historical row or adds quantities. Only the exact accepted
    # pointer selects current stock in upgrade(); BUILDING staging survives.
    # Return at most one offending key per check instead of materializing the
    # millions of historical StockBin rows into Python groups.
    current = bind.execute(sa.text(
        "SELECT current_generation_id FROM planning_truth_state WHERE id = 1"
    )).scalar()
    keys = "b.item_id, b.characteristic_ref, b.organization_ref, b.warehouse_ref1c"
    checks = [
        (
            f"SELECT {keys} FROM stock_bin b WHERE b.ledger_generation_id IS NULL LIMIT 1",
            "R6 StockBin provenance missing for physical key {key}",
        ),
        (
            f"SELECT {keys} FROM stock_bin b LEFT JOIN ledger_generation g "
            "ON g.id = b.ledger_generation_id WHERE g.status IS NULL LIMIT 1",
            "R6 StockBin generation missing for physical key {key}",
        ),
        (
            f"SELECT {keys} FROM stock_bin b "
            f"GROUP BY b.ledger_generation_id, {keys} HAVING COUNT(*) > 1 LIMIT 1",
            "R6 StockBin migration ambiguous duplicate generation for physical key {key}",
        ),
    ]
    if current is None:
        checks.extend([
            (
                f"SELECT {keys} FROM stock_bin b GROUP BY {keys} HAVING COUNT(*) > 1 LIMIT 1",
                "R6 StockBin migration ambiguous for physical key {key}: no accepted generation",
            ),
            (
                f"SELECT {keys} FROM stock_bin b JOIN ledger_generation g "
                "ON g.id = b.ledger_generation_id WHERE g.status <> 'building' LIMIT 1",
                "R6 StockBin migration has non-building history without accepted pointer for {key}",
            ),
        ])
    for statement, message in checks:
        row = bind.execute(sa.text(statement)).first()
        if row is not None:
            raise RuntimeError(message.format(key=tuple(row)))


def upgrade() -> None:
    bind = op.get_bind()
    # PostgreSQL 11+ stores this constant default as metadata for old rows:
    # avoid an UPDATE of every historical copy and its WAL/index churn. Drop
    # the bootstrap default below to preserve the previous final schema.
    op.add_column("stock_bin", sa.Column(
        "is_current", sa.Boolean(), nullable=True, server_default=sa.false(),
    ))
    _deduplicate(bind)
    if bind.execute(sa.text(
        "SELECT 1 FROM planning_truth_state WHERE id = 1 AND current_generation_id IS NOT NULL"
    )).first() is not None:
        bind.execute(sa.text(
            "UPDATE stock_bin SET is_current = true WHERE ledger_generation_id = "
            "(SELECT current_generation_id FROM planning_truth_state WHERE id = 1)"
        ))
    # Do not retain accepted/failed generation copies after the compact
    # projection is selected.  BUILDING rows are explicit staging and remain
    # available for their own generation lifecycle cleanup.
    bind.execute(sa.text(
        "DELETE FROM stock_bin "
        "WHERE ledger_generation_id IN ("
        "  SELECT g.id FROM ledger_generation g "
        "  WHERE g.status <> 'building' "
        "    AND g.id <> COALESCE((SELECT current_generation_id "
        "                         FROM planning_truth_state WHERE id = 1), -1)"
        ")"
    ))
    with op.batch_alter_table("stock_bin") as batch:
        batch.alter_column(
            "is_current", existing_type=sa.Boolean(), existing_nullable=True,
            server_default=None,
        )
        batch.drop_constraint("ux_stock_bin_ledger_key", type_="unique")
        batch.create_unique_constraint("ux_stock_bin_generation_key", [
            "ledger_generation_id", "item_id", "characteristic_ref",
            "organization_ref", "warehouse_ref1c",
        ])
    op.create_index(
        "ux_stock_bin_current_physical_key", "stock_bin",
        ["item_id", "characteristic_ref", "organization_ref", "warehouse_ref1c"],
        unique=True, postgresql_where=sa.text("is_current = true"),
        sqlite_where=sa.text("is_current = 1"),
    )


def downgrade() -> None:
    op.drop_index("ux_stock_bin_current_physical_key", table_name="stock_bin")
    with op.batch_alter_table("stock_bin") as batch:
        batch.drop_constraint("ux_stock_bin_generation_key", type_="unique")
        batch.create_unique_constraint(
            "ux_stock_bin_ledger_key",
            ["ledger_generation_id", "item_id", "characteristic_ref", "organization_ref", "warehouse_ref1c"],
        )
    op.drop_column("stock_bin", "is_current")
