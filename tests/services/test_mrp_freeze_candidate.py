"""The freeze executor may materialize only unpublished candidate snapshots."""

from datetime import date, datetime, timezone
from hashlib import sha256
import json

import pytest

from app import models
from app.services.mrp_freeze import (
    LedgerPoolUnavailable,
    build_shared_pools,
    freeze_candidate_snapshots,
    refreeze_active_snapshots,
)


def _seal_manifest(target, entries, *, add_plan_ids=(), add_config=None, horizon_days=None, config_version_id=None):
    payload = {
        "version": 1,
        "entries": sorted(entries, key=lambda row: (row["plan_id"], row["action"])),
        "add_request": {
            "plan_ids": sorted(add_plan_ids),
            "horizon_days": horizon_days,
            "config_version_id": config_version_id,
            "config_snapshot": add_config or {},
        },
    }
    target.source_watermarks = {
        **target.source_watermarks,
        "obligation_refresh_manifest": payload,
        "obligation_refresh_manifest_hash": sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _candidate_world(db, quantities=(10, 20)):
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
    for index, qty in enumerate(quantities, start=1):
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
    _seal_manifest(target, [
        {
            "action": "refresh", "plan_id": parent.source_plan_id,
            "parent_run_id": parent.run_id, "candidate_run_id": candidate.run_id,
        }
        for parent, candidate in zip(parents, candidates)
    ])
    return accepted, target, item, parents, candidates, lines


def _add_candidate(db, target, item, *, index=9):
    plan = models.ProductionPlanHeader(
        name="new candidate", period_from=date(2026, 9, index), period_to=date(2026, 9, 30), status="fixed",
    )
    db.add(plan); db.flush()
    line = models.ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id, bucket_date=plan.period_from, qty=3,
    )
    candidate = models.PlanningRun(
        status="BUILDING_SNAPSHOT", ledger_generation_id=target.id, prior_run_id=None,
        source_plan_id=plan.id, period_from=plan.period_from, period_to=plan.period_to,
        horizon_days=45, config_version_id=None, config_snapshot={"first": True},
        started_at=datetime.now(timezone.utc), pinned=False,
    )
    db.add_all([line, candidate]); db.flush()
    return plan, line, candidate


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


def test_candidate_freeze_supports_sealed_add_only(db_session):
    accepted, target, item, _parents, _candidates, _lines = _candidate_world(db_session, ())
    plan, line, candidate = _add_candidate(db_session, target, item)
    _seal_manifest(
        target,
        [{"action": "add", "plan_id": plan.id, "parent_run_id": None, "candidate_run_id": candidate.run_id}],
        add_plan_ids=[plan.id], add_config={"first": True}, horizon_days=45,
    )

    report = freeze_candidate_snapshots(
        db_session, parent_generation_id=accepted.id, target_generation_id=target.id,
        candidate_run_ids=[candidate.run_id],
    )

    assert report["order"] == [candidate.run_id]
    assert db_session.get(models.ProductionPlanLine, line.id).locked_by_run_id is None
    assert db_session.query(models.MrpRequirement).filter_by(run_id=candidate.run_id).count() == 1


def test_candidate_freeze_supports_sealed_refresh_and_add(db_session):
    accepted, target, item, parents, candidates, _lines = _candidate_world(db_session)
    plan, line, added = _add_candidate(db_session, target, item)
    _seal_manifest(
        target,
        [
            {
                "action": "refresh", "plan_id": parent.source_plan_id,
                "parent_run_id": parent.run_id, "candidate_run_id": candidate.run_id,
            }
            for parent, candidate in zip(parents, candidates)
        ] + [{"action": "add", "plan_id": plan.id, "parent_run_id": None, "candidate_run_id": added.run_id}],
        add_plan_ids=[plan.id], add_config={"first": True}, horizon_days=45,
    )

    freeze_candidate_snapshots(
        db_session, parent_generation_id=accepted.id, target_generation_id=target.id,
        candidate_run_ids=[row.run_id for row in candidates] + [added.run_id],
    )

    assert db_session.get(models.ProductionPlanLine, line.id).locked_by_run_id is None
    assert db_session.query(models.MrpRequirement).filter_by(run_id=added.run_id).count() == 1


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
