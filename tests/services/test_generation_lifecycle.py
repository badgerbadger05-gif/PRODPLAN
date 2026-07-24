from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.generation_bootstrap import (
    ALGORITHM_VERSION as BOOTSTRAP_ALGORITHM_VERSION,
)
from app.services.item_ledger.generation_lifecycle import (
    GenerationValidationError,
    accept_generation_build,
    OBLIGATION_ALGORITHM_VERSION,
    materialize_generation_stock_bins,
    validate_generation_build,
    REPLAY_ALGORITHM_VERSION,
)
from app.services.item_ledger.supplier_receipt_odata import (
    SupplierEvidenceDiagnostic,
    SupplierEvidenceExtractionResult,
)
from app.services.item_ledger.supplier_receipt_allocation import (
    RECEIPT_OPERATION,
    SupplierDocumentEvidence,
)


def _generation(
    db,
    key: str,
    *,
    empty: bool = False,
    algorithm_version: str = "test",
    source_watermarks: dict | None = None,
):
    cutoff = datetime(2026, 7, 31, 23, 59)
    batch = models.PhysicalImportBatch(
        batch_key=f"physical-{key}",
        status="completed",
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={"explicit_empty_prefix": True} if empty else {},
    )
    source_watermarks = dict(source_watermarks or {})
    if empty:
        source_watermarks.setdefault("explicit_empty_prefix", True)
    generation = models.LedgerGeneration(
        generation_key=f"generation-{key}",
        status="building",
        cutoff=cutoff,
        physical_import_batch=batch,
        algorithm_version=algorithm_version,
        replay_version="test",
        source_watermarks=source_watermarks,
        capabilities={},
    )
    db.add(generation)
    db.flush()
    return generation


def _add_checkpoint_stages(db_session, generation: models.LedgerGeneration) -> None:
    batch_cutoff = datetime(2026, 7, 31, 23, 58)
    for stage, algorithm_version in (
        ("reservation_materialize", OBLIGATION_ALGORITHM_VERSION),
        ("reservation_replay", REPLAY_ALGORITHM_VERSION),
    ):
        db_session.add(models.LedgerBuildBatch(
            ledger_generation_id=int(generation.id),
            stage=stage,
            batch_key=f"{stage}-{generation.id}",
            status="completed",
            algorithm_version=algorithm_version,
            metrics={},
            completed_at=batch_cutoff,
        ))
    db_session.flush()


def _bootstrap_gate_watermarks(
    generation: models.LedgerGeneration,
    *,
    opening_balance=True,
    historical_import_completed_through=None,
    convergence_valid=True,
    convergence_cutoff=None,
    convergence_batch_id=None,
) -> dict[str, object]:
    return {
        **dict(generation.source_watermarks or {}),
        **(
            {"opening_balance": {}}
            if opening_balance
            else {}
        ),
        "historical_import_completed_through": (
            historical_import_completed_through
            if historical_import_completed_through is not None
            else generation.cutoff.isoformat()
        ),
        "balance_convergence": {
            "valid": convergence_valid,
            "cutoff": (
                convergence_cutoff.isoformat()
                if convergence_cutoff is not None
                else generation.cutoff.isoformat()
            ),
            "physical_import_batch_id": (
                generation.physical_import_batch_id
                if convergence_batch_id is None
                else convergence_batch_id
            ),
        },
    }


def _synthetic(db, key: str = "ok", replenishment_method: str = "Производство"):
    generation = _generation(db, key)
    item = models.Item(
        item_code=f"ITEM-{key}",
        item_name=key,
        replenishment_method=replenishment_method,
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


def _configure_obligation_checkpoint(
    db_session,
    generation: models.LedgerGeneration,
    requirement: models.MrpRequirement,
    *,
    allow_unphased: bool,
    replay_allocated: Decimal | str = "5",
):
    _add_checkpoint_stages(db_session, generation)
    obligation_batch = (
        db_session.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == generation.id,
            models.LedgerBuildBatch.stage == "reservation_materialize",
        )
        .one()
    )
    obligation_batch.metrics = {
        "selected_requirement_ids": [int(requirement.id)],
        "legacy_net_phasing_requirement_ids": (
            [int(requirement.id)] if allow_unphased else []
        ),
    }
    replay_batch = (
        db_session.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == generation.id,
            models.LedgerBuildBatch.stage == "reservation_replay",
        )
        .one()
    )
    replay_batch.metrics = {
        "fact_qty": str(replay_allocated),
        "allocated_qty": str(replay_allocated),
        "unplanned_qty": "0",
    }
    materialize_generation_stock_bins(db_session, int(generation.id))
    if not db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id
    ).first():
        db_session.add(models.ReservationEntry(
            ledger_generation_id=generation.id,
            item_id=requirement.item_id,
            characteristic_ref="",
            organization_ref="",
            planning_stock_pool="selected",
            run_id=requirement.run_id,
            freeze_version=1,
            requirement_id=requirement.id,
            priority_period_from=requirement.period_from,
            priority_period_to=requirement.period_to,
            realization_mode=(
                "make" if requirement.item.replenishment_method == "Производство" else "consume"
            ),
            reserved_qty=Decimal(replay_allocated),
            realized_qty=Decimal(replay_allocated),
            lifecycle_status="active",
        ))
    db_session.flush()


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


def test_validation_rejects_unphased_allocation_for_nonlegacy_make_requirement(db_session):
    generation, requirement = _synthetic(db_session, "no-legacy-unphased")
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
    )
    reservation = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id
    ).one()
    fact = db_session.query(models.StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    db_session.add(models.ReservationEvent(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        item_id=reservation.item_id,
        characteristic_ref=reservation.characteristic_ref,
        organization_ref=reservation.organization_ref,
        planning_stock_pool=reservation.planning_stock_pool,
        event_kind="realize",
        reserved_delta=Decimal("5"),
        realized_delta=Decimal("5"),
        sle_id=fact.id,
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        match_rule="fifo",
        cycle_id=f"historical-replay:g{generation.id}",
        idempotency_key=f"hist:g{generation.id}:sle{fact.id}:r{reservation.id}",
        event_at=fact.posting_at,
    ))
    db_session.add(models.MrpExecutionAllocation(
        ledger_generation_id=generation.id,
        cycle_id=f"historical-replay:g{generation.id}",
        requirement_id=requirement.id,
        bucket_id=None,
        fact_type="linked_production",
        allocation_kind="execution",
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        fact_date=fact.posting_at,
        allocated_qty=Decimal("5"),
    ))
    db_session.commit()

    with pytest.raises(
        GenerationValidationError,
        match="unphased execution allocation requires legacy net-phasing flag",
    ):
        validate_generation_build(db_session, generation.id)


def test_validation_allows_unphased_allocation_for_legacy_flagged_make_requirement(db_session):
    generation, requirement = _synthetic(db_session, "legacy-unphased")
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=True,
    )
    reservation = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id
    ).one()
    fact = db_session.query(models.StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    db_session.add(models.ReservationEvent(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        item_id=reservation.item_id,
        characteristic_ref=reservation.characteristic_ref,
        organization_ref=reservation.organization_ref,
        planning_stock_pool=reservation.planning_stock_pool,
        event_kind="realize",
        reserved_delta=Decimal("5"),
        realized_delta=Decimal("5"),
        sle_id=fact.id,
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        match_rule="fifo",
        cycle_id=f"historical-replay:g{generation.id}",
        idempotency_key=f"legacy-hist:g{generation.id}:sle{fact.id}:r{reservation.id}",
        event_at=fact.posting_at,
    ))
    db_session.add(models.MrpExecutionAllocation(
        ledger_generation_id=generation.id,
        cycle_id=f"historical-replay:g{generation.id}",
        requirement_id=requirement.id,
        bucket_id=None,
        fact_type="linked_production",
        allocation_kind="execution",
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        fact_date=fact.posting_at,
        allocated_qty=Decimal("5"),
    ))
    db_session.commit()

    result = validate_generation_build(db_session, generation.id)

    assert result["valid"] is True
    assert result["execution_allocations"] == 1


def test_validation_allows_bucketless_allocation_without_legacy_flag(
    db_session,
):
    generation, requirement = _synthetic(
        db_session,
        "bucketless",
        replenishment_method="Покупка",
    )
    db_session.query(models.MrpRequirementBucket).filter(
        models.MrpRequirementBucket.requirement_id == requirement.id
    ).delete()
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
    )
    reservation = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id
    ).one()
    fact = db_session.query(models.StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    db_session.add(models.ReservationEvent(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        item_id=reservation.item_id,
        characteristic_ref=reservation.characteristic_ref,
        organization_ref=reservation.organization_ref,
        planning_stock_pool=reservation.planning_stock_pool,
        event_kind="realize",
        reserved_delta=Decimal("5"),
        realized_delta=Decimal("5"),
        sle_id=fact.id,
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        match_rule="fifo",
        cycle_id=f"historical-replay:g{generation.id}",
        idempotency_key="bucketless-hist:g{generation.id}:sle{fact.id}:r{reservation.id}",
        event_at=fact.posting_at,
    ))
    db_session.add(models.MrpExecutionAllocation(
        ledger_generation_id=generation.id,
        cycle_id=f"historical-replay:g{generation.id}",
        requirement_id=requirement.id,
        bucket_id=None,
        fact_type="component_consumption",
        allocation_kind="execution",
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        fact_date=fact.posting_at,
        allocated_qty=Decimal("5"),
    ))
    db_session.commit()

    result = validate_generation_build(db_session, generation.id)

    assert result["valid"] is True
    assert result["execution_allocations"] == 1


def test_validation_rejects_non_make_legacy_bucketless_flag(
    db_session,
):
    generation, requirement = _synthetic(
        db_session,
        "consume-legacy-flag",
        replenishment_method="Покупка",
    )
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=True,
    )
    reservation = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id
    ).one()
    fact = db_session.query(models.StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    db_session.add(models.ReservationEvent(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        item_id=reservation.item_id,
        characteristic_ref=reservation.characteristic_ref,
        organization_ref=reservation.organization_ref,
        planning_stock_pool=reservation.planning_stock_pool,
        event_kind="realize",
        reserved_delta=Decimal("5"),
        realized_delta=Decimal("5"),
        sle_id=fact.id,
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        match_rule="fifo",
        cycle_id=f"historical-replay:g{generation.id}",
        idempotency_key=f"consume-hist:g{generation.id}:sle{fact.id}:r{reservation.id}",
        event_at=fact.posting_at,
    ))
    db_session.add(models.MrpExecutionAllocation(
        ledger_generation_id=generation.id,
        cycle_id=f"historical-replay:g{generation.id}",
        requirement_id=requirement.id,
        bucket_id=None,
        fact_type="component_consumption",
        allocation_kind="execution",
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        fact_date=fact.posting_at,
        allocated_qty=Decimal("5"),
    ))
    db_session.commit()

    with pytest.raises(
        GenerationValidationError,
        match="unphased execution allocation requires legacy net-phasing flag",
    ):
        validate_generation_build(db_session, generation.id)


def test_validation_rejects_malformed_legacy_metric_ids(db_session):
    generation, requirement = _synthetic(
        db_session,
        "bad-metric-ids",
        replenishment_method="Покупка",
    )
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
    )
    obligation_batch = (
        db_session.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == generation.id,
            models.LedgerBuildBatch.stage == "reservation_materialize",
        )
        .one()
    )
    obligation_batch.metrics["legacy_net_phasing_requirement_ids"] = ["bad-id"]
    db_session.flush()
    reservation = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id
    ).one()
    fact = db_session.query(models.StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id
    ).one()
    db_session.add(models.ReservationEvent(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        item_id=reservation.item_id,
        characteristic_ref=reservation.characteristic_ref,
        organization_ref=reservation.organization_ref,
        planning_stock_pool=reservation.planning_stock_pool,
        event_kind="realize",
        reserved_delta=Decimal("5"),
        realized_delta=Decimal("5"),
        sle_id=fact.id,
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        match_rule="fifo",
        cycle_id=f"historical-replay:g{generation.id}",
        idempotency_key=f"malformed-hist:g{generation.id}:sle{fact.id}:r{reservation.id}",
        event_at=fact.posting_at,
    ))
    db_session.add(models.MrpExecutionAllocation(
        ledger_generation_id=generation.id,
        cycle_id=f"historical-replay:g{generation.id}",
        requirement_id=requirement.id,
        bucket_id=None,
        fact_type="component_consumption",
        allocation_kind="execution",
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        fact_date=fact.posting_at,
        allocated_qty=Decimal("5"),
    ))
    db_session.flush()

    with pytest.raises(
        GenerationValidationError,
        match="legacy_net_phasing_requirement_ids must contain integer ids",
    ):
        validate_generation_build(db_session, generation.id)


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


def test_bootstrap_validation_gate_requires_opening_balance_and_convergence_fields(
    db_session,
):
    generation = _generation(
        db_session,
        "bootstrap-gate-ok",
        empty=True,
        algorithm_version=BOOTSTRAP_ALGORITHM_VERSION,
    )
    _add_checkpoint_stages(db_session, generation)
    generation.source_watermarks = _bootstrap_gate_watermarks(generation)
    db_session.commit()

    result = validate_generation_build(db_session, generation.id, explicit_empty_physical=True)

    assert result["valid"] is True


def test_bootstrap_validation_gate_rejects_missing_opening_balance(
    db_session,
):
    generation = _generation(
        db_session,
        "bootstrap-gate-missing-open",
        empty=True,
        algorithm_version=BOOTSTRAP_ALGORITHM_VERSION,
    )
    _add_checkpoint_stages(db_session, generation)
    generation.source_watermarks = {
        "historical_import_completed_through": generation.cutoff.isoformat(),
        "balance_convergence": {
            "valid": True,
            "cutoff": generation.cutoff.isoformat(),
            "physical_import_batch_id": generation.physical_import_batch_id,
        },
    }
    db_session.commit()

    with pytest.raises(
        GenerationValidationError, match="source_watermarks.opening_balance"
    ):
        validate_generation_build(db_session, generation.id, explicit_empty_physical=True)


def test_bootstrap_validation_gate_rejects_misaligned_convergence_batch(
    db_session,
):
    generation = _generation(
        db_session,
        "bootstrap-gate-misaligned",
        empty=True,
        algorithm_version=BOOTSTRAP_ALGORITHM_VERSION,
    )
    _add_checkpoint_stages(db_session, generation)
    generation.source_watermarks = _bootstrap_gate_watermarks(
        generation,
        convergence_batch_id=generation.physical_import_batch_id + 1,
    )
    db_session.commit()

    with pytest.raises(
        GenerationValidationError,
        match="balance_convergence.physical_import_batch_id",
    ):
        validate_generation_build(db_session, generation.id, explicit_empty_physical=True)


def test_non_bootstrap_generation_is_not_subject_to_bootstrap_gate(
    db_session,
):
    generation = _generation(db_session, "regular-generation", empty=True)
    _add_checkpoint_stages(db_session, generation)
    db_session.commit()

    result = validate_generation_build(db_session, generation.id, explicit_empty_physical=True)

    assert result["valid"] is True


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
