"""Add a compact current custody state without deleting event history."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_09"
down_revision = "20260910_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "production_material_custody_projection",
        sa.Column("is_current", sa.Boolean(), nullable=True),
    )
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE production_material_custody_projection SET is_current = false"
    ))
    with op.batch_alter_table("production_material_custody_projection") as batch:
        batch.alter_column(
            "is_current", existing_type=sa.Boolean(), nullable=False,
        )
    # Only the explicit accepted truth pointer may become current.  If it is
    # absent, no row is guessed current and current custody remains fail-closed.
    bind.execute(sa.text(
        """
        UPDATE production_material_custody_projection AS p
        SET is_current = true
        WHERE p.ledger_generation_id = (
            SELECT current_generation_id FROM planning_truth_state WHERE id = 1
        )
        AND p.id = (
            SELECT max(p2.id)
            FROM production_material_custody_projection AS p2
            WHERE p2.ledger_generation_id = p.ledger_generation_id
              AND p2.product_id = p.product_id
              AND p2.component_item_id = p.component_item_id
              AND p2.location_kind = p.location_kind
              AND p2.warehouse_ref1c = p.warehouse_ref1c
        )
        """
    ))
    op.create_index(
        "ux_pm_custody_current_cell",
        "production_material_custody_projection",
        ["product_id", "component_item_id", "location_kind", "warehouse_ref1c"],
        unique=True,
        postgresql_where=sa.text("is_current = true"),
        sqlite_where=sa.text("is_current = 1"),
    )


def downgrade() -> None:
    op.drop_index("ux_pm_custody_current_cell", table_name="production_material_custody_projection")
    op.drop_column("production_material_custody_projection", "is_current")
