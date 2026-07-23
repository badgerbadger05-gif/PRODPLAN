"""Add Ledger generation ownership to planning proposals.

Revision ID: 20260723_07
Revises: 20260723_06
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_07"
down_revision = "20260723_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "planned_purchase",
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_planned_purchase_ledger_generation",
        "planned_purchase",
        "ledger_generation",
        ["ledger_generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_planned_purchase_ledger_generation_id",
        "planned_purchase",
        ["ledger_generation_id"],
        unique=False,
    )

    op.add_column(
        "production_products",
        sa.Column("ledger_generation_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_production_products_ledger_generation",
        "production_products",
        "ledger_generation",
        ["ledger_generation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_production_products_ledger_generation_id",
        "production_products",
        ["ledger_generation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_production_products_ledger_generation_id",
        table_name="production_products",
    )
    op.drop_constraint(
        "fk_production_products_ledger_generation",
        "production_products",
        type_="foreignkey",
    )
    op.drop_column("production_products", "ledger_generation_id")

    op.drop_index(
        "ix_planned_purchase_ledger_generation_id",
        table_name="planned_purchase",
    )
    op.drop_constraint(
        "fk_planned_purchase_ledger_generation",
        "planned_purchase",
        type_="foreignkey",
    )
    op.drop_column("planned_purchase", "ledger_generation_id")
