"""End-to-end contract tests for the caller-owned refresh orchestrator."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app import models
from app.services import obligation_refresh_orchestrator as workflow
from app.services.mrp_result_snapshot import read_mrp_result_manifest


def _world(db, *, with_parent=True, qty=5, replenishment_method="Покупка", period_from=date(2026, 8, 1)):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="orchestrator-physical", status="completed", cutoff=cutoff,
        source_watermarks={}, completed_at=cutoff,
    )
    accepted = models.LedgerGeneration(
        generation_key="orchestrator-accepted", status="accepted", cutoff=cutoff,
        source_watermarks={}, capabilities={"physical_ledger": True},
        physical_import_batch=physical, algorithm_version="test", accepted_at=cutoff,
    )
    item = models.Item(item_code="ORCH-PURCHASE", item_name="orchestrator purchase",
                       replenishment_method=replenishment_method)
    db.add_all([physical, accepted, item]); db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=accepted.id))
    plan = models.ProductionPlanHeader(name="orchestrator plan", status="fixed",
        period_from=period_from, period_to=date(2026, 8, 31))
    db.add(plan); db.flush()
    line = models.ProductionPlanLine(plan_id=plan.id, item_id=item.item_id,
        bucket_date=period_from, qty=Decimal(str(qty)))
    db.add(line); db.flush()
    parent = None
    if with_parent:
        parent = models.PlanningRun(status="FIXED_SNAPSHOT", ledger_generation_id=accepted.id,
            source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
            config_snapshot={}, started_at=cutoff, fixed_at=cutoff, finished_at=cutoff,
            pinned=True, active_freeze_version=1)
        db.add(parent); db.flush()
    db.commit()
    return accepted, plan, line, item, parent, cutoff


def _run(db, parent, key, *, add=(), config=None):
    return workflow.run_obligation_refresh(
        db, parent_generation_id=parent.id, generation_key=key, add_plan_ids=add,
        started_by="test", horizon_days=30, config_version_id=None,
        config_snapshot=config or {}, planning_pool_by_warehouse={},
        accepted_at=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )


def test_add_only_builds_real_checkpoints_and_promotes_persisted_read_snapshot(db_session):
    accepted, plan, _line, _item, _old, _cutoff = _world(db_session, with_parent=False)
    result = _run(db_session, accepted, "orch-add", add=[plan.id], config={"first": True})
    target = db_session.get(models.LedgerGeneration, result.target_generation_id)
    candidate = db_session.query(models.PlanningRun).filter_by(
        ledger_generation_id=target.id, status="FIXED_SNAPSHOT").one()
    batches = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=target.id).all()
    by_stage = {row.stage: row for row in batches}

    assert result.published is True
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == target.id
    assert target.capabilities == {
        "physical_ledger": True, "reservation_replay": True,
        "execution_allocations": True, "planning_snapshots": True,
    }
    assert {"physical_import", "reservation_materialize", "reservation_replay", "snapshot_build"} == set(by_stage)
    assert all(row.status == "completed" for row in by_stage.values())
    assert by_stage["snapshot_build"].metrics["future_supply_captured"] is True
    snapshot_id = by_stage["snapshot_build"].metrics["candidate_read_snapshot_ids"][str(candidate.run_id)]
    assert db_session.get(models.PlanningReadSnapshot, snapshot_id).truth_status == "accepted"
    # This public read function consumes the stored snapshot; it does not run MRP.
    assert read_mrp_result_manifest(db_session, candidate.run_id)["run_id"] == candidate.run_id


def test_refresh_plus_add_publishes_both_and_supersedes_only_parent(db_session):
    accepted, plan, _line, _item, old, _cutoff = _world(db_session)
    extra = models.ProductionPlanHeader(name="new", status="fixed",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30))
    db_session.add(extra); db_session.flush()
    item = db_session.query(models.Item).filter_by(item_code="ORCH-PURCHASE").one()
    db_session.add(models.ProductionPlanLine(plan_id=extra.id, item_id=item.item_id,
        bucket_date=extra.period_from, qty=Decimal("2")))
    db_session.commit()

    result = _run(db_session, accepted, "orch-refresh-add", add=[extra.id], config={"add": 1})
    rows = db_session.query(models.PlanningRun).filter(
        models.PlanningRun.run_id.in_(result.candidate_run_ids)).all()
    assert old.status == "SUPERSEDED"
    assert {row.source_plan_id for row in rows} == {plan.id, extra.id}
    assert all(row.status == "FIXED_SNAPSHOT" and row.pinned for row in rows)


def test_candidate_replay_applies_fifo_fact_to_candidate_reservation(db_session):
    accepted, _plan, _line, item, _old, cutoff = _world(
        db_session, qty=5, replenishment_method="Производство", period_from=date(2026, 7, 1)
    )
    physical = accepted.physical_import_batch
    db_session.add(models.StockLedgerEntry(
        ingest_batch_id=physical.id, source_content_hash="f" * 64, item_id=item.item_id,
        characteristic_ref="", organization_ref="", warehouse_ref1c="", qty=Decimal("5"),
        posting_at=datetime(2026, 7, 2, tzinfo=timezone.utc), record_type="receipt",
        movement_kind="assembly_in", recorder_type="Production", recorder_ref="FIFO", line_no="1",
    ))
    db_session.commit()
    result = _run(db_session, accepted, "orch-fifo")
    entry = db_session.query(models.ReservationEntry).filter_by(
        ledger_generation_id=result.target_generation_id).one()
    assert Decimal(str(entry.realized_qty)) == Decimal("5")
    assert db_session.query(models.MrpExecutionAllocation).filter_by(
        ledger_generation_id=result.target_generation_id).count() == 1


def test_failure_after_freeze_is_reversible_by_outer_transaction(db_session, monkeypatch):
    accepted, _plan, line, _item, old, _cutoff = _world(db_session)
    def fail(*_args, **_kwargs):
        raise RuntimeError("injected after freeze")
    monkeypatch.setattr(workflow, "replay_candidate_realizations", fail)
    outer = db_session.begin()
    with pytest.raises(RuntimeError, match="injected"):
        _run(db_session, accepted, "orch-rollback")
    outer.rollback()
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id
    assert old.status == "FIXED_SNAPSHOT"
    assert line.locked_by_run_id is None
    assert db_session.query(models.LedgerGeneration).filter_by(generation_key="orch-rollback").count() == 0


def test_committed_exact_retry_is_publisher_noop_and_changed_request_is_rejected(db_session):
    accepted, plan, _line, _item, _old, _cutoff = _world(db_session, with_parent=False)
    first = _run(db_session, accepted, "orch-retry", add=[plan.id], config={"v": 1})
    db_session.commit()
    second = _run(db_session, accepted, "orch-retry", add=[plan.id], config={"v": 1})
    assert second.target_generation_id == first.target_generation_id
    assert second.published is False
    with pytest.raises(workflow.ObligationRefreshOrchestratorError, match="conflicting retry"):
        _run(db_session, accepted, "orch-retry", add=[plan.id], config={"v": 2})


def test_stale_parent_is_rejected_before_published_retry(db_session):
    accepted, plan, _line, _item, _old, _cutoff = _world(db_session, with_parent=False)
    _run(db_session, accepted, "orch-stale", add=[plan.id])
    # Simulate another accepted generation winning the pointer after publication.
    current = db_session.get(models.PlanningTruthState, 1)
    current.current_generation_id = accepted.id
    db_session.flush()
    with pytest.raises(workflow.ObligationRefreshOrchestratorError, match="published retry requires target"):
        _run(db_session, accepted, "orch-stale", add=[plan.id])
