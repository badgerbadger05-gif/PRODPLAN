"""add one_c_transfer_ref1c and one_c_transfer_number to production_order_line_states

Revision ID: 20260522_01
Revises: 20260520_01
Create Date: 2026-05-22 09:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260522_01"
down_revision = "20260520_01"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "production_order_line_states", "one_c_transfer_ref1c"):
        op.add_column(
            "production_order_line_states",
            sa.Column("one_c_transfer_ref1c", sa.String(length=36), nullable=True),
        )
    if not _has_column(inspector, "production_order_line_states", "one_c_transfer_number"):
        op.add_column(
            "production_order_line_states",
            sa.Column("one_c_transfer_number", sa.String(length=50), nullable=True),
        )

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "production_order_line_states", "ix_pol_states_one_c_transfer_ref1c"):
        op.create_index(
            "ix_pol_states_one_c_transfer_ref1c",
            "production_order_line_states",
            ["one_c_transfer_ref1c"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_index(inspector, "production_order_line_states", "ix_pol_states_one_c_transfer_ref1c"):
        op.drop_index("ix_pol_states_one_c_transfer_ref1c", table_name="production_order_line_states")

    inspector = sa.inspect(bind)
    for col in ["one_c_transfer_number", "one_c_transfer_ref1c"]:
        if _has_column(inspector, "production_order_line_states", col):
            op.drop_column("production_order_line_states", col)
