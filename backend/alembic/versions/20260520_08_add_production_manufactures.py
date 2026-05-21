"""add production_manufactures (records of выпуск/сборка)

Plan rule (Следующие этапы #5):
> Реализовать кнопку "Произвести": запрос количества и исполнителя, создание
> производства и сдельного наряда.

Each click on "Произвести" creates one production_manufactures row. A single
production_products line can have multiple manufactures (partial production
across days/shifts). The 1C-side document is Document_СборкаЗапасов, posted
later via the manufacture-export service with sync_link integration.

Revision ID: 20260520_08
Revises: 20260520_07
Create Date: 2026-05-20 22:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260520_08"
down_revision = "20260520_07"
branch_labels = None
depends_on = None


def _has_table(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_table(inspector, "production_manufactures"):
        return

    op.create_table(
        "production_manufactures",
        sa.Column("manufacture_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("qty", sa.DECIMAL(15, 3), nullable=False),
        sa.Column("executor", sa.String(length=100), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        # Local lifecycle: draft -> exported (1C document created) ->
        # cancelled (admin reversal). Mirrors production_material_issues.
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("exported_ref1c", sa.String(length=36), nullable=True),
        sa.Column("exported_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("export_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["production_products.product_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["order_id"], ["production_orders.order_id"]),
        sa.PrimaryKeyConstraint("manufacture_id"),
    )
    op.create_index(
        "ix_production_manufactures_product_id",
        "production_manufactures",
        ["product_id"],
    )
    op.create_index(
        "ix_production_manufactures_order_id",
        "production_manufactures",
        ["order_id"],
    )
    op.create_index(
        "ix_production_manufactures_status",
        "production_manufactures",
        ["status"],
    )
    op.create_index(
        "ix_production_manufactures_exported_ref1c",
        "production_manufactures",
        ["exported_ref1c"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_table(inspector, "production_manufactures"):
        return
    op.drop_index(
        "ix_production_manufactures_exported_ref1c",
        table_name="production_manufactures",
    )
    op.drop_index(
        "ix_production_manufactures_status",
        table_name="production_manufactures",
    )
    op.drop_index(
        "ix_production_manufactures_order_id",
        table_name="production_manufactures",
    )
    op.drop_index(
        "ix_production_manufactures_product_id",
        table_name="production_manufactures",
    )
    op.drop_table("production_manufactures")
