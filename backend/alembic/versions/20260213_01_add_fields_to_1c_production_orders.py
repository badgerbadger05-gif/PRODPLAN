"""add state fields to production orders and normalize product lines

Revision ID: 20260213_01
Revises: 20260209_01
Create Date: 2026-02-13

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260213_01"
down_revision = "20260209_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- production_orders ---
    op.add_column(
        "production_orders",
        sa.Column("order_state_key", sa.String(36), nullable=True),
    )
    op.add_column(
        "production_orders",
        sa.Column("order_state_name", sa.String(255), nullable=True),
    )
    op.add_column(
        "production_orders",
        sa.Column("deletion_mark", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index(
        "ix_production_orders_order_state_key",
        "production_orders",
        ["order_state_key"],
    )
    op.create_index(
        "ix_production_orders_deletion_mark",
        "production_orders",
        ["deletion_mark"],
    )

    # --- production_products ---
    op.add_column(
        "production_products",
        sa.Column("line_number", sa.Integer(), nullable=True),
    )
    op.add_column(
        "production_products",
        sa.Column("characteristic_ref1c", sa.String(36), nullable=True),
    )
    op.create_index(
        "ix_production_products_line_number",
        "production_products",
        ["line_number"],
    )
    op.create_unique_constraint(
        "ux_production_products_order_line",
        "production_products",
        ["order_id", "line_number"],
    )


def downgrade() -> None:
    # production_products
    op.drop_constraint("ux_production_products_order_line", "production_products", type_="unique")
    op.drop_index("ix_production_products_line_number", table_name="production_products")
    op.drop_column("production_products", "characteristic_ref1c")
    op.drop_column("production_products", "line_number")

    # production_orders
    op.drop_index("ix_production_orders_deletion_mark", table_name="production_orders")
    op.drop_index("ix_production_orders_order_state_key", table_name="production_orders")
    op.drop_column("production_orders", "deletion_mark")
    op.drop_column("production_orders", "order_state_name")
    op.drop_column("production_orders", "order_state_key")

