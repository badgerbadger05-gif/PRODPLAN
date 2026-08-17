from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256

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
    RESERVATION_CONSUMPTION_ALGORITHM_VERSION,
    validate_generation_build,
    REPLAY_ALGORITHM_VERSION,
)
from app.services.item_ledger.future_supply_capture import (
    FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION,
)
from app.services.item_ledger.reservation_consumption_persistence import (
    materialize_reservation_consumption_allocations,
)
from app.services.purchase_control_snapshot import (
    CONSUMER as PURCHASE_JOURNAL_CONSUMER,
    SNAPSHOT_KEY as PURCHASE_JOURNAL_SNAPSHOT_KEY,
)
from app.services.item_ledger.assembly_output_persistence import (
    materialize_assembly_output_allocations,
)
from app.services.production_control_journal_snapshot import (
    CONSUMER as PRODUCTION_JOURNAL_CONSUMER,
    SNAPSHOT_KEY as PRODUCTION_JOURNAL_SNAPSHOT_KEY,
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
    if not db.query(models.StockWarehouse).count():
        db.add(models.StockWarehouse(
            warehouse_ref1c="WH",
            warehouse_name="Synthetic planning warehouse",
            is_selected=True,
            is_finished_goods=False,
        ))
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
    db.add(models.ProductionMaterialCustodyProjectionManifest(
        ledger_generation_id=generation.id,
        cutoff=cutoff,
        status="complete",
        is_baseline=True,
        source_event_high_watermark_id=0,
        observed_at=cutoff,
        built_at=cutoff,
    ))
    return generation


def _add_checkpoint_stages(
    db_session,
    generation: models.LedgerGeneration,
    *,
    add_execution_allocation: bool = True,
) -> None:
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
    materialize_assembly_output_allocations(db_session, int(generation.id))
    if add_execution_allocation:
        _add_execution_allocation_batch(db_session, generation)


def _add_execution_allocation_batch(
    db_session,
    generation: models.LedgerGeneration,
    *,
    algorithm_version: str = RESERVATION_CONSUMPTION_ALGORITHM_VERSION,
):
    if not db_session.query(models.StockWarehouse).filter(
        models.StockWarehouse.is_selected.is_(True),
        models.StockWarehouse.is_finished_goods.is_(False),
    ).count():
        db_session.add(models.StockWarehouse(
            warehouse_ref1c="WH-PLAN",
            warehouse_name="Planning warehouse",
            is_selected=True,
            is_finished_goods=False,
        ))
        db_session.flush()
    for reservation in db_session.query(models.ReservationEntry).filter(
        models.ReservationEntry.ledger_generation_id == generation.id,
        models.ReservationEntry.lifecycle_status == "active",
    ).all():
        baseline_query = db_session.query(models.MrpFreezeBaseline).filter_by(
            run_id=int(reservation.run_id),
            freeze_version=int(reservation.freeze_version),
            item_id=int(reservation.item_id),
            characteristic_ref=str(reservation.characteristic_ref or ""),
            organization_ref=str(reservation.organization_ref or ""),
            planning_stock_pool=str(reservation.planning_stock_pool or ""),
        )
        if baseline_query.count() == 0:
            db_session.add(models.MrpFreezeBaseline(
                run_id=int(reservation.run_id),
                freeze_version=int(reservation.freeze_version),
                item_id=int(reservation.item_id),
                characteristic_ref=str(reservation.characteristic_ref or ""),
                organization_ref=str(reservation.organization_ref or ""),
                planning_stock_pool=str(reservation.planning_stock_pool or ""),
                baseline_at=generation.cutoff,
                physical_import_batch_id=int(generation.physical_import_batch_id),
                stock_qty=Decimal("0"),
                produced_total=Decimal("0"),
                received_total=Decimal("0"),
            ))
    db_session.flush()
    batch = models.LedgerBuildBatch(
        ledger_generation_id=generation.id,
        stage="execution_allocation",
        batch_key=f"execution-allocation-{generation.id}",
        status="building",
        algorithm_version=algorithm_version,
        metrics={},
    )
    db_session.add(batch)
    db_session.flush()
    metrics = materialize_reservation_consumption_allocations(
        db_session,
        int(generation.id),
        int(batch.id),
    )
    batch.status = "completed"
    batch.metrics = dict(metrics)
    batch.completed_at = datetime(2026, 7, 31, 23, 58, tzinfo=timezone.utc)
    db_session.flush()
    return batch


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
    if not db.query(models.StockWarehouse).filter_by(warehouse_ref1c="WH").count():
        db.add(models.StockWarehouse(
            warehouse_ref1c="WH",
            warehouse_name="Outside planning contour",
            is_selected=True,
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
    db.add(models.MrpFreezeBaseline(
        run_id=run.run_id,
        freeze_version=1,
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref="",
        planning_stock_pool="default",
        baseline_at=plan.period_from,
        physical_import_batch_id=generation.physical_import_batch_id,
        stock_qty=Decimal("0"),
        produced_total=Decimal("0"),
        received_total=Decimal("0"),
    ))
    db.add(models.StockLedgerEntry(
        ingest_batch_id=generation.physical_import_batch_id,
        source_content_hash=sha256(f"hash-{key}".encode()).hexdigest(),
        item_id=item.item_id,
        characteristic_ref="",
        organization_ref=DEFAULT_ORGANIZATION_REF1C,
        warehouse_ref1c="WH-OUT",
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
    add_execution_allocation: bool = True,
):
    _add_checkpoint_stages(
        db_session,
        generation,
        add_execution_allocation=add_execution_allocation,
    )
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
    materialize_assembly_output_allocations(db_session, int(generation.id))
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
                "make" if requirement.item.replenishment_method == "Производство" else "buy"
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


def test_acceptance_marks_journal_snapshots_as_accepted_truth(db_session):
    generation, _requirement = _synthetic(db_session, "journal-truth")

    accept_generation_build(
        db_session,
        generation.id,
        replay_from=datetime(2026, 7, 1),
    )

    db_session.refresh(generation)
    assert generation.status == "accepted"
    assert generation.accepted_at is not None

    purchase_snapshot = (
        db_session.query(models.PlanningReadSnapshot)
        .filter_by(
            consumer=PURCHASE_JOURNAL_CONSUMER,
            snapshot_key=PURCHASE_JOURNAL_SNAPSHOT_KEY,
            ledger_generation_id=generation.id,
            truth_status="accepted",
        )
        .one()
    )
    production_snapshot = (
        db_session.query(models.PlanningReadSnapshot)
        .filter_by(
            consumer=PRODUCTION_JOURNAL_CONSUMER,
            snapshot_key=PRODUCTION_JOURNAL_SNAPSHOT_KEY,
            ledger_generation_id=generation.id,
            truth_status="accepted",
        )
        .one()
    )
    assert purchase_snapshot.published_at == generation.accepted_at
    assert production_snapshot.published_at == generation.accepted_at


def test_acceptance_fails_closed_when_claimed_journal_candidate_is_missing(
    db_session, monkeypatch
):
    generation, _requirement = _synthetic(db_session, "journal-missing")

    def no_purchase_candidate(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle.build_purchase_journal_candidate",
        no_purchase_candidate,
    )
    with pytest.raises(
        GenerationValidationError,
        match="no journal candidate to publish",
    ):
        accept_generation_build(
            db_session,
            generation.id,
            replay_from=datetime(2026, 7, 1),
        )

    db_session.expire_all()
    assert db_session.get(models.LedgerGeneration, generation.id).status == "building"
    assert (
        db_session.query(models.PlanningTruthState)
        .filter_by(id=1)
        .count()
        == 0
    )
    assert (
        db_session.query(models.PlanningReadSnapshot)
        .filter_by(
            ledger_generation_id=generation.id,
            consumer=PURCHASE_JOURNAL_CONSUMER,
            truth_status="accepted",
            snapshot_key=PURCHASE_JOURNAL_SNAPSHOT_KEY,
        )
        .count()
        == 0
    )


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
        match="planning read snapshot build failed",
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


def test_assembly_queue_snapshot_failure_does_not_switch_planning_truth_pointer(
    db_session, monkeypatch
):
    anchor_batch = models.PhysicalImportBatch(
        batch_key="assembly-anchor-physical",
        status="completed",
        cutoff=datetime(2026, 7, 31, 23, 59),
        source_watermarks={},
    )
    anchor_generation = models.LedgerGeneration(
        generation_key="assembly-anchor-generation",
        status="accepted",
        cutoff=datetime(2026, 7, 31, 23, 59),
        accepted_at=datetime(2026, 7, 31, 23, 59),
        source_watermarks={},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "planning_snapshots": True,
            "assembly_queue": True,
        },
        physical_import_batch=anchor_batch,
        algorithm_version="tests/assembly-anchor",
    )
    db_session.add_all([anchor_batch, anchor_generation])
    db_session.flush()
    db_session.add(
        models.PlanningTruthState(id=1, current_generation_id=anchor_generation.id)
    )
    db_session.flush()
    previous_generation_id = int(anchor_generation.id)

    target_generation, _target_requirement = _synthetic(
        db_session,
        "assembly-queue-snapshot-rollback",
    )

    def fail_assembly_queue_snapshot(*_args, **_kwargs):
        raise ValueError("assembly queue snapshot requires a BUILDING generation")

    monkeypatch.setattr(
        "app.services.item_ledger.generation_lifecycle.build_assembly_queue_snapshot",
        fail_assembly_queue_snapshot,
    )

    with pytest.raises(
        GenerationValidationError,
        match="planning read snapshot build failed",
    ):
        accept_generation_build(
            db_session,
            target_generation.id,
            replay_from=datetime(2026, 7, 1),
        )

    db_session.expire_all()
    pointer = db_session.get(models.PlanningTruthState, 1)
    assert pointer is not None
    assert pointer.current_generation_id == previous_generation_id
    assert db_session.get(models.LedgerGeneration, target_generation.id).status == "building"
    assert (
        db_session.query(models.PlanningReadSnapshot)
        .filter_by(ledger_generation_id=target_generation.id)
        .count()
        == 0
    )


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
    db_session.flush()

    result = validate_generation_build(db_session, generation.id)

    assert result["allocated_qty"] == "5.000"
    assert result["execution_allocations"]["allocations"] == 0


def test_validation_rejects_generation_without_execution_allocation_batch(db_session):
    generation, requirement = _synthetic(db_session, "missing-execution-batch")
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
        add_execution_allocation=False,
    )

    with pytest.raises(
        GenerationValidationError,
        match="execution_allocation",
    ):
        validate_generation_build(db_session, generation.id)


def test_validation_rejects_wrong_execution_allocation_algorithm_version(db_session):
    generation, requirement = _synthetic(db_session, "wrong-execution-version")
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
        add_execution_allocation=False,
    )
    _add_execution_allocation_batch(
        db_session,
        generation,
        algorithm_version="wrong/version",
    )

    with pytest.raises(
        GenerationValidationError,
        match="unexpected reservation consumption allocation algorithm",
    ):
        validate_generation_build(db_session, generation.id)


def test_validation_allows_zero_execution_allocation_rows_with_proven_batch(db_session):
    generation, requirement = _synthetic(db_session, "zero-execution-rows")
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
        add_execution_allocation=False,
    )
    _add_matching_reservation_event(
        db_session,
        generation,
        requirement,
    )
    _add_execution_allocation_batch(
        db_session,
        generation,
    )

    result = validate_generation_build(db_session, generation.id)

    assert result["valid"] is True
    assert result["execution_allocations"] == {
        "facts": 0,
        "allocations": 0,
        "fact_qty": "0",
        "allocated_qty": "0",
        "surplus_qty": "0",
        "allocation_checksum": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
    }


def test_validation_rejects_execution_metric_row_drift(db_session):
    generation, requirement = _synthetic(db_session, "execution-metric-drift")
    _configure_obligation_checkpoint(
        db_session,
        generation,
        requirement,
        allow_unphased=False,
        add_execution_allocation=False,
    )
    _add_matching_reservation_event(
        db_session,
        generation,
        requirement,
    )
    batch = _add_execution_allocation_batch(
        db_session,
        generation,
    )
    batch.metrics["allocations"] = "1"
    db_session.flush()

    with pytest.raises(
        GenerationValidationError,
        match="execution allocation metric does not match allocation row count",
    ):
        validate_generation_build(db_session, generation.id)


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
    reservation.replenishment_required_qty = Decimal("5")
    reservation.realized_qty = Decimal("5")
    reservation.replenishment_received_qty = Decimal("5")
    _add_matching_reservation_event(db_session, generation, requirement)
    event = db_session.query(models.ReservationEvent).filter_by(
        ledger_generation_id=generation.id
    ).one()
    event.cycle_id = f"historical-supplier:g{generation.id}:accept"
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
    db_session.commit()

    result = validate_generation_build(db_session, generation.id)
    assert result["valid"] is True
    assert result["execution_allocations"]["allocations"] == 0


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
    db_session.commit()

    result = validate_generation_build(db_session, generation.id)

    assert result["valid"] is True
    assert result["execution_allocations"]["allocations"] == 0


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
    db_session.commit()

    result = validate_generation_build(db_session, generation.id)

    assert result["valid"] is True
    assert result["execution_allocations"]["allocations"] == 0


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
    db_session.commit()

    result = validate_generation_build(db_session, generation.id)
    assert result["valid"] is True
    assert result["execution_allocations"]["allocations"] == 0


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
    db_session.flush()

    result = validate_generation_build(db_session, generation.id)
    assert result["valid"] is True
    assert result["execution_allocations"]["allocations"] == 0


def test_validation_rejects_replay_ambiguous_pool_metrics(db_session):
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
        "ambiguous_pool_facts": "1",
        "ambiguous_identity_facts": "0",
    }
    db_session.flush()

    with pytest.raises(GenerationValidationError, match="unresolved planning-stock pools"):
        validate_generation_build(db_session, generation.id)


def test_validation_allows_replay_ambiguous_identity_metrics(db_session):
    generation, _requirement = _synthetic(
        db_session,
        "ambiguous-identity-replay-metrics",
        replenishment_method="Покупка",
    )
    _configure_obligation_checkpoint(
        db_session,
        generation,
        _requirement,
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
        "ambiguous_pool_facts": "0",
        "ambiguous_identity_facts": "1",
    }
    db_session.flush()

    result = validate_generation_build(db_session, generation.id)

    assert result["valid"] is True


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


def test_accept_generation_with_no_future_supply_parent_creates_zero_capture(db_session):
    generation, _requirement = _synthetic(db_session, "zero-future-supply")
    result = accept_generation_build(
        db_session,
        generation.id,
        replay_from=datetime(2026, 7, 1),
    )

    assert result["capabilities"]["future_supply"] is True
    assert result["future_supply"]["rows"] == 0
    batch = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=int(generation.id),
        stage="future_supply_capture",
    ).one()
    assert batch.status == "completed"
    assert batch.algorithm_version == FUTURE_SUPPLY_CAPTURE_ALGORITHM_VERSION
    assert batch.metrics["rows"] == 0
    db_session.refresh(generation)
    assert generation.capabilities["future_supply"] is True


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
    assert result["supplier_receipts"]["surplus_qty"] == "3.000"
    assert result["supplier_receipt_surplus_qty"] == "3.000"
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
    assert Decimal(result["supplier_receipts"]["surplus_qty"]) == Decimal("0")
    assert Decimal(result["supplier_receipt_surplus_qty"]) == Decimal("0")
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


def _mrp_gate_lineage(db, *, key):
    """An accepted anchor carrying one live run, plus a fact-only child fork."""
    anchor = _generation(db, f"{key}-anchor")
    anchor.status = "accepted"
    anchor.accepted_at = anchor.cutoff
    plan = models.ProductionPlanHeader(
        name=f"Gate plan {key}",
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="fixed",
    )
    db.add(plan)
    db.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        config_snapshot={},
        ledger_generation_id=int(anchor.id),
        ledger_cutoff=anchor.cutoff,
        active_freeze_version=1,
        source_plan_id=int(plan.id),
    )
    db.add(run)
    db.flush()
    child = _generation(
        db,
        f"{key}-child",
        source_watermarks={
            "generation_kind": "physical_refresh",
            "parent_generation_id": int(anchor.id),
        },
    )
    return anchor, run, child


def test_mrp_quantity_gate_still_sees_a_run_inherited_by_a_physical_refresh(db_session):
    """The gate must count the live scope, not runs re-anchored to this fork.

    Scoping it to ``ledger_generation_id == generation.id`` matched nothing after
    the first physical refresh, so the gate silently validated zero requirements
    on every hourly publish.
    """
    from app.services.item_ledger.generation_lifecycle import _mrp_quantity_checkpoint

    _anchor, run, child = _mrp_gate_lineage(db_session, key="mrp-gate-ok")
    item = models.Item(item_code="GATE-OK", item_name="Gate ok")
    db_session.add(item)
    db_session.flush()
    db_session.add(models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        freeze_version=1,
        total_required_qty=Decimal("10"),
        net_required_qty=Decimal("4"),
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="open",
    ))
    db_session.flush()

    assert _mrp_quantity_checkpoint(db_session, child) == 1


def test_mrp_quantity_gate_rejects_an_inherited_run_with_net_above_gross(db_session):
    from app.services.item_ledger.generation_lifecycle import _mrp_quantity_checkpoint

    _anchor, run, child = _mrp_gate_lineage(db_session, key="mrp-gate-bad")
    item = models.Item(item_code="GATE-BAD", item_name="Gate bad")
    db_session.add(item)
    db_session.flush()
    db_session.add(models.MrpRequirement(
        run_id=run.run_id,
        item_id=item.item_id,
        freeze_version=1,
        total_required_qty=Decimal("2"),
        net_required_qty=Decimal("5"),
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        status="open",
    ))
    db_session.flush()

    with pytest.raises(GenerationValidationError, match="0 <= net <= gross"):
        _mrp_quantity_checkpoint(db_session, child)
