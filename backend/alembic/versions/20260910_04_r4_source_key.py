"""Keep current source stream separate from canonical distribution scope."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_04"
down_revision = "20260910_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "current_replenishment_state",
        sa.Column("source_key", sa.String(length=160), nullable=True),
    )
    op.execute(
        "UPDATE current_replenishment_state SET source_key = scope_key "
        "WHERE source_key IS NULL"
    )
    op.alter_column("current_replenishment_state", "source_key", nullable=False)


def downgrade() -> None:
    op.drop_column("current_replenishment_state", "source_key")
