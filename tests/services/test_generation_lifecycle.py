from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.generation_lifecycle import (
    GenerationValidationError,
    accept_generation_build,
    validate_generation_build,
)
from app.services.item_ledger.supplier_receipt_odata import (
    SupplierEvidenceDiagnostic,
    SupplierEvidenceExtractionResult,
)
from app.services.item_ledger.supplier_receipt_allocation import (
    RECEIPT_OPERATION,
    SupplierDocumentEvidence,
)


def _generation(db, key: str, *, empty: bool = False):
    cutoff = datetime(2026, 7, 31, 23, 59)
    batch = models.PhysicalImportBatch(
        batch_key=f"physical-{key}",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True} if empty else {},
    )
    generation = models.LedgerGeneration(
        generation_key=f"generation-{key}",
        status="building",
        cutoff=cutoff,
        physical_import_batch=batch,
        algorithm_version="test",
        replay_version="test",
        source_watermarks={"explicit_empty_prefix": True} if empty else {},
        capabilities={},
    )
    db.add(generation)
    db.flush()
    return generation


def _synthetic(db, key: str = "ok"):
    generation = _generation(db, key)
    item = models.Item(
        item_code=f"ITEM-{key}",
        item_name=key,
        replenishment_method="Производство",
    )
    plan = models.ProductionPlanHeader(
        name=f"Plan {key}",
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
        status="fixed",
        fixed_at=datetime(2026, 7, 1),
        created_by="test",
    )
    db.add_all([item, plan])
    db.flush()
    run = models.PlanningRun(
        source_plan_id=plan.id,
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        period_from=plan.period_from,
        period_to=plan.period_to,
        fixed_at=datetime(2026, 7, 1),
        active_freeze_version=1,
    )
    db.add(run)
    db.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        total_required_qty=5,
        net_required_qty=5,
        covered_qty=0,
        remaining_qty=5,
        period_from=plan.period_from,
        period_to=plan.period_to,
        bom_level=0,
        freeze_version=1,
    )
    db.add(requirement)
    db.flush()
    db.add(models.MrpRequirementBucket(
        requirement_id=requirement.id,
        run_id=run.run_id,
        item_id=item.item_id,
        bucket_date=date(2026, 7, 20),
        gross_qty=5,
        net_qty=5,
    ))
    db.add(models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash=f"hash-{key}",
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH",
        qty=Decimal("5"),
        qty_after=Decimal("999999"),
        posting_at=datetime(2026, 7, 20, 10),
        record_type="Receipt",
        movement_kind="assembly_in",
        recorder_type="Production",
        recorder_ref=f"REC-{key}",
        line_no="1",
        ingest_source="test",
        active=False,
    ))
    db.commit()
    return generation, requirement


def test_successful_synthetic_pipeline_publishes_only_after_validation(db_session):
    generation, requirement = _synthetic(db_session)

    result = accept_generation_build(
        db_session,
        generation.id,
        replay_from=datetime(2026, 7, 1),
    )

    assert result["status"] == "accepted"
    assert result["valid"] is True
    assert result["allocated_qty"] == "5.000"
    db_session.refresh(generation)
    assert generation.status == "accepted"
    assert generation.capabilities["execution_allocations"] is True
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == generation.id
    assert db_session.query(models.StockBin).filter_by(
        ledger_generation_id=generation.id
    ).one().on_hand == Decimal("5")
    assert db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id,
        requirement_id=requirement.id,
    ).count() == 1


def test_failure_rolls_back_every_build_write_and_pointer(db_session, monkeypatch):
    generation, _requirement = _synthetic(db_session, "rollback")

    def fail_after_materialization(*_args, **_kwargs):
        raise RuntimeError("replay failed")

    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle.run_historical_replay",
        fail_after_materialization,
    )
    with pytest.raises(RuntimeError, match="replay failed"):
        accept_generation_build(
            db_session,
            generation.id,
            replay_from=datetime(2026, 7, 1),
        )

    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, generation.id).status == "building"
    assert db_session.query(models.StockBin).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0
    assert db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0
    assert db_session.get(models.PlanningTruthState, 1) is None


def test_accepted_generation_is_immutable(db_session):
    generation, _requirement = _synthetic(db_session, "immutable")
    accept_generation_build(
        db_session, generation.id, replay_from=datetime(2026, 7, 1)
    )
    before = db_session.query(models.StockBin).filter_by(
        ledger_generation_id=generation.id
    ).count()

    with pytest.raises(GenerationValidationError, match="BUILDING"):
        accept_generation_build(
            db_session, generation.id, replay_from=datetime(2026, 7, 1)
        )
    assert db_session.query(models.StockBin).filter_by(
        ledger_generation_id=generation.id
    ).count() == before


def test_foreign_generation_rows_are_isolated(db_session):
    generation, requirement = _synthetic(db_session, "isolation")
    accept_generation_build(
        db_session, generation.id, replay_from=datetime(2026, 7, 1)
    )
    # Validation is BUILDING-only. Recreate the state solely to prove a foreign
    # allocation cannot enter the scoped sums before acceptance.
    generation.status = "building"
    generation.accepted_at = None
    other = _generation(db_session, "foreign", empty=True)
    db_session.add(models.MrpExecutionAllocation(
        ledger_generation_id=other.id,
        cycle_id="foreign",
        requirement_id=requirement.id,
        fact_type="component_consumption",
        allocation_kind="execution",
        fact_ref="foreign",
        fact_line_ref="1",
        allocated_qty=999,
    ))
    db_session.flush()

    result = validate_generation_build(db_session, generation.id)

    assert result["allocated_qty"] == "5.000"
    assert result["execution_allocations"] == 1


def test_empty_prefix_requires_explicit_declaration(db_session):
    implicit = _generation(db_session, "implicit-empty")
    db_session.commit()
    with pytest.raises(GenerationValidationError, match="must be explicit"):
        accept_generation_build(
            db_session,
            implicit.id,
            replay_from=datetime(2026, 7, 1),
        )
    explicit = _generation(db_session, "explicit-empty", empty=True)
    db_session.commit()

    result = accept_generation_build(
        db_session,
        explicit.id,
        replay_from=datetime(2026, 7, 1),
    )

    assert result["physical_facts"] == 0
    assert result["status"] == "accepted"


def test_structural_supplier_evidence_diagnostic_blocks_before_fifo_and_acceptance(
    db_session, monkeypatch
):
    generation, requirement = _synthetic(db_session, "supplier-diagnostic")
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="supplier-diagnostic",
        item_id=requirement.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH",
        qty=Decimal("1"),
        posting_at=datetime(2026, 7, 21),
        record_type="Receipt",
        movement_kind="supplier_receipt",
        recorder_type="Document_ПриходнаяНакладная",
        recorder_ref="receipt-diagnostic",
        line_no="1",
        ingest_source="test",
    ))
    db_session.commit()
    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle."
        "extract_supplier_document_evidence",
        lambda *_args, **_kwargs: SupplierEvidenceExtractionResult(
            evidence=(),
            diagnostics=(SupplierEvidenceDiagnostic(
                recorder_type="Document_ПриходнаяНакладная",
                recorder_ref="receipt-diagnostic",
                line_no="1",
                code="item_mismatch",
                detail="document item differs from Ledger row",
            ),),
            fetched_document_count=1,
        ),
    )

    def fifo_must_not_run(*_args, **_kwargs):
        raise AssertionError("supplier FIFO ran despite incomplete evidence")

    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle."
        "rebuild_supplier_receipt_coverage",
        fifo_must_not_run,
    )

    for _attempt in range(2):
        with pytest.raises(
            GenerationValidationError, match="item_mismatch"
        ):
            accept_generation_build(
                db_session,
                generation.id,
                replay_from=datetime(2026, 7, 1),
                odata_client=object(),
            )

    db_session.expire_all()
    current = db_session.get(models.LedgerGeneration, generation.id)
    assert current.status == "building"
    assert current.capabilities == {}
    assert db_session.get(models.PlanningTruthState, 1) is None
    assert db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).filter_by(ledger_generation_id=generation.id).count() == 0


def test_supplier_candidates_require_explicit_odata_client(db_session):
    generation, requirement = _synthetic(db_session, "supplier-client")
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="supplier-client",
        item_id=requirement.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH",
        qty=Decimal("1"),
        posting_at=datetime(2026, 7, 21),
        record_type="Receipt",
        movement_kind="supplier_receipt",
        recorder_type="Document_ПриходнаяНакладная",
        recorder_ref="receipt-client",
        line_no="1",
        ingest_source="test",
    ))
    db_session.commit()

    with pytest.raises(GenerationValidationError, match="OData client"):
        accept_generation_build(
            db_session,
            generation.id,
            replay_from=datetime(2026, 7, 1),
        )

    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, generation.id).status == "building"
    assert db_session.get(models.PlanningTruthState, 1) is None


def test_direct_supplier_receipt_is_explicitly_unplanned_but_does_not_block(
    db_session, monkeypatch
):
    generation, requirement = _synthetic(db_session, "direct-receipt")
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="direct-receipt",
        item_id=requirement.item_id,
        characteristic_ref="",
        organization_ref="",
        warehouse_ref1c="WH",
        qty=Decimal("3"),
        posting_at=datetime(2026, 7, 21),
        record_type="Receipt",
        movement_kind="receipt",
        recorder_type="Document_ПриходнаяНакладная",
        recorder_ref="direct-receipt",
        line_no="1",
        ingest_source="test",
    ))
    db_session.commit()
    evidence = SupplierDocumentEvidence(
        receipt_doc_type="Document_ПриходнаяНакладная",
        receipt_doc_ref="direct-receipt",
        receipt_doc_line_no="1",
        operation_key=RECEIPT_OPERATION,
        operation_name="Приобретение у поставщика",
        supplier_order_type="",
        supplier_order_ref="",
        supplier_order_line_no="0",
        item_id=requirement.item_id,
        characteristic_ref="",
        warehouse_ref1c="WH",
        signed_qty=Decimal("3"),
    )
    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle."
        "extract_supplier_document_evidence",
        lambda *_args, **_kwargs: SupplierEvidenceExtractionResult(
            evidence=(evidence,),
            diagnostics=(),
            fetched_document_count=1,
        ),
    )

    result = accept_generation_build(
        db_session,
        generation.id,
        replay_from=datetime(2026, 7, 1),
        odata_client=object(),
    )

    assert result["status"] == "accepted"
    assert result["supplier_receipts"]["unplanned_qty"] == "3.000"
    assert result["supplier_receipt_unplanned_qty"] == "3.000"
    assert result["supplier_receipts"]["status_counts"]["unmatched"] == 1
    assert generation.capabilities["supplier_receipt_coverage"] is True
    provenance = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).filter_by(ledger_generation_id=generation.id).one()
    assert provenance.match_status == "unmatched"
