from datetime import date, datetime, timedelta
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
    SupplierReceiptExclusion,
)
from app.services.item_ledger.supplier_receipt_allocation import (
    RECEIPT_OPERATION,
    SupplierDocumentEvidence,
)
from app.services.one_c_export_common import DEFAULT_ORGANIZATION_REF1C


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


def _physical_refresh_gate_watermarks(
    generation: models.LedgerGeneration,
    *,
    historical_import_completed_through=None,
    convergence_valid=True,
    convergence_cutoff=None,
    convergence_batch_id=None,
) -> dict[str, object]:
    return {
        **dict(generation.source_watermarks or {}),
        "generation_kind": "physical_refresh",
        "historical_import_completed_through": (
            historical_import_completed_through.isoformat()
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


def _physical_refresh_gate_ready(
    db_session, key: str = "physical-refresh-gate"
) -> tuple[models.LedgerGeneration, models.MrpRequirement]:
    generation, requirement = _synthetic(
        db_session,
        key,
        replenishment_method="Производство",
    )
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
    )
    generation.source_watermarks = _physical_refresh_gate_watermarks(generation)
    db_session.commit()
    return generation, requirement


def _synthetic(db, key: str = "ok", replenishment_method: str = "Производство"):
    generation = _generation(db, key)
    db.add(models.StockWarehouse(
        warehouse_ref1c="WH",
        warehouse_name="Outside planning contour",
        is_selected=False,
        is_finished_goods=False,
    ))
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
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
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


def _add_matching_reservation_event(
    db_session,
    generation: models.LedgerGeneration,
    requirement: models.MrpRequirement,
) -> None:
    reservation = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id,
        requirement_id=requirement.id,
    ).one()
    fact = db_session.query(models.StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id,
    ).order_by(models.StockLedgerEntry.id.asc()).first()
    assert fact is not None
    db_session.add(models.ReservationEvent(
        ledger_generation_id=generation.id,
        reservation_id=reservation.id,
        item_id=reservation.item_id,
        characteristic_ref=reservation.characteristic_ref,
        organization_ref=reservation.organization_ref,
        planning_stock_pool=reservation.planning_stock_pool,
        event_kind="realize",
        reserved_delta=reservation.reserved_qty,
        realized_delta=reservation.realized_qty,
        sle_id=fact.id,
        fact_ref=fact.recorder_ref,
        fact_line_ref=fact.line_no,
        match_rule="fifo",
        cycle_id=f"historical-replay:g{generation.id}",
        idempotency_key=f"test:g{generation.id}:r{reservation.id}:sle{fact.id}",
        event_at=fact.posting_at,
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


def test_snapshot_build_failure_rolls_back_acceptance_atomically(db_session, monkeypatch):
    generation, _requirement = _synthetic(db_session, "snapshot-rollback")

    def fail_snapshot_build(*_args, **_kwargs):
        raise ValueError("snapshot provenance is incomplete")

    monkeypatch.setattr(
        "app.services.period_plan_service.build_period_plan_execution_snapshots_for_generation",
        fail_snapshot_build,
    )

    with pytest.raises(
        GenerationValidationError,
        match="period-plan execution snapshot build failed",
    ):
        accept_generation_build(
            db_session,
            generation.id,
            replay_from=datetime(2026, 7, 1),
        )

    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, generation.id).status == "building"
    assert db_session.get(models.PlanningTruthState, 1) is None
    assert db_session.query(models.StockBin).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0
    assert db_session.query(models.PlanningReadSnapshot).filter_by(
        ledger_generation_id=generation.id
    ).count() == 0


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


def test_validation_accepts_current_generation_supplier_reservation_cycle(db_session):
    generation, _requirement = _synthetic(db_session, "supplier-cycle")
    accept_generation_build(
        db_session, generation.id, replay_from=datetime(2026, 7, 1)
    )
    generation.status = "building"
    generation.accepted_at = None
    event = db_session.query(models.ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).first()
    event.cycle_id = f"historical-supplier:g{generation.id}:accept"
    db_session.flush()

    result = validate_generation_build(db_session, generation.id)

    assert result["valid"] is True


def test_validation_rejects_foreign_generation_supplier_reservation_cycle(db_session):
    generation, _requirement = _synthetic(db_session, "foreign-supplier-cycle")
    accept_generation_build(
        db_session, generation.id, replay_from=datetime(2026, 7, 1)
    )
    generation.status = "building"
    generation.accepted_at = None
    event = db_session.query(models.ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).first()
    event.cycle_id = f"historical-supplier:g{generation.id + 1}:accept"
    db_session.flush()

    with pytest.raises(
        GenerationValidationError,
        match="legacy reservation event entered generation build",
    ):
        validate_generation_build(db_session, generation.id)


def test_validation_matches_supplier_allocations_to_supplier_realization_events(db_session):
    generation, requirement = _synthetic(db_session, "supplier-realization")
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
        replay_allocated="0",
    )
    reservation = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id
    ).one()
    reservation.realization_mode = "buy"
    reservation.reserved_qty = Decimal("5")
    reservation.realized_qty = Decimal("5")
    _add_matching_reservation_event(db_session, generation, requirement)
    event = db_session.query(models.ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).one()
    event.cycle_id = f"historical-supplier:g{generation.id}:accept"
    db_session.add(models.MrpExecutionAllocation(
        ledger_generation_id=generation.id,
        cycle_id=f"historical-supplier:g{generation.id}:accept",
        requirement_id=requirement.id,
        fact_type="supplier_receipt",
        allocation_kind="execution",
        fact_ref=event.fact_ref,
        fact_line_ref=event.fact_line_ref,
        allocated_qty=Decimal("5"),
    ))
    db_session.flush()

    result = validate_generation_build(db_session, generation.id)

    assert result["valid"] is True
    assert result["allocated_qty"] == "0"


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


def test_validation_uses_gross_capacity_for_consume_allocation(
    db_session,
):
    generation, requirement = _synthetic(
        db_session,
        "validate-consume-gross",
        replenishment_method="Производство",
    )
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
        replay_allocated="26",
    )
    make_reservation = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id,
    ).one()
    make_reservation.reserved_qty = Decimal("0")
    make_reservation.realized_qty = Decimal("0")
    consume_reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=make_reservation.item_id,
        characteristic_ref=make_reservation.characteristic_ref,
        organization_ref=make_reservation.organization_ref,
        planning_stock_pool=make_reservation.planning_stock_pool,
        run_id=make_reservation.run_id,
        freeze_version=1,
        requirement_id=make_reservation.requirement_id,
        priority_period_from=make_reservation.priority_period_from,
        priority_period_to=make_reservation.priority_period_to,
        realization_mode="consume",
        reserved_qty=Decimal("26"),
        realized_qty=Decimal("26"),
        lifecycle_status="active",
    )
    db_session.add(consume_reservation)
    db_session.flush()
    bucket = db_session.query(models.MrpRequirementBucket).filter_by(
        requirement_id=requirement.id,
    ).one()
    bucket.gross_qty = Decimal("26")
    bucket.net_qty = Decimal("0")
    fact = db_session.query(models.StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id,
    ).one()
    db_session.add_all([
        models.ReservationEvent(
            ledger_generation_id=generation.id,
            reservation_id=consume_reservation.id,
            item_id=consume_reservation.item_id,
            characteristic_ref=consume_reservation.characteristic_ref,
            organization_ref=consume_reservation.organization_ref,
            planning_stock_pool=consume_reservation.planning_stock_pool,
            event_kind="realize",
            reserved_delta=Decimal("26"),
            realized_delta=Decimal("26"),
            sle_id=fact.id,
            fact_ref=fact.recorder_ref,
            fact_line_ref=fact.line_no,
            match_rule="fifo",
            cycle_id=f"historical-replay:g{generation.id}",
            idempotency_key=f"consume-alloc:g{generation.id}:sle{fact.id}:r{consume_reservation.id}",
            event_at=fact.posting_at,
        ),
        models.MrpExecutionAllocation(
            ledger_generation_id=generation.id,
            cycle_id=f"historical-replay:g{generation.id}",
            requirement_id=requirement.id,
            bucket_id=bucket.id,
            fact_type="component_consumption",
            allocation_kind="execution",
            fact_ref=fact.recorder_ref,
            fact_line_ref=fact.line_no,
            fact_date=fact.posting_at,
            allocated_qty=Decimal("26"),
        ),
    ])
    db_session.flush()

    result = validate_generation_build(db_session, generation.id)
    assert result["valid"] is True
    assert result["execution_allocations"] == 1


def test_validation_isolates_bucket_capacity_by_mode_in_single_bucket(db_session):
    generation, requirement = _synthetic(
        db_session,
        "validate-mode-isolated",
        replenishment_method="Производство",
    )
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
        replay_allocated="52",
    )
    make_reservation = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=generation.id,
    ).one()
    make_reservation.reserved_qty = Decimal("26")
    make_reservation.realized_qty = Decimal("26")
    consume_reservation = models.ReservationEntry(
        ledger_generation_id=generation.id,
        item_id=make_reservation.item_id,
        characteristic_ref=make_reservation.characteristic_ref,
        organization_ref=make_reservation.organization_ref,
        planning_stock_pool=make_reservation.planning_stock_pool,
        run_id=make_reservation.run_id,
        freeze_version=1,
        requirement_id=make_reservation.requirement_id,
        priority_period_from=make_reservation.priority_period_from,
        priority_period_to=make_reservation.priority_period_to,
        realization_mode="consume",
        reserved_qty=Decimal("26"),
        realized_qty=Decimal("26"),
        lifecycle_status="active",
    )
    db_session.add(consume_reservation)
    db_session.flush()
    bucket = db_session.query(models.MrpRequirementBucket).filter_by(
        requirement_id=requirement.id,
    ).one()
    bucket.gross_qty = Decimal("26")
    bucket.net_qty = Decimal("26")
    fact = db_session.query(models.StockLedgerEntry).filter_by(
        ingest_batch_id=generation.physical_import_batch_id,
    ).one()
    db_session.add_all([
        models.ReservationEvent(
            ledger_generation_id=generation.id,
            reservation_id=make_reservation.id,
            item_id=make_reservation.item_id,
            characteristic_ref=make_reservation.characteristic_ref,
            organization_ref=make_reservation.organization_ref,
            planning_stock_pool=make_reservation.planning_stock_pool,
            event_kind="realize",
            reserved_delta=Decimal("26"),
            realized_delta=Decimal("26"),
            sle_id=fact.id,
            fact_ref=fact.recorder_ref,
            fact_line_ref=fact.line_no,
            match_rule="fifo",
            cycle_id=f"historical-replay:g{generation.id}",
            idempotency_key=f"mode-make:g{generation.id}:sle{fact.id}:r{make_reservation.id}",
            event_at=fact.posting_at,
        ),
        models.MrpExecutionAllocation(
            ledger_generation_id=generation.id,
            cycle_id=f"historical-replay:g{generation.id}",
            requirement_id=requirement.id,
            bucket_id=bucket.id,
            fact_type="linked_production",
            allocation_kind="execution",
            fact_ref=fact.recorder_ref,
            fact_line_ref=fact.line_no,
            fact_date=fact.posting_at,
            allocated_qty=Decimal("26"),
        ),
        models.ReservationEvent(
            ledger_generation_id=generation.id,
            reservation_id=consume_reservation.id,
            item_id=consume_reservation.item_id,
            characteristic_ref=consume_reservation.characteristic_ref,
            organization_ref=consume_reservation.organization_ref,
            planning_stock_pool=consume_reservation.planning_stock_pool,
            event_kind="realize",
            reserved_delta=Decimal("26"),
            realized_delta=Decimal("26"),
            sle_id=fact.id,
            fact_ref=fact.recorder_ref,
            fact_line_ref=fact.line_no,
            match_rule="fifo",
            cycle_id=f"historical-replay:g{generation.id}",
            idempotency_key=f"mode-consume:g{generation.id}:sle{fact.id}:r{consume_reservation.id}",
            event_at=fact.posting_at,
        ),
        models.MrpExecutionAllocation(
            ledger_generation_id=generation.id,
            cycle_id=f"historical-replay:g{generation.id}",
            requirement_id=requirement.id,
            bucket_id=bucket.id,
            fact_type="component_consumption",
            allocation_kind="execution",
            fact_ref=fact.recorder_ref,
            fact_line_ref=fact.line_no,
            fact_date=fact.posting_at,
            allocated_qty=Decimal("26"),
        ),
    ])
    db_session.flush()

    result = validate_generation_build(db_session, generation.id)
    assert result["valid"] is True
    assert result["execution_allocations"] == 2


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


@pytest.mark.parametrize(
    "ambiguous_pool,ambiguous_identity,pattern",
    [
        ("1", "0", "unresolved planning-stock pools"),
        ("0", "1", "unresolved provenance identities"),
    ],
)
def test_validation_rejects_replay_ambiguity_metrics(
    db_session, ambiguous_pool, ambiguous_identity, pattern
):
    generation, requirement = _synthetic(
        db_session,
        "ambiguous-replay-metrics",
        replenishment_method="Покупка",
    )
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
        replay_allocated="0",
    )
    replay_batch = (
        db_session.query(models.LedgerBuildBatch)
        .filter(
            models.LedgerBuildBatch.ledger_generation_id == generation.id,
            models.LedgerBuildBatch.stage == "reservation_replay",
        )
        .one()
    )
    replay_batch.metrics = {
        "fact_qty": "0",
        "allocated_qty": "0",
        "unplanned_qty": "0",
        "ambiguous_pool_facts": ambiguous_pool,
        "ambiguous_identity_facts": ambiguous_identity,
    }
    db_session.flush()

    with pytest.raises(GenerationValidationError, match=pattern):
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


def test_physical_refresh_validation_gate_rejects_wrong_cutoff(
    db_session,
):
    generation, _requirement = _physical_refresh_gate_ready(
        db_session,
        "physical-refresh-cutoff",
    )
    generation.source_watermarks = _physical_refresh_gate_watermarks(
        generation,
        historical_import_completed_through=generation.cutoff - timedelta(hours=1),
    )
    db_session.commit()

    with pytest.raises(
        GenerationValidationError,
        match="historical_import_completed_through must equal generation cutoff",
    ):
        validate_generation_build(
            db_session,
            generation.id,
            explicit_empty_physical=True,
        )


def test_physical_refresh_validation_rejects_mismatched_convergence_batch(
    db_session,
):
    generation, _requirement = _physical_refresh_gate_ready(
        db_session,
        "physical-refresh-batch",
    )
    generation.source_watermarks = _physical_refresh_gate_watermarks(
        generation,
        convergence_batch_id=generation.physical_import_batch_id + 1,
    )
    db_session.commit()

    with pytest.raises(
        GenerationValidationError,
        match="balance_convergence.physical_import_batch_id",
    ):
        validate_generation_build(
            db_session,
            generation.id,
            explicit_empty_physical=True,
        )


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
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
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
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
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


def test_internal_transfer_does_not_enter_supplier_evidence_gate(
    db_session, monkeypatch
):
    generation, requirement = _synthetic(db_session, "internal-transfer")
    for line_no, qty, warehouse, record_type, movement_kind in (
        ("1", Decimal("-2"), "WH-FROM", "Expense", "transfer_out"),
        ("2", Decimal("2"), "WH-TO", "Receipt", "transfer_in"),
    ):
        db_session.add(models.StockLedgerEntry(
            ingest_batch_id=generation.physical_import_batch_id,
            source_content_hash=f"internal-transfer-{line_no}",
            item_id=requirement.item_id,
            characteristic_ref="",
            organization_ref=DEFAULT_ORGANIZATION_REF1C,
            warehouse_ref1c=warehouse,
            qty=qty,
            posting_at=datetime(2026, 7, 21),
            record_type=record_type,
            movement_kind=movement_kind,
            recorder_type="Document_ПеремещениеЗапасов",
            recorder_ref="transfer-only",
            line_no=line_no,
            ingest_source="test",
        ))
    db_session.commit()

    def extraction_must_not_run(*_args, **_kwargs):
        raise AssertionError("internal transfer entered supplier evidence")

    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle."
        "extract_supplier_document_evidence",
        extraction_must_not_run,
    )

    result = accept_generation_build(
        db_session,
        generation.id,
        replay_from=datetime(2026, 7, 1),
    )

    assert result["status"] == "accepted"
    assert result["supplier_receipt_evidence"] == 0
    assert db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).filter_by(ledger_generation_id=generation.id).count() == 0


def test_direct_supplier_receipt_is_explicitly_unplanned_but_does_not_block(
    db_session, monkeypatch
):
    generation, requirement = _synthetic(db_session, "direct-receipt")
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="direct-receipt",
        item_id=requirement.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
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
            ignored_stock_ledger_entries=(),
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


def test_accepted_generation_can_ignore_customer_sale_expense_rows(db_session, monkeypatch):
    generation, requirement = _synthetic(db_session, "ignored-customer-sale")
    ignored = models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="ignored-customer-sale",
        item_id=requirement.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH",
        qty=Decimal("-2"),
        posting_at=datetime(2026, 7, 21),
        record_type="Expense",
        movement_kind="supplier_return",
        recorder_type="Document_РасходнаяНакладная",
        recorder_ref="ignored-sale",
        line_no="1",
        ingest_source="test",
    )
    db_session.add(ignored)
    db_session.commit()

    ignored_id = int(ignored.id)
    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle."
        "extract_supplier_document_evidence",
        lambda *_args, **_kwargs: SupplierEvidenceExtractionResult(
            evidence=(),
            diagnostics=(),
            ignored_stock_ledger_entries=(
                SupplierReceiptExclusion(
                    stock_ledger_entry_id=ignored_id,
                    operation_key="8d970836-9934-11eb-e39a-fa163e61326a",
                    operation_name="Продажа Покупателю",
                ),
            ),
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
    assert result["supplier_receipt_evidence"] == 1
    assert result["supplier_receipt_ignored_count"] == 1
    assert Decimal(result["supplier_receipt_ignored_qty"]) == Decimal("-2")
    assert Decimal(result["supplier_receipts"]["unplanned_qty"]) == Decimal("0")
    assert Decimal(result["supplier_receipt_unplanned_qty"]) == Decimal("0")
    provenance = db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).filter_by(ledger_generation_id=generation.id).one()
    assert provenance.match_status == "excluded_non_supplier"
    assert provenance.operation_kind == "non_supplier_expense"
    assert provenance.operation_key == "8d970836-9934-11eb-e39a-fa163e61326a"
    assert provenance.match_rule == "supplier-receipt-non-supplier-exclusion"
    assert provenance.reason == "non-supplier expense operation"
    assert result["supplier_receipts"]["status_counts"]["excluded_non_supplier"] == 1


def test_validation_rejects_supplier_receipt_candidate_rows_without_provenance(db_session):
    generation, requirement = _synthetic(db_session, "missing-provenance")
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
        replay_allocated="0",
    )
    _add_matching_reservation_event(db_session, generation, requirement)
    ignored = models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="missing-provenance",
        item_id=requirement.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH",
        qty=Decimal("-2"),
        posting_at=datetime(2026, 7, 21),
        record_type="Expense",
        movement_kind="supplier_return",
        recorder_type="Document_РасходнаяНакладная",
        recorder_ref="missing-provenance",
        line_no="1",
        ingest_source="test",
    )
    db_session.add(ignored)
    db_session.commit()

    with pytest.raises(
        GenerationValidationError,
        match="supplier receipt evidence must cover all supplier candidate rows",
    ):
        validate_generation_build(db_session, generation.id)


def test_accept_rejects_ignored_supplier_entries_if_not_candidate_rows(db_session, monkeypatch):
    generation, requirement = _synthetic(db_session, "supplier-foreign-ignored")
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="ignored-failure",
        item_id=requirement.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH",
        qty=Decimal("2"),
        posting_at=datetime(2026, 7, 21),
        record_type="Receipt",
        movement_kind="supplier_receipt",
        recorder_type="Document_ПриходнаяНакладная",
        recorder_ref="missing-candidate",
        line_no="1",
        ingest_source="test",
    ))
    db_session.flush()

    invalid_ignored_id = -1
    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle."
        "extract_supplier_document_evidence",
        lambda *_args, **_kwargs: SupplierEvidenceExtractionResult(
            evidence=(),
            diagnostics=(),
            ignored_stock_ledger_entries=(
                SupplierReceiptExclusion(
                    stock_ledger_entry_id=invalid_ignored_id,
                    operation_key="8d970836-9934-11eb-e39a-fa163e61326a",
                    operation_name="Продажа Покупателю",
                ),
            ),
            fetched_document_count=1,
        ),
    )

    with pytest.raises(
        GenerationValidationError,
        match="ignored supplier stock entry ids must reference supplier candidates",
    ):
        accept_generation_build(
            db_session,
            generation.id,
            replay_from=datetime(2026, 7, 1),
            odata_client=object(),
        )


def test_supplier_candidates_only_include_default_organization_rows(
    db_session, monkeypatch
):
    generation, requirement = _synthetic(db_session, "supplier-org-filter")
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="supplier-default-org",
        item_id=requirement.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH",
        qty=Decimal("1"),
        posting_at=datetime(2026, 7, 21),
        record_type="Receipt",
        movement_kind="supplier_receipt",
        recorder_type="Document_ПриходнаяНакладная",
        recorder_ref="supplier-default-org",
        line_no="1",
        ingest_source="test",
    ))
    foreign = models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash="supplier-foreign-org",
        item_id=requirement.item_id,
        characteristic_ref="",
        organization_ref="00000000-0000-0000-0000-000000000001",
        warehouse_ref1c="WH",
        qty=Decimal("1"),
        posting_at=datetime(2026, 7, 21),
        record_type="Receipt",
        movement_kind="supplier_receipt",
        recorder_type="Document_ПриходнаяНакладная",
        recorder_ref="supplier-foreign-org",
        line_no="1",
        ingest_source="test",
    )
    db_session.add(foreign)
    db_session.commit()

    default_candidate = db_session.query(models.StockLedgerEntry).filter_by(
        recorder_ref="supplier-default-org",
        ingest_batch_id=generation.physical_import_batch_id,
    ).one()
    evidence = SupplierDocumentEvidence(
        receipt_doc_type="Document_ПриходнаяНакладная",
        receipt_doc_ref=default_candidate.recorder_ref,
        receipt_doc_line_no=default_candidate.line_no,
        operation_key=RECEIPT_OPERATION,
        operation_name="Приобретение у поставщика",
        supplier_order_type="",
        supplier_order_ref="",
        supplier_order_line_no="0",
        item_id=default_candidate.item_id,
        characteristic_ref=default_candidate.characteristic_ref or "",
        warehouse_ref1c=default_candidate.warehouse_ref1c or "",
        signed_qty=Decimal(str(default_candidate.qty)),
    )
    observed: list[int] = []

    def extract(_db, _client, rows):
        observed[:] = [int(row.id) for row in rows if row.id is not None]
        assert len(observed) == 1
        assert int(observed[0]) == int(default_candidate.id)
        return SupplierEvidenceExtractionResult(
            evidence=(evidence,),
            diagnostics=(),
            ignored_stock_ledger_entries=(),
            fetched_document_count=1,
        )

    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle."
        "extract_supplier_document_evidence",
        extract,
    )

    result = accept_generation_build(
        db_session,
        generation.id,
        replay_from=datetime(2026, 7, 1),
        odata_client=object(),
    )

    assert result["status"] == "accepted"
    assert result["supplier_receipt_evidence"] == 1
    assert observed == [int(default_candidate.id)]
    assert db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).filter_by(
        ledger_generation_id=generation.id,
        stock_ledger_entry_id=default_candidate.id,
    ).one()
    assert db_session.query(
        models.StockLedgerSupplierReceiptProvenance
    ).filter_by(
        ledger_generation_id=generation.id,
        stock_ledger_entry_id=foreign.id,
    ).count() == 0
