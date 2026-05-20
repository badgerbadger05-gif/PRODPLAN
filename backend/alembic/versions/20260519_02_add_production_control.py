"""add production control journal and material issues

Revision ID: 20260519_02
Revises: 20260519_01
Create Date: 2026-05-19 14:30:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260519_02"
down_revision = "20260519_01"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "production_order_line_states"):
        op.create_table(
            "production_order_line_states",
            sa.Column("state_id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="new"),
            sa.Column("workshop_id", sa.Integer(), nullable=True),
            sa.Column("planned_start_date", sa.Date(), nullable=True),
            sa.Column("planned_finish_date", sa.Date(), nullable=True),
            sa.Column("opened_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("route_sheet_printed_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("issue_status", sa.String(length=32), nullable=False, server_default="not_requested"),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["product_id"], ["production_products.product_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workshop_id"], ["production_resources.resource_id"]),
            sa.PrimaryKeyConstraint("state_id"),
            sa.UniqueConstraint("product_id", name="ux_production_order_line_states_product"),
        )
        op.create_index("ix_production_order_line_states_state_id", "production_order_line_states", ["state_id"])
        op.create_index("ix_production_order_line_states_status", "production_order_line_states", ["status"])
        op.create_index("ix_production_order_line_states_workshop_id", "production_order_line_states", ["workshop_id"])
        op.create_index("ix_production_order_line_states_planned_start_date", "production_order_line_states", ["planned_start_date"])
        op.create_index("ix_production_order_line_states_planned_finish_date", "production_order_line_states", ["planned_finish_date"])
        op.create_index("ix_production_order_line_states_issue_status", "production_order_line_states", ["issue_status"])

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "production_material_issues"):
        op.create_table(
            "production_material_issues",
            sa.Column("issue_id", sa.Integer(), nullable=False),
            sa.Column("document_number", sa.String(length=50), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("order_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
            sa.Column("warehouse_ref1c", sa.String(length=36), nullable=True),
            sa.Column("initiated_by", sa.String(length=100), nullable=True),
            sa.Column("exported_ref1c", sa.String(length=36), nullable=True),
            sa.Column("exported_at", sa.TIMESTAMP(), nullable=True),
            sa.Column("export_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["order_id"], ["production_orders.order_id"]),
            sa.ForeignKeyConstraint(["product_id"], ["production_products.product_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("issue_id"),
            sa.UniqueConstraint("document_number"),
        )
        op.create_index("ix_production_material_issues_issue_id", "production_material_issues", ["issue_id"])
        op.create_index("ix_production_material_issues_document_number", "production_material_issues", ["document_number"], unique=True)
        op.create_index("ix_production_material_issues_product_id", "production_material_issues", ["product_id"])
        op.create_index("ix_production_material_issues_order_id", "production_material_issues", ["order_id"])
        op.create_index("ix_production_material_issues_status", "production_material_issues", ["status"])
        op.create_index("ix_production_material_issues_warehouse_ref1c", "production_material_issues", ["warehouse_ref1c"])
        op.create_index("ix_production_material_issues_exported_ref1c", "production_material_issues", ["exported_ref1c"])

    inspector = sa.inspect(bind)
    if not _has_table(inspector, "production_material_issue_lines"):
        op.create_table(
            "production_material_issue_lines",
            sa.Column("line_id", sa.Integer(), nullable=False),
            sa.Column("issue_id", sa.Integer(), nullable=False),
            sa.Column("component_item_id", sa.Integer(), nullable=False),
            sa.Column("required_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("issued_qty", sa.DECIMAL(15, 3), nullable=False, server_default="0"),
            sa.Column("unit", sa.String(length=50), nullable=True),
            sa.Column("source_spec_id", sa.Integer(), nullable=True),
            sa.Column("line_status", sa.String(length=32), nullable=False, server_default="planned"),
            sa.Column("created_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.TIMESTAMP(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["component_item_id"], ["items.item_id"]),
            sa.ForeignKeyConstraint(["issue_id"], ["production_material_issues.issue_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["source_spec_id"], ["specifications.spec_id"]),
            sa.PrimaryKeyConstraint("line_id"),
        )
        op.create_index("ix_production_material_issue_lines_line_id", "production_material_issue_lines", ["line_id"])
        op.create_index("ix_production_material_issue_lines_component_item_id", "production_material_issue_lines", ["component_item_id"])
        op.create_index("ix_production_material_issue_lines_line_status", "production_material_issue_lines", ["line_status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "production_material_issue_lines"):
        op.drop_index("ix_production_material_issue_lines_line_status", table_name="production_material_issue_lines")
        op.drop_index("ix_production_material_issue_lines_component_item_id", table_name="production_material_issue_lines")
        op.drop_index("ix_production_material_issue_lines_line_id", table_name="production_material_issue_lines")
        op.drop_table("production_material_issue_lines")

    inspector = sa.inspect(bind)
    if _has_table(inspector, "production_material_issues"):
        op.drop_index("ix_production_material_issues_exported_ref1c", table_name="production_material_issues")
        op.drop_index("ix_production_material_issues_warehouse_ref1c", table_name="production_material_issues")
        op.drop_index("ix_production_material_issues_status", table_name="production_material_issues")
        op.drop_index("ix_production_material_issues_order_id", table_name="production_material_issues")
        op.drop_index("ix_production_material_issues_product_id", table_name="production_material_issues")
        op.drop_index("ix_production_material_issues_document_number", table_name="production_material_issues")
        op.drop_index("ix_production_material_issues_issue_id", table_name="production_material_issues")
        op.drop_table("production_material_issues")

    inspector = sa.inspect(bind)
    if _has_table(inspector, "production_order_line_states"):
        op.drop_index("ix_production_order_line_states_issue_status", table_name="production_order_line_states")
        op.drop_index("ix_production_order_line_states_planned_finish_date", table_name="production_order_line_states")
        op.drop_index("ix_production_order_line_states_planned_start_date", table_name="production_order_line_states")
        op.drop_index("ix_production_order_line_states_workshop_id", table_name="production_order_line_states")
        op.drop_index("ix_production_order_line_states_status", table_name="production_order_line_states")
        op.drop_index("ix_production_order_line_states_state_id", table_name="production_order_line_states")
        op.drop_table("production_order_line_states")
