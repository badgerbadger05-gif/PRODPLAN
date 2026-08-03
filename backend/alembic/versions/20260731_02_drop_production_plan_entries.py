"""Drop legacy production plan entries table."""

from alembic import op
import sqlalchemy as sa


revision = "20260731_02"
down_revision = "20260731_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "production_plan_entries" not in inspector.get_table_names():
        return
    op.drop_table("production_plan_entries")


def downgrade() -> None:
    # Recreate an empty compatibility schema; historical rows require backup.
    op.create_table(
        "production_plan_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.item_id"), nullable=False),
        sa.Column("stage_id", sa.Integer(), sa.ForeignKey("production_stages.stage_id"), nullable=True),
        sa.Column("date", sa.DateTime(), nullable=False),
        sa.Column("planned_qty", sa.DECIMAL(10, 3), nullable=True),
        sa.Column("completed_qty", sa.DECIMAL(10, 3), nullable=True),
        sa.Column("status", sa.String(20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_production_plan_entries_id", "production_plan_entries", ["id"])
