"""Pin material coverage snapshots to a Ledger generation.

Revision ID: 20260730_01
Revises: 20260726_14
"""

from alembic import op
import sqlalchemy as sa


revision = "20260730_01"
down_revision = "20260726_14"
branch_labels = None
depends_on = None

TABLE_NAME = "production_order_line_states"
COLUMN_NAME = "material_coverage_ledger_generation_id"
INDEX_NAME = "ix_prod_line_state_coverage_generation"
FK_NAME = "fk_prod_line_state_coverage_generation"
# PostgreSQL silently truncated the original 70-byte explicit identifiers to
# these names when the first version of this migration reached a database.
LEGACY_INDEX_NAME = "ix_production_order_line_states_material_coverage_ledger_genera"
LEGACY_FK_NAME = "fk_production_order_line_states_material_coverage_ledger_genera"


def _is_coverage_index(index: dict) -> bool:
    return list(index.get("column_names") or ()) == [COLUMN_NAME]


def _is_coverage_fk(foreign_key: dict) -> bool:
    return (
        list(foreign_key.get("constrained_columns") or ()) == [COLUMN_NAME]
        and foreign_key.get("referred_table") == "ledger_generation"
        and list(foreign_key.get("referred_columns") or ()) == ["id"]
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    if COLUMN_NAME not in existing:
        op.add_column(
            TABLE_NAME,
            sa.Column(COLUMN_NAME, sa.BigInteger(), nullable=True),
        )
    inspector = sa.inspect(bind)
    indexes = inspector.get_indexes(TABLE_NAME)
    if not any(_is_coverage_index(index) for index in indexes):
        op.create_index(INDEX_NAME, TABLE_NAME, [COLUMN_NAME], unique=False)
    inspector = sa.inspect(bind)
    foreign_keys = inspector.get_foreign_keys(TABLE_NAME)
    if not any(_is_coverage_fk(foreign_key) for foreign_key in foreign_keys):
        op.create_foreign_key(
            FK_NAME,
            TABLE_NAME,
            "ledger_generation",
            [COLUMN_NAME],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return

    foreign_key_names = {
        foreign_key.get("name")
        for foreign_key in inspector.get_foreign_keys(TABLE_NAME)
        if _is_coverage_fk(foreign_key)
        and foreign_key.get("name") in {FK_NAME, LEGACY_FK_NAME}
    }
    for name in sorted(foreign_key_names):
        op.drop_constraint(name, TABLE_NAME, type_="foreignkey")

    inspector = sa.inspect(bind)
    index_names = {
        index.get("name")
        for index in inspector.get_indexes(TABLE_NAME)
        if _is_coverage_index(index)
        and index.get("name") in {INDEX_NAME, LEGACY_INDEX_NAME}
    }
    for name in sorted(index_names):
        op.drop_index(name, table_name=TABLE_NAME)

    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    if COLUMN_NAME in columns:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
