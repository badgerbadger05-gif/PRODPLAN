"""link production order lines to fixed MRP requirements

Revision ID: 20260522_06
Revises: 20260522_05
Create Date: 2026-05-22 15:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260522_06"
down_revision = "20260522_05"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "production_products", "source_mrp_requirement_id"):
        op.add_column("production_products", sa.Column("source_mrp_requirement_id", sa.Integer(), nullable=True))
    if not _has_column(inspector, "production_products", "source_mrp_allocation_key"):
        op.add_column("production_products", sa.Column("source_mrp_allocation_key", sa.String(length=100), nullable=True))

    inspector = sa.inspect(bind)
    if not _has_index(inspector, "production_products", "ix_production_products_source_mrp_requirement_id"):
        op.create_index(
            "ix_production_products_source_mrp_requirement_id",
            "production_products",
            ["source_mrp_requirement_id"],
            unique=False,
        )
    if not _has_index(inspector, "production_products", "ix_production_products_source_mrp_allocation_key"):
        op.create_index(
            "ix_production_products_source_mrp_allocation_key",
            "production_products",
            ["source_mrp_allocation_key"],
            unique=False,
        )

    inspector = sa.inspect(bind)
    existing_fks = {fk.get("name") for fk in inspector.get_foreign_keys("production_products")}
    if "fk_production_products_source_mrp_requirement_id" not in existing_fks:
        op.create_foreign_key(
            "fk_production_products_source_mrp_requirement_id",
            "production_products",
            "mrp_requirement",
            ["source_mrp_requirement_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    existing_fks = {fk.get("name") for fk in inspector.get_foreign_keys("production_products")}
    if "fk_production_products_source_mrp_requirement_id" in existing_fks:
        op.drop_constraint("fk_production_products_source_mrp_requirement_id", "production_products", type_="foreignkey")

    inspector = sa.inspect(bind)
    for idx_name in (
        "ix_production_products_source_mrp_allocation_key",
        "ix_production_products_source_mrp_requirement_id",
    ):
        if _has_index(inspector, "production_products", idx_name):
            op.drop_index(idx_name, table_name="production_products")

    inspector = sa.inspect(bind)
    for col_name in ("source_mrp_allocation_key", "source_mrp_requirement_id"):
        if _has_column(inspector, "production_products", col_name):
            op.drop_column("production_products", col_name)
