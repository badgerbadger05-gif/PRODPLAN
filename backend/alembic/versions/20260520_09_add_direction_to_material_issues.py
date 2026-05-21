"""add direction column to production_material_issues

Plan rule (Следующие этапы #6):
> При частичном выпуске создавать обратное перемещение лишних компонентов
> на исходные склады.

This direction column distinguishes the regular outgoing transfer
('issue': source warehouse -> workshop) from the leftover return
('return': workshop -> source warehouse). Both are represented as
ProductionMaterialIssue rows so the existing export pipeline emits them
as Document_ПеремещениеЗапасов; the export service simply uses whatever
source / destination warehouses are stored on the row.

The partial UNIQUE index ux_production_material_issues_active_per_product
is rebuilt to also require direction='issue' — the constraint guarded
against duplicate outgoing drafts, returns are a separate logical batch.

Revision ID: 20260520_09
Revises: 20260520_08
Create Date: 2026-05-20 23:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260520_09"
down_revision = "20260520_08"
branch_labels = None
depends_on = None


def _has_column(inspector, table: str, column: str) -> bool:
    return column in {c["name"] for c in inspector.get_columns(table)}


def _has_index(inspector, table: str, index: str) -> bool:
    return index in {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "production_material_issues", "direction"):
        op.add_column(
            "production_material_issues",
            sa.Column(
                "direction",
                sa.String(length=16),
                nullable=False,
                server_default="issue",
            ),
        )

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

    # Rebuild with direction='issue' in the predicate. Return drafts are
    # allowed to coexist with outgoing drafts because they're a separate
    # logical document.
    op.create_index(
        "ux_production_material_issues_active_per_product",
        "production_material_issues",
        ["product_id"],
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
        postgresql_where=sa.text("status IN ('draft', 'requested')"),
    )

    if _has_column(inspector, "production_material_issues", "direction"):
        op.drop_column("production_material_issues", "direction")
