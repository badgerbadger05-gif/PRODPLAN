"""add employees catalog

Revision ID: 20260526_01
Revises: 20260522_08
Create Date: 2026-05-26 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260526_01"
down_revision = "20260522_08"
branch_labels = None
depends_on = None


def _has_table(inspector, table_name: str) -> bool:
    return table_name in set(inspector.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "employees"):
        op.create_table(
            "employees",
            sa.Column("employee_id", sa.Integer(), nullable=False),
            sa.Column("employee_ref1c", sa.String(length=36), nullable=False),
            sa.Column("employee_code", sa.String(length=50), nullable=True),
            sa.Column("employee_name", sa.String(length=255), nullable=False),
            sa.Column("deletion_mark", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("data_version", sa.String(length=50), nullable=True),
            sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.TIMESTAMP(), server_default=sa.func.now(), nullable=False),
            sa.PrimaryKeyConstraint("employee_id"),
            sa.UniqueConstraint("employee_ref1c"),
        )
        op.create_index("ix_employees_employee_id", "employees", ["employee_id"], unique=False)
        op.create_index("ix_employees_employee_ref1c", "employees", ["employee_ref1c"], unique=False)
        op.create_index("ix_employees_employee_code", "employees", ["employee_code"], unique=False)
        op.create_index("ix_employees_deletion_mark", "employees", ["deletion_mark"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, "employees"):
        op.drop_index("ix_employees_deletion_mark", table_name="employees")
        op.drop_index("ix_employees_employee_code", table_name="employees")
        op.drop_index("ix_employees_employee_ref1c", table_name="employees")
        op.drop_index("ix_employees_employee_id", table_name="employees")
        op.drop_table("employees")
