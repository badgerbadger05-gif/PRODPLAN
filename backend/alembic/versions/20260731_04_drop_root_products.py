"""Drop the retired legacy root-products table."""

from alembic import op
import sqlalchemy as sa


revision = "20260731_04"
down_revision = "20260731_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "root_products" not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes("root_products")}
    if "ix_root_products_id" in indexes:
        op.drop_index("ix_root_products_id", table_name="root_products")
    op.drop_table("root_products")


def downgrade() -> None:
    # Recreate an empty compatibility schema; historical rows require backup.
    op.create_table(
        "root_products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id"), nullable=False, unique=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_root_products_id", "root_products", ["id"])
