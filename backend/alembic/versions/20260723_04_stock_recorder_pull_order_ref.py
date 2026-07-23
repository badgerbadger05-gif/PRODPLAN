"""stock_recorder_pull.order_ref — production-order GUID from the document header

Revision ID: 20260723_04
Revises: 20260723_03

Additive, nullable. pull_recorder_movements (item_ledger/ingest.py) captures the
producing order's GUID from the recorder document header at pull time:
``Document_СборкаЗапасов.ЗаказНаПроизводство_Key`` or
``Document_ПеремещениеЗапасов.ДокументОснование`` (when the basis type is
``Document_ЗаказНаПроизводство``). The SLE→reservation matching
(reservation_ledger._MatchIndex) uses it as the second resolution source after
sync_link, so documents created directly in 1C (mimo нас) still peg to
``ProductionOrder.order_ref1c``. Inspector-guarded so a re-run of
``upgrade head`` is a no-op.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_04"
down_revision = "20260723_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(bind)

    columns = (
        set()
        if inspector is None
        else {row["name"] for row in inspector.get_columns("stock_recorder_pull")}
    )

    if inspector is None or "order_ref" not in columns:
        op.add_column(
            "stock_recorder_pull",
            sa.Column("order_ref", sa.String(length=36), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("stock_recorder_pull", "order_ref")
