"""Compact StockBin to one current row per complete physical key."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_08"
down_revision = "20260910_07"
branch_labels = None
depends_on = None


def _deduplicate(bind) -> None:
    # Historical generations are provenance copies, not additive stock. Keep
    # the newest deterministic row for each full physical key before enforcing
    # the compact unique key. The accepted Ledger fold remains the writer of
    # the resulting value on the next publication.
    current = bind.execute(sa.text(
        "SELECT current_generation_id FROM planning_truth_state WHERE id = 1"
    )).scalar()
    rows = bind.execute(sa.text(
        "SELECT id, item_id, characteristic_ref, organization_ref, warehouse_ref1c, ledger_generation_id "
        "FROM stock_bin ORDER BY item_id, characteristic_ref, organization_ref, warehouse_ref1c, id"
    )).mappings().all()
    groups = {}
    for row in rows:
        key = (row["item_id"], row["characteristic_ref"], row["organization_ref"], row["warehouse_ref1c"])
        groups.setdefault(key, []).append(row)
    for key, candidates in groups.items():
        if any(row["ledger_generation_id"] is None for row in candidates):
            raise RuntimeError(f"R6 StockBin provenance missing for physical key {key}")
        if current is None and len(candidates) > 1:
            raise RuntimeError(
                f"R6 StockBin migration ambiguous for physical key {key}: no accepted generation"
            )
        selected = [row for row in candidates if current is not None and row["ledger_generation_id"] == int(current)]
        if current is not None and len(selected) != 1:
            raise RuntimeError(
                f"R6 StockBin migration ambiguous for physical key {key}: "
                f"expected one row for accepted generation {current}, found {len(selected)}"
            )


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("stock_bin", sa.Column("is_current", sa.Boolean(), nullable=True))
    bind.execute(sa.text("UPDATE stock_bin SET is_current = false"))
    _deduplicate(bind)
    if bind.execute(sa.text(
        "SELECT 1 FROM planning_truth_state WHERE id = 1 AND current_generation_id IS NOT NULL"
    )).first() is not None:
        bind.execute(sa.text(
            "UPDATE stock_bin SET is_current = true WHERE ledger_generation_id = "
            "(SELECT current_generation_id FROM planning_truth_state WHERE id = 1)"
        ))
    with op.batch_alter_table("stock_bin") as batch:
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
