"""add production recipient warehouse to workshop settings

Revision ID: 20260527_01
Revises: 20260526_01
Create Date: 2026-05-27 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260527_01"
down_revision = "20260526_01"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "workshop_warehouse_bindings", "production_warehouse_ref1c"):
        op.add_column(
            "workshop_warehouse_bindings",
            sa.Column("production_warehouse_ref1c", sa.String(length=36), nullable=True),
        )
        op.execute(
            """
            UPDATE workshop_warehouse_bindings
               SET production_warehouse_ref1c = warehouse_ref1c
             WHERE production_warehouse_ref1c IS NULL
               AND warehouse_ref1c IS NOT NULL
            """
        )

    inspector = sa.inspect(bind)
    if not _has_index(
        inspector,
        "workshop_warehouse_bindings",
        "ix_workshop_warehouse_bindings_production_warehouse_ref1c",
    ):
        op.create_index(
            "ix_workshop_warehouse_bindings_production_warehouse_ref1c",
            "workshop_warehouse_bindings",
            ["production_warehouse_ref1c"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_index(
        inspector,
        "workshop_warehouse_bindings",
        "ix_workshop_warehouse_bindings_production_warehouse_ref1c",
    ):
        op.drop_index(
            "ix_workshop_warehouse_bindings_production_warehouse_ref1c",
            table_name="workshop_warehouse_bindings",
        )
    inspector = sa.inspect(bind)
    if _has_column(inspector, "workshop_warehouse_bindings", "production_warehouse_ref1c"):
        op.drop_column("workshop_warehouse_bindings", "production_warehouse_ref1c")
