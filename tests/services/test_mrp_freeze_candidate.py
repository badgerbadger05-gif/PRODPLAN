"""The freeze executor may materialize only unpublished candidate snapshots."""

from datetime import date, datetime, timezone

import pytest

from app import models
from app.services.mrp_freeze import (
    LedgerPoolUnavailable,
    build_shared_pools,
    freeze_candidate_snapshots,
    refreeze_active_snapshots,
)


def _candidate_world(db):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="candidate-freeze-physical", status="completed", cutoff=cutoff,
        source_watermarks={}, completed_at=cutoff,
    )
    accepted = models.LedgerGeneration(
        generation_key="candidate-freeze-accepted", status="accepted", cutoff=cutoff,
        source_watermarks={}, capabilities={}, physical_import_batch=physical,
        algorithm_version="tests", accepted_at=cutoff,
    )
    target = models.LedgerGeneration(
        generation_key="candidate-freeze-target", status="building", cutoff=cutoff,
        source_watermarks={}, capabilities={}, physical_import_batch=physical,
        algorithm_version="tests",
    )
    db.add_all([accepted, target]); db.flush()
    target.source_watermarks = {"generation_kind": "obligation_refresh", "parent_generation_id": accepted.id}
    db.add(models.PlanningTruthState(id=1, current_generation_id=accepted.id))

    item = models.Item(item_code="CAND-FREEZE", item_name="candidate purchased", unit="шт", replenishment_method="Покупка")
    db.add(item); db.flush()
    db.add(models.StockBin(
        ledger_generation_id=target.id, item_id=item.item_id,
        characteristic_ref="", organization_ref="", warehouse_ref1c="", on_hand=15,
    ))
    # The future-supply capture precedes the executor and belongs to the exact
    # candidate generation.
    capture = models.LedgerBuildBatch(
        ledger_generation_id=target.id, stage="snapshot_build", batch_key="candidate-future",
        status="completed", algorithm_version="tests", metrics={},
    )
    db.add(capture); db.flush()
    def future(ref, qty):
        return models.LedgerFutureSupply(
            ledger_generation_id=target.id, supply_kind="supplier_order", item_id=item.item_id,
            characteristic_ref="", organization_ref="", planning_stock_pool="default", destination_warehouse_ref1c="WH-PLAN",
            source_ref=ref, source_line_ref="1", ordered_qty_at_cutoff=qty, realized_qty_at_cutoff=0,
            open_qty_at_cutoff=qty, eta_date=date(2026, 8, 1), source_state_key="open",
            capture_cutoff=cutoff, source_content_hash=(ref * 64)[:64], capture_batch_id=capture.id,
            evidence_status="exact",
        )
    db.add(future("t", 2))

    parents, candidates, lines = [], [], []
    for index, qty in enumerate((10, 20), start=1):
        plan = models.ProductionPlanHeader(
            name=f"candidate {index}", period_from=date(2026, 8, index), period_to=date(2026, 8, 31), status="fixed",
        )
        db.add(plan); db.flush()
        line = models.ProductionPlanLine(plan_id=plan.id, item_id=item.item_id, bucket_date=date(2026, 8, index), qty=qty)
        parent = models.PlanningRun(
            status="FIXED_SNAPSHOT", ledger_generation_id=accepted.id, source_plan_id=plan.id,
            period_from=plan.period_from, period_to=plan.period_to, config_snapshot={},
            started_at=cutoff, fixed_at=cutoff, finished_at=cutoff, pinned=True, active_freeze_version=7,
        )
        db.add_all([line, parent]); db.flush()
        candidate = models.PlanningRun(
            status="BUILDING_SNAPSHOT", ledger_generation_id=target.id, prior_run_id=parent.run_id,
            source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
            config_snapshot={}, started_at=cutoff, pinned=False,
        )
        db.add(candidate); db.flush()
        parents.append(parent); candidates.append(candidate); lines.append(line)
    return accepted, target, item, parents, candidates, lines


def test_candidate_freeze_preserves_published_state_and_consumes_exact_pool_once(db_session, monkeypatch):
    accepted, target, item, parents, candidates, lines = _candidate_world(db_session)
    parent_before = [(row.run_id, row.status, row.ledger_generation_id, row.active_freeze_version) for row in parents]
    lock_before = [row.locked_by_run_id for row in lines]
    calls = {"commit": 0, "rollback": 0}
    monkeypatch.setattr(db_session, "commit", lambda: calls.__setitem__("commit", calls["commit"] + 1))
    monkeypatch.setattr(db_session, "rollback", lambda: calls.__setitem__("rollback", calls["rollback"] + 1))

    report = freeze_candidate_snapshots(
        db_session, parent_generation_id=accepted.id, target_generation_id=target.id,
        candidate_run_ids=[row.run_id for row in candidates],
    )
    db_session.flush()

    assert report["order"] == [row.run_id for row in candidates]
    assert calls == {"commit": 0, "rollback": 0}
    assert [(row.run_id, row.status, row.ledger_generation_id, row.active_freeze_version) for row in parents] == parent_before
    assert [db_session.get(models.ProductionPlanLine, row.id).locked_by_run_id for row in lines] == lock_before
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id
    assert all(row.run_id != parent.run_id for row, parent in zip(candidates, parents))
    assert db_session.query(models.MrpRequirement).filter(models.MrpRequirement.run_id.in_([p.run_id for p in parents])).count() == 0

    # 15 physical + 2 exact target supplier supply cover the queue once:
    # first candidate 10, second candidate gets only 7 and must buy 13.
    requirements = [
        db_session.query(models.MrpRequirement).filter_by(run_id=row.run_id, item_id=item.item_id).one()
        for row in candidates
    ]
    assert [float(row.net_required_qty) for row in requirements] == pytest.approx([0, 15])
    purchases = [
        sum(float(row.qty) for row in db_session.query(models.PlannedPurchase).filter_by(run_id=candidate.run_id).all())
        for candidate in candidates
    ]
    assert purchases == pytest.approx([0, 13])
    assert all(row.ledger_generation_id == target.id for row in db_session.query(models.PlannedPurchase).all())


def test_retired_refreeze_entrypoint_is_not_callable(db_session):
    with pytest.raises(LedgerPoolUnavailable, match="retired"):
        refreeze_active_snapshots(db_session)


def test_candidate_freeze_rejects_non_building_target(db_session):
    accepted, target, _item, _parents, candidates, _lines = _candidate_world(db_session)
    target.status = "accepted"
    with pytest.raises(LedgerPoolUnavailable, match="BUILDING"):
        freeze_candidate_snapshots(
            db_session, parent_generation_id=accepted.id, target_generation_id=target.id,
            candidate_run_ids=[row.run_id for row in candidates],
        )


def test_exact_future_supply_without_pool_or_destination_is_rejected(db_session):
    _accepted, target, item, _parents, _candidates, _lines = _candidate_world(db_session)
    capture = db_session.query(models.LedgerBuildBatch).filter_by(
        ledger_generation_id=target.id, batch_key="candidate-future"
    ).one()
    db_session.add(models.LedgerFutureSupply(
        ledger_generation_id=target.id, supply_kind="supplier_order", item_id=item.item_id,
        characteristic_ref="", organization_ref="", planning_stock_pool="", destination_warehouse_ref1c="",
        source_ref="corrupt", source_line_ref="2", ordered_qty_at_cutoff=1, realized_qty_at_cutoff=0,
        open_qty_at_cutoff=1, eta_date=date(2026, 8, 1), source_state_key="open",
        capture_cutoff=target.cutoff, source_content_hash="c" * 64, capture_batch_id=capture.id,
        evidence_status="exact",
    ))
    db_session.flush()
    with pytest.raises(LedgerPoolUnavailable, match="pool or destination"):
        build_shared_pools(
            db_session, [], ledger_generation_id=target.id, relevant_item_ids=[item.item_id]
        )
