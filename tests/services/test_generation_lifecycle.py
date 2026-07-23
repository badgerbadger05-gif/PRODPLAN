from datetime import date, datetime
from decimal import Decimal

import pytest

from app import models
from app.services.item_ledger.generation_lifecycle import (
    GenerationValidationError,
    accept_generation_build,
    validate_generation_build,
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
