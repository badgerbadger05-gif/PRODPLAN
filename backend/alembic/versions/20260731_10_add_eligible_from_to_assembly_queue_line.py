"""Add eligible_from to assembly queue lines.

Revision ID: 20260731_10
Revises: 20260731_09
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_10"
down_revision = "20260731_09"
branch_labels = None
depends_on = None


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    if table not in set(inspector.get_table_names()):
        return False
    return column in {row["name"] for row in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_column(inspector, "assembly_queue_line", "eligible_from"):
        return

    op.add_column(
        "assembly_queue_line",
        sa.Column(
            "eligible_from",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if (
        "assembly_queue_line" in set(inspector.get_table_names())
        and _has_column(inspector, "assembly_queue_line", "eligible_from")
    ):
        op.drop_column("assembly_queue_line", "eligible_from")
