"""add workshop assignment source to line states

Revision ID: 20260615_01
Revises: 20260611_01
Create Date: 2026-06-15 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260615_01"
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
    if not _has_column(inspector, "production_order_line_states", "workshop_id_source"):
        op.add_column(
            "production_order_line_states",
            sa.Column("workshop_id_source", sa.String(length=16), nullable=True),
        )
    if not _has_column(inspector, "production_order_line_states", "workshop_id_set_at"):
        op.add_column(
            "production_order_line_states",
            sa.Column("workshop_id_set_at", sa.TIMESTAMP(), nullable=True),
        )
    if not _has_index(inspector, "production_order_line_states", "ix_production_order_line_states_workshop_id_source"):
        op.create_index(
            "ix_production_order_line_states_workshop_id_source",
            "production_order_line_states",
            ["workshop_id_source"],
            unique=False,
        )
    op.execute(
        """
        UPDATE production_order_line_states
        SET workshop_id_source = 'legacy'
        WHERE workshop_id IS NOT NULL
          AND workshop_id_source IS NULL
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_index(inspector, "production_order_line_states", "ix_production_order_line_states_workshop_id_source"):
        op.drop_index(
            "ix_production_order_line_states_workshop_id_source",
            table_name="production_order_line_states",
        )
    if _has_column(inspector, "production_order_line_states", "workshop_id_set_at"):
        op.drop_column("production_order_line_states", "workshop_id_set_at")
    if _has_column(inspector, "production_order_line_states", "workshop_id_source"):
        op.drop_column("production_order_line_states", "workshop_id_source")
