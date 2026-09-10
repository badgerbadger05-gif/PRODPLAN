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
    connection = op.get_bind()
    missing_ids = connection.execute(
        sa.text(
            "SELECT id FROM current_replenishment_state "
            "WHERE scope_checksum IS NULL"
        )
    ).scalars().all()
    for state_id in missing_ids:
        connection.execute(
            sa.text(
                "UPDATE current_replenishment_state SET scope_checksum = :checksum "
                "WHERE id = :state_id"
            ),
            {"checksum": "0" * 64, "state_id": int(state_id)},
        )
    op.alter_column("current_replenishment_state", "scope_checksum", nullable=False)


def downgrade() -> None:
    op.drop_column("current_replenishment_state", "scope_checksum")
