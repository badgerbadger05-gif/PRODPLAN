"""add source_mrp_requirement_id to planned_purchase for MRP traceability

Revision ID: 20260522_08
Revises: 20260522_07
Create Date: 2026-05-25 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260522_08"
down_revision = "20260522_07"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, "planned_purchase", "source_mrp_requirement_id"):
        op.add_column(
            "planned_purchase",
            sa.Column(
                "source_mrp_requirement_id",
                sa.Integer(),
                sa.ForeignKey("mrp_requirement.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, "planned_purchase", "source_mrp_requirement_id"):
        op.drop_column("planned_purchase", "source_mrp_requirement_id")
