"""Add cumulative frozen BOM component norms.

Revision ID: 20260801_01
Revises: 20260731_10
"""

from alembic import op
import sqlalchemy as sa


revision = "20260801_01"
down_revision = "20260731_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mrp_freeze_component_cumulative",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("freeze_version", sa.Integer(), nullable=False),
        sa.Column("root_item_id", sa.Integer(), nullable=False),
        sa.Column("component_item_id", sa.Integer(), nullable=False),
        sa.Column(
            "cumulative_norm_qty_per_root_unit",
            sa.DECIMAL(precision=15, scale=3),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "cumulative_norm_qty_per_root_unit >= 0",
            name="ck_mrp_freeze_component_cumulative_norm_non_negative",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["root_item_id"], ["items.item_id"]),
        sa.ForeignKeyConstraint(["component_item_id"], ["items.item_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "freeze_version",
            "root_item_id",
            "component_item_id",
            name="ux_mrp_freeze_component_cumulative_root",
        ),
    )
    op.create_index(
        "ix_mrp_freeze_component_cumulative_run_version",
        "mrp_freeze_component_cumulative",
        ["run_id", "freeze_version"],
        unique=False,
    )
    op.create_index(
        "ix_mrp_freeze_component_cumulative_root",
        "mrp_freeze_component_cumulative",
        ["root_item_id"],
        unique=False,
    )
    op.create_index(
        "ix_mrp_freeze_component_cumulative_component",
        "mrp_freeze_component_cumulative",
        ["component_item_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_mrp_freeze_component_cumulative_component", table_name="mrp_freeze_component_cumulative")
    op.drop_index("ix_mrp_freeze_component_cumulative_root", table_name="mrp_freeze_component_cumulative")
    op.drop_index("ix_mrp_freeze_component_cumulative_run_version", table_name="mrp_freeze_component_cumulative")
    op.drop_table("mrp_freeze_component_cumulative")
