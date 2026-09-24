"""Plain index on stock_ledger_fact_supersession(old_sle_id).

The freeze-boundary revision lookup (decision §53) joins every revision it
reads to its first superseding batch by ``old_sle_id``; the only existing
index leads with ``import_batch_id`` and cannot serve that join.  A normal
``CREATE INDEX`` (alembic runs in a transaction).
"""

from alembic import op


revision = "20260924_03"
down_revision = "20260924_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_stock_ledger_supersession_old_sle",
        "stock_ledger_fact_supersession",
        ["old_sle_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stock_ledger_supersession_old_sle",
        table_name="stock_ledger_fact_supersession",
    )
