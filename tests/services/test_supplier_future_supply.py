"""Focused contract tests for supplier future-supply evidence."""

from datetime import datetime, date
from decimal import Decimal

from app import models
from app.services.item_ledger.supplier_future_supply import supplier_future_supply_evidence


def _context(db):
    cutoff = datetime(2026, 7, 31, 23, 59)
    item = models.Item(item_code="FUT-SUP", item_name="Future supplier")
    physical = models.PhysicalImportBatch(
        batch_key="future-supplier-physical", status="completed", cutoff=cutoff,
        completed_at=cutoff, source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="future-supplier-generation", status="building", cutoff=cutoff,
        source_watermarks={}, capabilities={}, physical_import_batch=physical,
        algorithm_version="test",
    )
    run = models.PlanningRun(config_snapshot={})
    db.add_all((item, generation, run))
    db.flush()
    purchase = models.PlannedPurchase(
        run_id=run.run_id, item_id=item.item_id, requested_qty=Decimal("10"),
        planned_qty=Decimal("10"), qty=Decimal("10"), need_date=date(2026, 8, 10),
        order_date=date(2026, 7, 20), lead_time_days=10, bucket_date=date(2026, 8, 10),
        ledger_generation_id=generation.id,
    )
    db.add(purchase)
    db.flush()
    allocation = models.PurchaseExportLineAllocation(
        ledger_generation_id=generation.id, supplier_order_ref="order-1",
        supplier_order_line_no="1", planned_purchase_id=purchase.purchase_id,
        allocated_qty=Decimal("10"),
    )
    link = models.SyncLink(
        source_system="PRODPLAN", source_doctype="planned_purchase",
        source_id=purchase.purchase_id, target_system="1C",
        target_entity="Document_ЗаказПоставщику", target_ref_key="order-1",
        ledger_generation_id=generation.id, status="success",
    )
    db.add_all((allocation, link))
    db.flush()
    return generation, item, allocation


def _receipt(db, generation, item, *, qty, ingest_batch_id=None, suffix="a"):
    sle = models.StockLedgerEntry(
        ingest_batch_id=ingest_batch_id or generation.physical_import_batch_id,
        source_content_hash=(suffix * 64)[:64], item_id=item.item_id, qty=Decimal(qty),
        posting_at=datetime(2026, 7, 25), record_type="Receipt", movement_kind="receipt",
        recorder_type="Document_ПоступлениеТоваров", recorder_ref=f"receipt-{suffix}", line_no="1",
    )
    db.add(sle)
    db.flush()
    db.add(models.StockLedgerSupplierReceiptProvenance(
        ledger_generation_id=generation.id, stock_ledger_entry_id=sle.id,
        receipt_doc_type=sle.recorder_type, receipt_doc_ref=sle.recorder_ref,
        receipt_doc_line_no=sle.line_no, supplier_order_ref="order-1",
        supplier_order_line_no="1", operation_kind="supplier_receipt",
        evidence_hash="e" * 64, evidence_payload={}, match_rule="exact", match_status="exact",
        ambiguity_count=0,
    ))


def test_old_unstamped_export_is_rejected_even_with_exact_receipt(db_session):
    generation, item, _allocation = _context(db_session)
    _receipt(db_session, generation, item, qty="4")
    db_session.flush()

    rows = supplier_future_supply_evidence(db_session, generation.id)

    assert len(rows) == 1
    assert rows[0].evidence_status == "rejected"
    assert rows[0].reason == "destination_not_stamped"
    assert rows[0].ordered_qty_at_cutoff == Decimal("10")
    # Receipt is retained for audit, but rejected rows become no future supply
    # when passed to the capture core.
    assert rows[0].realized_qty_at_cutoff == Decimal("4")


def test_stamped_line_counts_only_visible_exact_receipt_provenance(db_session):
    generation, item, allocation = _context(db_session)
    # These are future exporter fields.  The adapter intentionally does not
    # infer them; it merely becomes exact once an immutable exporter adds them.
    allocation.planning_stock_pool = "main"
    allocation.destination_warehouse_ref1c = "warehouse-1"
    _receipt(db_session, generation, item, qty="4", suffix="a")
    later = models.PhysicalImportBatch(
        batch_key="future-supplier-later", status="completed", cutoff=datetime(2026, 8, 1),
        completed_at=datetime(2026, 8, 1), source_watermarks={},
    )
    db_session.add(later)
    db_session.flush()
    _receipt(db_session, generation, item, qty="9", ingest_batch_id=later.id, suffix="b")
    db_session.flush()

    row = supplier_future_supply_evidence(db_session, generation.id)[0]

    assert row.evidence_status == "exact"
    assert row.realized_qty_at_cutoff == Decimal("4")
    assert row.ordered_qty_at_cutoff == Decimal("10")
    assert row.planning_stock_pool == "main"
