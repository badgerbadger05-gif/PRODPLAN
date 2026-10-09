from datetime import datetime
from decimal import Decimal
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest

from app import models
from app.services.item_ledger.generation_lifecycle import _persist_non_supplier_receipt_rows


def test_processing_receipt_migration_preserves_facts_and_protects_downgrade(db_session, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "backend/alembic/versions/20261009_01_non_supplier_receipt_provenance.py"
    spec = importlib.util.spec_from_file_location("processing_receipt_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    operations = Operations(MigrationContext.configure(db_session.connection()))
    monkeypatch.setattr(migration, "op", operations)
    migration._replace_checks(False)
    migration.upgrade()
    migration.downgrade()
    migration.upgrade()
    cutoff = datetime(2026, 10, 8, 11, 16, 57)
    batch = models.PhysicalImportBatch(batch_key="processor-migration", status="completed", cutoff=cutoff,
        completed_at=cutoff, source_complete=True, source_watermarks={})
    item = models.Item(item_code="PROCESSOR-MIGRATION", item_name="Processor migration")
    db_session.add_all([batch, item])
    db_session.flush()
    generation = models.LedgerGeneration(generation_key="processor-migration", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, physical_import_batch_id=batch.id, algorithm_version="test", source_watermarks={})
    db_session.add(generation)
    db_session.flush()
    row = models.StockLedgerEntry(ingest_batch_id=batch.id, item_id=item.item_id,
        source_content_hash="processor-return".ljust(64, "0"), business_identity="processor-return",
        recorder_type="Document_ПриходнаяНакладная", recorder_ref="processor-return", line_no="1",
        warehouse_ref1c="wh", characteristic_ref="", organization_ref="", qty=Decimal("14"),
        posting_at=cutoff, record_type="Receipt", movement_kind="receipt", ingest_source="test")
    db_session.add(row)
    db_session.flush()
    _persist_non_supplier_receipt_rows(db_session, generation_id=generation.id, supplier_candidates=(row,),
        ignored_stock_ledger_entries=((row.id, "8d96f3f0-9934-11eb-e39a-fa163e61326a", "ВозвратОтПереработчика"),))
    assert row.qty == Decimal("14")
    assert db_session.query(models.StockLedgerSupplierReceiptProvenance).one().operation_kind == "non_supplier_receipt"
    with pytest.raises(RuntimeError, match="Cannot downgrade"):
        migration.downgrade()
    assert row.qty == Decimal("14")
