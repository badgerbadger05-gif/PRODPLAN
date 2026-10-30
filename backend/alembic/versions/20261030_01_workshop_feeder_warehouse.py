"""Add workshop-level feeder warehouse.

Revision ID: 20261030_01
Revises: 20261029_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20261030_01"
down_revision = "20261029_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workshop_warehouse_bindings",
        sa.Column("feeder_warehouse_ref1c", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_workshop_warehouse_bindings_feeder_warehouse_ref1c",
        "workshop_warehouse_bindings",
        ["feeder_warehouse_ref1c"],
        unique=False,
    )
    op.drop_index(
        "ux_production_material_issues_active_per_product",
        table_name="production_material_issues",
    )
    op.create_index(
        "ux_production_material_issues_active_per_product",
        "production_material_issues",
        ["product_id", "source_warehouse_ref1c"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('draft', 'requested') AND direction = 'issue' "
            "AND exported_ref1c IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_production_material_issues_active_per_product",
        table_name="production_material_issues",
    )
    op.create_index(
        "ux_production_material_issues_active_per_product",
        "production_material_issues",
        ["product_id", "source_warehouse_ref1c"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('draft', 'requested') AND direction = 'issue'"
        ),
    )
    op.drop_index(
        "ix_workshop_warehouse_bindings_feeder_warehouse_ref1c",
        table_name="workshop_warehouse_bindings",
    )
    op.drop_column("workshop_warehouse_bindings", "feeder_warehouse_ref1c")
