"""Plain index on stock_ledger_fact_supersession(old_sle_id).

The freeze-boundary revision lookup (decision §53) joins every revision it
reads to its first superseding batch by ``old_sle_id``; the only existing
index leads with ``import_batch_id`` and cannot serve that join.  A normal
``CREATE INDEX`` (alembic runs in a transaction).

It also drops ``ix_stock_ledger_entry_business_identity_batch`` (20260924_02):
since decision §55 the lookup reads revisions by document
(``recorder_type, recorder_ref``, served by ``ix_stock_ledger_entry_recorder``)
and the same-line-number fast path is resolved in memory, so nothing queries
``stock_ledger_entry`` by ``business_identity`` across revisions any more
(the only identity query, R3 activation, filters ``active`` and uses the
partial identity index).  The downgrade recreates it.
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
    op.drop_index(
        "ix_stock_ledger_entry_business_identity_batch",
        table_name="stock_ledger_entry",
    )


def downgrade() -> None:
    op.create_index(
        "ix_stock_ledger_entry_business_identity_batch",
        "stock_ledger_entry",
        ["business_identity", "ingest_batch_id"],
    )
    op.drop_index(
        "ix_stock_ledger_supersession_old_sle",
        table_name="stock_ledger_fact_supersession",
    )
