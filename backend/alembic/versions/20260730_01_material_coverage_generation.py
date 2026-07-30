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
INDEX_NAME = "ix_production_order_line_states_material_coverage_ledger_generation_id"
FK_NAME = "fk_production_order_line_states_material_coverage_ledger_generation_id"


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
    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    if INDEX_NAME not in indexes:
        op.create_index(INDEX_NAME, TABLE_NAME, [COLUMN_NAME], unique=False)
    foreign_keys = {
        foreign_key.get("name")
        for foreign_key in inspector.get_foreign_keys(TABLE_NAME)
    }
    if FK_NAME not in foreign_keys:
        op.create_foreign_key(
            FK_NAME,
            TABLE_NAME,
            "ledger_generation",
            [COLUMN_NAME],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    op.drop_constraint(FK_NAME, TABLE_NAME, type_="foreignkey")
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.drop_column(TABLE_NAME, COLUMN_NAME)
