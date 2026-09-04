"""Persist an exact root-scoped selected BOM branch.

Revision ID: 20260904_02
Revises: 20260904_01
"""

from alembic import op
import sqlalchemy as sa


revision = "20260904_02"
down_revision = "20260904_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mrp_freeze_component") as batch:
        batch.drop_constraint("ux_mrp_freeze_component_spec", type_="unique")
        batch.add_column(sa.Column("root_item_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("child_spec_ref", sa.String(length=36), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("child_spec_version", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_mrp_freeze_component_root_item",
            "items",
            ["root_item_id"],
            ["item_id"],
        )
        batch.create_unique_constraint(
            "ux_mrp_freeze_component_root_branch",
            [
                "run_id",
                "freeze_version",
                "root_item_id",
                "parent_item_id",
                "component_item_id",
                "spec_ref",
                "child_spec_ref",
            ],
        )
        batch.create_index(
            "ix_mrp_freeze_component_root_item", ["root_item_id"], unique=False
        )

    op.create_table(
        "mrp_freeze_bom_node",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("freeze_version", sa.Integer(), nullable=False),
        sa.Column("root_item_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("spec_ref", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("spec_version", sa.String(length=64), nullable=True),
        sa.Column(
            "replenishment_mode",
            sa.String(length=20),
            nullable=False,
            server_default="unavailable",
        ),
        sa.Column("replenishment_time_days", sa.Integer(), nullable=True),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column(
            "material_warehouse_ref1c",
            sa.String(length=36),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "output_warehouse_ref1c",
            sa.String(length=36),
            nullable=False,
            server_default="",
        ),
        sa.Column("is_stock_item", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_kitting", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("route_reason", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["run_id"], ["planning_run.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["root_item_id"], ["items.item_id"]),
        sa.ForeignKeyConstraint(["item_id"], ["items.item_id"]),
        sa.ForeignKeyConstraint(["resource_id"], ["production_resources.resource_id"]),
        sa.UniqueConstraint(
            "run_id",
            "freeze_version",
            "root_item_id",
            "item_id",
            "spec_ref",
            name="ux_mrp_freeze_bom_node_scope",
        ),
    )
    op.create_index(
        "ix_mrp_freeze_bom_node_run_version",
        "mrp_freeze_bom_node",
        ["run_id", "freeze_version"],
    )
    op.create_index(
        "ix_mrp_freeze_bom_node_root",
        "mrp_freeze_bom_node",
        ["root_item_id"],
    )
    op.create_index(
        "ix_mrp_freeze_bom_node_item",
        "mrp_freeze_bom_node",
        ["item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_mrp_freeze_bom_node_item", table_name="mrp_freeze_bom_node")
    op.drop_index("ix_mrp_freeze_bom_node_root", table_name="mrp_freeze_bom_node")
    op.drop_index("ix_mrp_freeze_bom_node_run_version", table_name="mrp_freeze_bom_node")
    op.drop_table("mrp_freeze_bom_node")

    with op.batch_alter_table("mrp_freeze_component") as batch:
        batch.drop_index("ix_mrp_freeze_component_root_item")
        batch.drop_constraint("ux_mrp_freeze_component_root_branch", type_="unique")
        batch.drop_constraint("fk_mrp_freeze_component_root_item", type_="foreignkey")
        batch.drop_column("child_spec_version")
        batch.drop_column("child_spec_ref")
        batch.drop_column("root_item_id")
        batch.create_unique_constraint(
            "ux_mrp_freeze_component_spec",
            [
                "run_id",
                "freeze_version",
                "parent_item_id",
                "component_item_id",
                "spec_ref",
            ],
        )
