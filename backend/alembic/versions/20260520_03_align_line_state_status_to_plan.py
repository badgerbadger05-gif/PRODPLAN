"""align production_order_line_states.status set to the plan

Plan rule: колонка журнала называется "Обеспечение" со значениями
shortage / partial / ready / to_move / assembled / produced_partial / produced.
Старый набор new/opened/in_work/waiting_materials/done/cancelled был про
прогресс цеха, а не про обеспечение компонентами. Перекладываем существующие
строки и меняем server_default на shortage. Cancelled оставляем как
out-of-band 8-й статус, потому что в плане нет, но в реальной жизни нужен.

Маппинг:
  new                  -> shortage   (default, coverage not yet evaluated)
  opened               -> ready      (workshop accepted -> materials ok)
  waiting_materials    -> shortage   (waiting on materials = no coverage)
  in_work              -> to_move    (materials are moving / in-progress)
  done                 -> produced
  cancelled            -> cancelled  (kept)

Revision ID: 20260520_03
Revises: 20260520_02
Create Date: 2026-05-20 17:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "20260520_03"
down_revision = "20260520_02"
branch_labels = None
depends_on = None


_OLD_TO_NEW = {
    "new": "shortage",
    "opened": "ready",
    "waiting_materials": "shortage",
    "in_work": "to_move",
    "done": "produced",
    # 'cancelled' stays 'cancelled' — no rewrite.
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "production_order_line_states" not in inspector.get_table_names():
        return

    # 1) remap existing rows
    for old, new in _OLD_TO_NEW.items():
        op.execute(
            sa.text(
                "UPDATE production_order_line_states SET status = :new WHERE status = :old"
            ).bindparams(new=new, old=old)
        )

    # 2) change column default to 'shortage' for newly inserted rows
    op.alter_column(
        "production_order_line_states",
        "status",
        server_default="shortage",
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "production_order_line_states" not in inspector.get_table_names():
        return

    # Reverse mapping is lossy (multiple new statuses collapsed). We do a
    # best-effort downgrade: anything in the plan set goes back to 'new', and
    # restore the old server_default. Cancelled stays cancelled.
    op.execute(
        sa.text(
            "UPDATE production_order_line_states SET status = 'new' "
            "WHERE status IN ('shortage', 'partial', 'ready', 'to_move', 'assembled', "
            "'produced_partial', 'produced')"
        )
    )
    op.alter_column(
        "production_order_line_states",
        "status",
        server_default="new",
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )
