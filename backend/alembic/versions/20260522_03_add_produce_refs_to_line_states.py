"""add one_c_assembly_ref1c and one_c_piecework_ref1c to production_order_line_states

Revision ID: 20260522_03
Revises: 20260522_02
Create Date: 2026-05-22 11:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260522_03"
down_revision = "20260522_02"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for col_name in ("one_c_assembly_ref1c", "one_c_piecework_ref1c"):
        if not _has_column(inspector, "production_order_line_states", col_name):
            op.add_column(
                "production_order_line_states",
                sa.Column(col_name, sa.String(length=36), nullable=True),
            )

    inspector = sa.inspect(bind)
    for idx_name, col_name in (
        ("ix_pol_states_one_c_assembly_ref1c", "one_c_assembly_ref1c"),
        ("ix_pol_states_one_c_piecework_ref1c", "one_c_piecework_ref1c"),
    ):
        if not _has_index(inspector, "production_order_line_states", idx_name):
            op.create_index(idx_name, "production_order_line_states", [col_name], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for idx_name in ("ix_pol_states_one_c_piecework_ref1c", "ix_pol_states_one_c_assembly_ref1c"):
        if _has_index(inspector, "production_order_line_states", idx_name):
            op.drop_index(idx_name, table_name="production_order_line_states")

    inspector = sa.inspect(bind)
    for col_name in ("one_c_piecework_ref1c", "one_c_assembly_ref1c"):
        if _has_column(inspector, "production_order_line_states", col_name):
            op.drop_column("production_order_line_states", col_name)
