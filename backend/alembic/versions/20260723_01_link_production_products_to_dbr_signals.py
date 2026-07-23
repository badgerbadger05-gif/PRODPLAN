"""link production journal rows to DBR feeder signals

Revision ID: 20260723_01
Revises: 20260721_03
Create Date: 2026-07-23 12:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_01"
down_revision = "20260721_03"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_column(inspector, "production_products", "source_dbr_signal_id"):
        op.add_column(
            "production_products",
            sa.Column("source_dbr_signal_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            "fk_production_products_source_dbr_signal_id_dbr_feeder_signal",
            "production_products",
            "dbr_feeder_signal",
            ["source_dbr_signal_id"],
            ["id"],
            ondelete="SET NULL",
        )
    inspector = sa.inspect(bind)
    if not _has_index(inspector, "production_products", "ix_production_products_source_dbr_signal_id"):
        op.create_index(
            "ix_production_products_source_dbr_signal_id",
            "production_products",
            ["source_dbr_signal_id"],
        )
    if not _has_index(inspector, "production_products", "ux_production_products_source_dbr_signal"):
        op.create_index(
            "ux_production_products_source_dbr_signal",
            "production_products",
            ["source_dbr_signal_id"],
            unique=True,
            postgresql_where=sa.text("source_dbr_signal_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_index(inspector, "production_products", "ux_production_products_source_dbr_signal"):
        op.drop_index("ux_production_products_source_dbr_signal", table_name="production_products")
    if _has_index(inspector, "production_products", "ix_production_products_source_dbr_signal_id"):
        op.drop_index("ix_production_products_source_dbr_signal_id", table_name="production_products")
    if _has_column(inspector, "production_products", "source_dbr_signal_id"):
        try:
            op.drop_constraint(
                "fk_production_products_source_dbr_signal_id_dbr_feeder_signal",
                "production_products",
                type_="foreignkey",
            )
        except Exception:
            pass
        op.drop_column("production_products", "source_dbr_signal_id")
