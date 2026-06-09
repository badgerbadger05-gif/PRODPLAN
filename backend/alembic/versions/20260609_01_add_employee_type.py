"""add employee type for brigades

Revision ID: 20260609_01
Revises: 20260605_04
Create Date: 2026-06-09 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260609_01"
down_revision = "20260605_04"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "employees" not in set(inspector.get_table_names()):
        return
    if not _has_column(inspector, "employees", "employee_type"):
        op.add_column(
            "employees",
            sa.Column(
                "employee_type",
                sa.String(length=20),
                nullable=False,
                server_default="employee",
            ),
        )
    try:
        op.create_index("ix_employees_employee_type", "employees", ["employee_type"], unique=False)
    except Exception:
        pass


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "employees" not in set(inspector.get_table_names()):
        return
    try:
        op.drop_index("ix_employees_employee_type", table_name="employees")
    except Exception:
        pass
    if _has_column(inspector, "employees", "employee_type"):
        op.drop_column("employees", "employee_type")
