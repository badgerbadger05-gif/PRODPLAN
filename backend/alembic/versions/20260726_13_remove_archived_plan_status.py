"""Replace the legacy archived plan state with canonical closed.

Revision ID: 20260726_13
Revises: 20260726_12
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_13"
down_revision = "20260726_12"
branch_labels = None
depends_on = None

TABLE_NAME = "production_plan_header"
CONSTRAINT_NAME = "ck_production_plan_header_status"


def _has_constraint(bind, name: str) -> bool:
    return name in {
        row["name"]
        for row in sa.inspect(bind).get_check_constraints(TABLE_NAME)
    }


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE_NAME not in sa.inspect(bind).get_table_names():
        return
    if _has_constraint(bind, CONSTRAINT_NAME):
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="check")
    table = sa.table(TABLE_NAME, sa.column("status"))
    op.execute(table.update().where(table.c.status == "archived").values(status="closed"))
    op.create_check_constraint(
        CONSTRAINT_NAME,
        TABLE_NAME,
        "status in ('draft', 'fixed', 'closed')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE_NAME not in sa.inspect(bind).get_table_names():
        return
    if _has_constraint(bind, CONSTRAINT_NAME):
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="check")
    op.create_check_constraint(
        CONSTRAINT_NAME,
        TABLE_NAME,
        "status in ('draft', 'fixed', 'archived', 'closed')",
    )
