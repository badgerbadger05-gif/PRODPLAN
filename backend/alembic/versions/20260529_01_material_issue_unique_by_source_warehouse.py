"""allow active material issues per source warehouse

Revision ID: 20260529_01
Revises: 20260527_01
Create Date: 2026-05-29 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260529_01"
down_revision = "20260527_01"
branch_labels = None
depends_on = None


def _has_index(inspector, table: str, index: str) -> bool:
    return index in {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_index(
        inspector,
        "production_material_issues",
        "ux_production_material_issues_active_per_product",
    ):
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


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_index(
        inspector,
        "production_material_issues",
        "ux_production_material_issues_active_per_product",
    ):
        op.drop_index(
            "ux_production_material_issues_active_per_product",
            table_name="production_material_issues",
        )
    op.create_index(
        "ux_production_material_issues_active_per_product",
        "production_material_issues",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('draft', 'requested') AND direction = 'issue'"
        ),
    )
