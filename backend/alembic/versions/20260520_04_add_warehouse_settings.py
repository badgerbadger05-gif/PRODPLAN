"""add workshop -> warehouse binding and ignored-warehouse settings

Plan rules:
- "привязка участок -> склад получатель"
- "список игнорируемых складов" — чтобы не задавать лишние вопросы по
  остаткам, например если компонент лежит в изоляторе брака.

Revision ID: 20260520_04
Revises: 20260520_03
Create Date: 2026-05-20 18:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260520_04"
down_revision = "20260520_03"
branch_labels = None
depends_on = None


def _has_table(inspector, name: str) -> bool:
    return name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "workshop_warehouse_bindings"):
        op.create_table(
            "workshop_warehouse_bindings",
            sa.Column("binding_id", sa.Integer(), nullable=False),
            sa.Column("workshop_id", sa.Integer(), nullable=False),
            sa.Column("warehouse_ref1c", sa.String(length=36), nullable=False),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["workshop_id"], ["production_resources.resource_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("binding_id"),
            sa.UniqueConstraint("workshop_id", name="ux_workshop_warehouse_bindings_workshop"),
        )
        op.create_index(
            "ix_workshop_warehouse_bindings_workshop_id",
            "workshop_warehouse_bindings",
            ["workshop_id"],
        )
        op.create_index(
            "ix_workshop_warehouse_bindings_warehouse_ref1c",
            "workshop_warehouse_bindings",
            ["warehouse_ref1c"],
        )

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "ignored_warehouses"):
        op.create_table(
            "ignored_warehouses",
            sa.Column("warehouse_ref1c", sa.String(length=36), nullable=False),
            sa.Column("warehouse_name", sa.String(length=255), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.PrimaryKeyConstraint("warehouse_ref1c"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_table(inspector, "ignored_warehouses"):
        op.drop_table("ignored_warehouses")

    inspector = sa.inspect(bind)
    if _has_table(inspector, "workshop_warehouse_bindings"):
        op.drop_index(
            "ix_workshop_warehouse_bindings_warehouse_ref1c",
            table_name="workshop_warehouse_bindings",
        )
        op.drop_index(
            "ix_workshop_warehouse_bindings_workshop_id",
            table_name="workshop_warehouse_bindings",
        )
        op.drop_table("workshop_warehouse_bindings")
