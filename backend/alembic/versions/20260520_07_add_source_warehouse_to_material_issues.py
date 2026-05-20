"""add source_warehouse_ref1c to production_material_issues

A material-issue document corresponds to a Document_ПеремещениеЗапасов in 1C —
items move FROM a source warehouse TO a destination warehouse. Pre-existing
`warehouse_ref1c` already stored the destination (the workshop's bound
warehouse from workshop_warehouse_bindings). This migration adds the source
side so the exporter can emit both endpoints of the transfer.

Source remains nullable: when it isn't known the user fills it in 1C on the
draft (Posted=false) document.

Revision ID: 20260520_07
Revises: 20260520_06
Create Date: 2026-05-20 21:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260520_07"
down_revision = "20260520_06"
branch_labels = None
depends_on = None


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_column(inspector, "production_material_issues", "source_warehouse_ref1c"):
        op.add_column(
            "production_material_issues",
            sa.Column("source_warehouse_ref1c", sa.String(length=36), nullable=True),
        )
        op.create_index(
            "ix_production_material_issues_source_warehouse_ref1c",
            "production_material_issues",
            ["source_warehouse_ref1c"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_column(inspector, "production_material_issues", "source_warehouse_ref1c"):
        op.drop_index(
            "ix_production_material_issues_source_warehouse_ref1c",
            table_name="production_material_issues",
        )
        op.drop_column("production_material_issues", "source_warehouse_ref1c")
