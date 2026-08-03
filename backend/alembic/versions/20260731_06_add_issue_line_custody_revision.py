"""Track last emitted custody-event revision on material-issue lines."""

from alembic import op
import sqlalchemy as sa


revision = "20260731_06"
down_revision = "20260731_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "production_material_issue_lines" not in table_names:
        return

    columns = {row["name"] for row in inspector.get_columns("production_material_issue_lines")}
    if "custody_event_revision" not in columns:
        op.add_column(
            "production_material_issue_lines",
            sa.Column(
                "custody_event_revision",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "production_material_issue_lines" not in table_names:
        return
    columns = {row["name"] for row in inspector.get_columns("production_material_issue_lines")}
    if "custody_event_revision" in columns:
        op.drop_column("production_material_issue_lines", "custody_event_revision")
