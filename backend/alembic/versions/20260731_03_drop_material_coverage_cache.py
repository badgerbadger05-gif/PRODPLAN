"""drop mutable production-line material coverage cache

Revision ID: 20260731_03
Revises: 20260731_02
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_03"
down_revision = "20260731_02"
branch_labels = None
depends_on = None

_TABLE = "production_order_line_states"
_COLUMNS = (
    "material_coverage_snapshot",
    "material_coverage_ledger_generation_id",
    "material_coverage_calculated_at",
    "material_coverage_label",
    "material_coverage_status",
)


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _indexes() -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}


def upgrade() -> None:
    indexes = _indexes()
    for name in (
        "ix_prod_line_state_coverage_generation",
        "ix_production_order_line_states_material_coverage_status",
    ):
        if name in indexes:
            op.drop_index(name, table_name=_TABLE)
    columns = _columns()
    for name in _COLUMNS:
        if name in columns:
            op.drop_column(_TABLE, name)


def downgrade() -> None:
    columns = _columns()
    additions = {
        "material_coverage_status": sa.Column("material_coverage_status", sa.String(32), nullable=True),
        "material_coverage_label": sa.Column("material_coverage_label", sa.String(64), nullable=True),
        "material_coverage_calculated_at": sa.Column("material_coverage_calculated_at", sa.TIMESTAMP(), nullable=True),
        "material_coverage_ledger_generation_id": sa.Column("material_coverage_ledger_generation_id", sa.BigInteger(), nullable=True),
        "material_coverage_snapshot": sa.Column("material_coverage_snapshot", sa.JSON(), nullable=True),
    }
    for name in reversed(_COLUMNS):
        if name not in columns:
            op.add_column(_TABLE, additions[name])
    op.create_foreign_key(
        "fk_prod_line_state_coverage_generation",
        _TABLE,
        "ledger_generation",
        ["material_coverage_ledger_generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_prod_line_state_coverage_generation", _TABLE, ["material_coverage_ledger_generation_id"])
    op.create_index("ix_production_order_line_states_material_coverage_status", _TABLE, ["material_coverage_status"])
