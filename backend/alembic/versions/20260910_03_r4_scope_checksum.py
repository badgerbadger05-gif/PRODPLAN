"""R4 source marker payload checksum for exact idempotency."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_03"
down_revision = "20260910_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "current_replenishment_state",
        sa.Column("scope_checksum", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE current_replenishment_state SET scope_checksum = "
        "repeat('0', 64) WHERE scope_checksum IS NULL"
    )
    op.alter_column("current_replenishment_state", "scope_checksum", nullable=False)


def downgrade() -> None:
    op.drop_column("current_replenishment_state", "scope_checksum")
