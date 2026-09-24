"""Plain index on stock_ledger_entry(business_identity, ingest_batch_id).

The freeze-boundary test (decision §53) reads every revision of a document
line by its stable ``business_identity``.  The existing identity index is
partial (``WHERE active``) and cannot serve superseded revisions, so the
lookup scanned the table.  A normal ``CREATE INDEX`` (alembic runs in a
transaction, so ``CONCURRENTLY`` is not available); the table is ~250k rows,
which builds in seconds under the writers-stopped deploy window.

Superseded: decision §55 moved the lookup to the document axis
(``recorder_type, recorder_ref``), and no code queries revisions by
``business_identity`` any more.  This migration is retained because it is
already applied on rehearsal copies; 20260924_03 drops the index again.
"""

from alembic import op


revision = "20260924_02"
down_revision = "20260924_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_stock_ledger_entry_business_identity_batch",
        "stock_ledger_entry",
        ["business_identity", "ingest_batch_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stock_ledger_entry_business_identity_batch",
        table_name="stock_ledger_entry",
    )
