"""Schema contract for exact supplier-order and physical receipt lineage."""

import datetime
from decimal import Decimal
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from app import models


def _fixture(db_session):
    item = models.Item(item_code="SUP-LINEAGE", item_name="Supplier lineage")
    batch = models.PhysicalImportBatch(
        batch_key="supplier-lineage-batch",
        status="completed",
        source_watermarks={},
        completed_at=datetime.datetime(2026, 7, 23),
    )
    generation = models.LedgerGeneration(
        generation_key="supplier-lineage-generation",
        status="building",
        source_watermarks={},
        capabilities={},
        physical_import_batch=batch,
        algorithm_version="test/1",
    )
    run = models.PlanningRun(config_snapshot={})
    db_session.add_all([item, generation, run])
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        period_from=datetime.date(2026, 7, 1),
        period_to=datetime.date(2026, 7, 31),
    )
    purchase = models.PlannedPurchase(
        run_id=run.run_id,
        item_id=item.item_id,
        requested_qty=Decimal("7"),
        planned_qty=Decimal("7"),
        qty=Decimal("7"),
        need_date=datetime.date(2026, 7, 31),
        order_date=datetime.date(2026, 7, 23),
        lead_time_days=8,
        bucket_date=datetime.date(2026, 7, 31),
        ledger_generation_id=generation.id,
    )
    sle = models.StockLedgerEntry(
        ingest_batch_id=batch.id,
        source_content_hash="b" * 64,
        item_id=item.item_id,
        qty=Decimal("7"),
        posting_at=datetime.datetime(2026, 7, 23, 12),
        record_type="Receipt",
        movement_kind="receipt",
        recorder_type="Document_ПоступлениеТоваров",
        recorder_ref="receipt-guid",
        line_no="3",
    )
    db_session.add_all([requirement, purchase, sle])
    db_session.flush()
    return generation, requirement, purchase, sle


def test_supplier_lineage_models_preserve_exact_many_to_one_export(db_session):
    generation, requirement, purchase, sle = _fixture(db_session)
    second = models.PlannedPurchase(
        run_id=purchase.run_id,
        item_id=purchase.item_id,
        requested_qty=Decimal("2"),
        planned_qty=Decimal("2"),
        qty=Decimal("2"),
        need_date=purchase.need_date,
        order_date=purchase.order_date,
        lead_time_days=8,
        bucket_date=purchase.bucket_date,
        ledger_generation_id=generation.id,
    )
    db_session.add(second)
    db_session.flush()
    db_session.add_all(
        [
            models.PurchaseExportLineAllocation(
                ledger_generation_id=generation.id,
                supplier_order_ref="order-guid",
                supplier_order_line_no="1",
                planned_purchase_id=purchase.purchase_id,
                allocated_qty=Decimal("5"),
            ),
            models.PurchaseExportLineAllocation(
                ledger_generation_id=generation.id,
                supplier_order_ref="order-guid",
                supplier_order_line_no="1",
                planned_purchase_id=second.purchase_id,
                allocated_qty=Decimal("2"),
            ),
            models.StockLedgerSupplierReceiptProvenance(
                ledger_generation_id=generation.id,
                stock_ledger_entry_id=sle.id,
                receipt_doc_type=sle.recorder_type,
                receipt_doc_ref=sle.recorder_ref,
                receipt_doc_line_no=sle.line_no,
                supplier_order_ref="order-guid",
                supplier_order_line_no="1",
                match_rule="1c-base-document-line",
                match_status="exact",
                ambiguity_count=0,
            ),
            models.MrpExecutionAllocation(
                ledger_generation_id=generation.id,
                cycle_id="supplier-test",
                requirement_id=requirement.id,
                fact_type="supplier_receipt",
                allocation_kind="coverage_realization",
                fact_ref=sle.recorder_ref,
                fact_line_ref=sle.line_no,
                fact_date=sle.posting_at,
                allocated_qty=Decimal("7"),
                stock_ledger_entry_id=sle.id,
            ),
        ]
    )
    db_session.commit()

    export_rows = (
        db_session.query(models.PurchaseExportLineAllocation)
        .order_by(models.PurchaseExportLineAllocation.planned_purchase_id)
        .all()
    )
    assert len(export_rows) == 2
    assert sum((row.allocated_qty for row in export_rows), Decimal("0")) == Decimal("7")
    provenance = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).one()
    assert provenance.stock_ledger_entry_id == sle.id
    assert provenance.match_status == "exact"
    allocation = db_session.query(models.MrpExecutionAllocation).one()
    assert allocation.stock_ledger_entry_id == sle.id


def test_one_receipt_document_line_may_emit_multiple_physical_sles(db_session):
    generation, _requirement, _purchase, first_sle = _fixture(db_session)
    second_sle = models.StockLedgerEntry(
        ingest_batch_id=first_sle.ingest_batch_id,
        source_content_hash="c" * 64,
        item_id=first_sle.item_id,
        warehouse_ref1c="second-physical-key",
        qty=Decimal("7"),
        posting_at=first_sle.posting_at,
        record_type=first_sle.record_type,
        movement_kind=first_sle.movement_kind,
        recorder_type=first_sle.recorder_type,
        recorder_ref=first_sle.recorder_ref,
        line_no=first_sle.line_no,
    )
    db_session.add(second_sle)
    db_session.flush()

    for sle in (first_sle, second_sle):
        db_session.add(
            models.StockLedgerSupplierReceiptProvenance(
                ledger_generation_id=generation.id,
                stock_ledger_entry_id=sle.id,
                receipt_doc_type=sle.recorder_type,
                receipt_doc_ref=sle.recorder_ref,
                receipt_doc_line_no=sle.line_no,
                supplier_order_ref="order-guid",
                supplier_order_line_no="1",
                match_rule="1c-base-document-line",
                match_status="exact",
                ambiguity_count=0,
            )
        )
    db_session.commit()

    rows = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).all()
    assert {row.stock_ledger_entry_id for row in rows} == {
        first_sle.id,
        second_sle.id,
    }


def test_supplier_lineage_migration_follows_planning_storage_head():
    path = (
        Path(__file__).resolve().parents[2]
        / "backend/alembic/versions/20260723_11_supplier_receipt_lineage.py"
    )
    spec = spec_from_file_location("supplier_receipt_lineage_migration", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "20260723_11"
    assert module.down_revision == "20260723_10"


def test_all_planning_proposals_expose_generation_lineage():
    for model in (
        models.PlannedOrder,
        models.PlannedPurchase,
        models.PlannedRework,
    ):
        column = model.__table__.c.ledger_generation_id
        assert column.nullable is True
        assert column.foreign_keys
        fk = next(iter(column.foreign_keys))
        assert fk.target_fullname == "ledger_generation.id"
        assert fk.ondelete == "RESTRICT"
