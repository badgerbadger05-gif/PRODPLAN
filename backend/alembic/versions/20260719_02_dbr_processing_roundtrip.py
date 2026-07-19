"""DBR фаза 4: порог алерта просроченного кругорейса переработки

Revision ID: 20260719_02
Revises: 20260719_01

dbr_settings.processing_roundtrip_days (дефолт 14) — открытый заказ
переработчику старше N дней считается просроченным кругорейсом (борд §5
питатель-3-гальваника-round-trip.md).

Guard pattern: инспекция перед шагом, повторный прогон / create_all-схема — no-op.
"""

from alembic import op
import sqlalchemy as sa

revision = "20260719_02"
down_revision = "20260719_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)
    columns = set() if inspector is None else {
        row["name"] for row in inspector.get_columns("dbr_settings")
    }
    if inspector is None or "processing_roundtrip_days" not in columns:
        op.add_column(
            "dbr_settings",
            sa.Column("processing_roundtrip_days", sa.Integer(), nullable=False, server_default="14"),
        )


def downgrade() -> None:
    op.drop_column("dbr_settings", "processing_roundtrip_days")
