"""Allow closed status for production plan headers.

Revision ID: 20260726_03
Revises: 20260726_02
"""

from alembic import op
import sqlalchemy as sa

revision = "20260726_03"
down_revision = "20260726_02"
branch_labels = None
depends_on = None


CONSTRAINT_NAME = "ck_production_plan_header_status"
TABLE_NAME = "production_plan_header"
CONSTRAINT_EXPR = "status in ('draft', 'fixed', 'archived', 'closed')"
OLD_CONSTRAINT_EXPR = "status in ('draft', 'fixed', 'archived')"


def _has_constraint(bind, table_name: str, name: str) -> bool:
    inspector = sa.inspect(bind)
    constraints = {row["name"] for row in inspector.get_check_constraints(table_name)}
    return name in constraints


def upgrade() -> None:
    bind = op.get_bind()
    if TABLE_NAME not in sa.inspect(bind).get_table_names():
        return
    if _has_constraint(bind, TABLE_NAME, CONSTRAINT_NAME):
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="check")
    op.create_check_constraint(
        CONSTRAINT_NAME,
        TABLE_NAME,
        CONSTRAINT_EXPR,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if TABLE_NAME not in sa.inspect(bind).get_table_names():
        return
    table = sa.table(
        TABLE_NAME,
        sa.column("status"),
    )
    op.execute(table.update().where(table.c.status == "closed").values(status="archived"))
    if _has_constraint(bind, TABLE_NAME, CONSTRAINT_NAME):
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="check")
    op.create_check_constraint(
        CONSTRAINT_NAME,
        TABLE_NAME,
        OLD_CONSTRAINT_EXPR,
    )
