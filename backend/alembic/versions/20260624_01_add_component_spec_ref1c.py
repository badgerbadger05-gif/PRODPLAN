"""add component_spec_ref1c to spec_components

Закреплённая спецификация компонента (1С: Спецификации_Состав.Спецификация_Key).
Значима для строк типа Сборка/Узел; входит в естественный ключ строки состава
(один компонент может стоять в спецификации несколько раз с разными спеками).

Revision ID: 20260624_01
Revises: 20260611_01
Create Date: 2026-06-24 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260624_01"
down_revision = "20260611_01"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "spec_components" not in set(inspector.get_table_names()):
        return
    if not _has_column(inspector, "spec_components", "component_spec_ref1c"):
        op.add_column(
            "spec_components",
            sa.Column("component_spec_ref1c", sa.String(length=36), nullable=True),
        )
    if not _has_index(inspector, "spec_components", "ix_spec_components_component_spec_ref1c"):
        op.create_index(
            "ix_spec_components_component_spec_ref1c",
            "spec_components",
            ["component_spec_ref1c"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "spec_components" not in set(inspector.get_table_names()):
        return
    if _has_index(inspector, "spec_components", "ix_spec_components_component_spec_ref1c"):
        op.drop_index("ix_spec_components_component_spec_ref1c", table_name="spec_components")
    if _has_column(inspector, "spec_components", "component_spec_ref1c"):
        op.drop_column("spec_components", "component_spec_ref1c")
