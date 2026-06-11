"""add per-operation manufacture executors

Revision ID: 20260611_01
Revises: 20260609_01
Create Date: 2026-06-11 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260611_01"
down_revision = "20260609_01"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_table(inspector, "production_manufacture_operations"):
        op.create_table(
            "production_manufacture_operations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("manufacture_id", sa.Integer(), nullable=False),
            sa.Column("spec_operation_id", sa.Integer(), nullable=True),
            sa.Column("operation_id", sa.Integer(), nullable=False),
            sa.Column("line_number", sa.Integer(), nullable=False),
            sa.Column("employee_ref1c", sa.String(length=36), nullable=False),
            sa.Column("employee_name", sa.String(length=255), nullable=False),
            sa.Column("employee_type", sa.String(length=20), nullable=False, server_default="employee"),
            sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["manufacture_id"], ["production_manufactures.manufacture_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["operation_id"], ["operations.operation_id"]),
            sa.ForeignKeyConstraint(["spec_operation_id"], ["spec_operations.spec_operation_id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for index_name, columns in (
        ("ix_production_manufacture_operations_manufacture_id", ["manufacture_id"]),
        ("ix_production_manufacture_operations_spec_operation_id", ["spec_operation_id"]),
        ("ix_production_manufacture_operations_operation_id", ["operation_id"]),
        ("ix_production_manufacture_operations_employee_ref1c", ["employee_ref1c"]),
    ):
        if not _has_index(inspector, "production_manufacture_operations", index_name):
            op.create_index(index_name, "production_manufacture_operations", columns, unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_table(inspector, "production_manufacture_operations"):
        op.drop_table("production_manufacture_operations")
