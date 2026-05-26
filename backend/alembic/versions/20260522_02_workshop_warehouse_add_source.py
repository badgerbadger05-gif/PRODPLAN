"""add source_warehouse_ref1c to production_control_workshop_warehouses

Revision ID: 20260522_02
Revises: 20260522_01
Create Date: 2026-05-22 10:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260522_02"
down_revision = "20260522_01"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "production_control_workshop_warehouses", "source_warehouse_ref1c"):
        op.add_column(
            "production_control_workshop_warehouses",
            sa.Column("source_warehouse_ref1c", sa.String(length=36), nullable=True),
        )
    inspector = sa.inspect(bind)
    if not _has_index(
        inspector,
        "production_control_workshop_warehouses",
        "ix_pcww_source_warehouse_ref1c",
    ):
        op.create_index(
            "ix_pcww_source_warehouse_ref1c",
            "production_control_workshop_warehouses",
            ["source_warehouse_ref1c"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_index(inspector, "production_control_workshop_warehouses", "ix_pcww_source_warehouse_ref1c"):
        op.drop_index(
            "ix_pcww_source_warehouse_ref1c",
            table_name="production_control_workshop_warehouses",
        )
    inspector = sa.inspect(bind)
    if _has_column(inspector, "production_control_workshop_warehouses", "source_warehouse_ref1c"):
        op.drop_column("production_control_workshop_warehouses", "source_warehouse_ref1c")
