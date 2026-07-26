"""Drop the non-canonical manual DBR contour.

Revision ID: 20260726_06
Revises: 20260726_05

``dbr_assembly_rate`` is intentionally retained: it is the existing assembly
takt master and will be consumed by the canonical drum owner.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_06"
down_revision = "20260726_05"
branch_labels = None
depends_on = None


LEGACY_TABLES_IN_DROP_ORDER = (
    "dbr_feeder_signal",
    "dbr_supermarket_position",
    "dbr_drum_capacity_gap",
    "dbr_drum_slot",
    "dbr_drum_schedule_program",
    "dbr_drum_schedule",
    "dbr_production_program_item",
    "dbr_production_program",
    "dbr_category_supply_risk",
    "dbr_settings",
)


def _drop_source_signal_link(bind) -> None:
    inspector = sa.inspect(bind)
    if "production_products" not in inspector.get_table_names():
        return
    columns = {row["name"] for row in inspector.get_columns("production_products")}
    if "source_dbr_signal_id" not in columns:
        return

    indexes = {
        row["name"]
        for row in inspector.get_indexes("production_products")
        if row.get("name")
    }
    for index_name in (
        "ux_production_products_source_dbr_signal",
        "ix_production_products_source_dbr_signal_id",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name="production_products")

    inspector = sa.inspect(bind)
    foreign_keys = [
        row
        for row in inspector.get_foreign_keys("production_products")
        if row.get("referred_table") == "dbr_feeder_signal"
        or "source_dbr_signal_id" in (row.get("constrained_columns") or ())
    ]
    with op.batch_alter_table("production_products") as batch_op:
        for foreign_key in foreign_keys:
            if foreign_key.get("name"):
                batch_op.drop_constraint(foreign_key["name"], type_="foreignkey")
        batch_op.drop_column("source_dbr_signal_id")


def upgrade() -> None:
    bind = op.get_bind()
    _drop_source_signal_link(bind)
    existing = set(sa.inspect(bind).get_table_names())
    for table_name in LEGACY_TABLES_IN_DROP_ORDER:
        if table_name in existing:
            op.drop_table(table_name)


def downgrade() -> None:
    raise RuntimeError(
        "20260726_06 intentionally removes the non-canonical DBR contour and "
        "cannot be downgraded; restore a pre-migration database backup instead"
    )
