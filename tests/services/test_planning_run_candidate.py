from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app import models
from app.services.planning_run_candidate import (
    PlanningRunCandidateError,
    create_added_candidate_run,
)


def _generation(db, *, key: str, status: str, cutoff, watermarks=None):
    physical = models.PhysicalImportBatch(
        batch_key=f"physical:{key}", status="completed", cutoff=cutoff,
        source_watermarks={}, completed_at=cutoff,
    )
    generation = models.LedgerGeneration(
        generation_key=key, status=status, cutoff=cutoff,
        source_watermarks=watermarks or {}, capabilities={},
        physical_import_batch=physical, algorithm_version="tests/1",
        accepted_at=cutoff if status == "accepted" else None,
    )
    db.add(generation)
    db.flush()
    return generation


def _parent_and_target(db):
    cutoff = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    accepted = _generation(db, key="accepted", status="accepted", cutoff=cutoff)
    db.add(models.PlanningTruthState(id=1, current_generation_id=accepted.id))
    db.flush()
    plan = models.ProductionPlanHeader(
        name="candidate source", period_from=date(2026, 7, 1), period_to=date(2026, 7, 31),
    )
    db.add(plan)
    db.flush()
    parent = models.PlanningRun(
        status="FIXED_SNAPSHOT", ledger_generation_id=accepted.id, source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to, horizon_days=90,
        config_version_id=None, config_snapshot={"nested": {"pool": ["A"]}},
        started_by="original", started_at=cutoff, fixed_at=cutoff, finished_at=cutoff,
        warnings={"old": True}, kpi={"old": 1}, active_freeze_version=5, pinned=True,
    )
    db.add(parent)
    db.flush()
    target = _generation(
        db, key="refresh", status="building", cutoff=cutoff,
        watermarks={"generation_kind": "obligation_refresh", "parent_generation_id": accepted.id},
    )
    target.physical_import_batch_id = accepted.physical_import_batch_id
    db.commit()
    return parent, accepted, target


def test_create_added_candidate_for_first_fixed_plan_copies_sealed_config(db_session):
    _parent, accepted, target = _parent_and_target(db_session)
    # The helper fixture creates a refresh parent for a different source plan;
    # this fixed plan has no run on the accepted generation yet.
    plan = models.ProductionPlanHeader(
        name="new source", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
        status="fixed",
    )
    db_session.add(plan)
    db_session.commit()
    supplied_config = {"sealed": {"pool": ["A"]}}

    candidate = create_added_candidate_run(
        db_session, plan.id, target.id, "add-worker",
        horizon_days=45, config_version_id=None, config_snapshot=supplied_config,
    )

    assert candidate.status == "BUILDING_SNAPSHOT"
    assert candidate.prior_run_id is None
    assert candidate.ledger_generation_id == target.id
    assert candidate.source_plan_id == plan.id
    assert (candidate.period_from, candidate.period_to) == (plan.period_from, plan.period_to)
    assert candidate.horizon_days == 45
    assert candidate.config_snapshot == supplied_config
    assert candidate.config_snapshot is not supplied_config
    candidate.config_snapshot["sealed"]["pool"].append("B")
    assert supplied_config == {"sealed": {"pool": ["A"]}}
    assert candidate.started_by == "add-worker"
    assert candidate.finished_at is None and candidate.fixed_at is None
    assert candidate.warnings == {} and candidate.kpi == {}
    assert candidate.active_freeze_version is None and candidate.pinned is False
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id


def test_create_added_candidate_retries_exact_add_and_rejects_conflicts(db_session):
    _parent, accepted, target = _parent_and_target(db_session)
    plan = models.ProductionPlanHeader(
        name="retry add", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="fixed",
    )
    db_session.add(plan)
    db_session.commit()

    first = create_added_candidate_run(
        db_session, plan.id, target.id, "worker-a",
        horizon_days=30, config_version_id=None, config_snapshot={"v": 1},
    )
    db_session.commit()
    again = create_added_candidate_run(
        db_session, plan.id, target.id, "worker-b",
        horizon_days=30, config_version_id=None, config_snapshot={"v": 1},
    )
    assert again.run_id == first.run_id

    with pytest.raises(PlanningRunCandidateError, match="conflicting add lineage"):
        create_added_candidate_run(
            db_session, plan.id, target.id, "worker-c",
            horizon_days=31, config_version_id=None, config_snapshot={"v": 1},
        )

    # A refresh candidate has the same partial-index identity but is never a
    # retry of an add candidate.
    again.prior_run_id = 999
    db_session.flush()
    with pytest.raises(PlanningRunCandidateError, match="conflicting add lineage"):
        create_added_candidate_run(
            db_session, plan.id, target.id, "worker-c",
            horizon_days=30, config_version_id=None, config_snapshot={"v": 1},
        )
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id


def test_create_added_candidate_rejects_legacy_current_duplicate(db_session):
    _parent, accepted, target = _parent_and_target(db_session)
    plan = models.ProductionPlanHeader(
        name="legacy duplicate", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="fixed",
    )
    db_session.add(plan)
    db_session.flush()
    duplicate_current = models.PlanningRun(
        status="FIXED_SNAPSHOT", ledger_generation_id=None, source_plan_id=plan.id,
        period_from=plan.period_from, period_to=plan.period_to, config_snapshot={},
    )
    db_session.add(duplicate_current)
    db_session.flush()
    item = models.Item(
        item_code="legacy-dup-item", item_name="Legacy duplicate item",
        unit="шт", replenishment_method="Покупка", replenishment_time=7, status="active",
    )
    db_session.add(item)
    db_session.flush()
    req = models.MrpRequirement(
        run_id=duplicate_current.run_id, item_id=item.item_id, total_required_qty=0,
        net_required_qty=0, period_from=plan.period_from, period_to=plan.period_to, bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=accepted.id,
        item_id=item.item_id,
        run_id=duplicate_current.run_id,
        freeze_version=0,
        requirement_id=req.id,
        priority_period_from=plan.period_from,
        priority_period_to=plan.period_to,
    ))
    db_session.flush()

    with pytest.raises(PlanningRunCandidateError, match="already has a FIXED_SNAPSHOT"):
        create_added_candidate_run(
            db_session, plan.id, target.id, "worker",
            horizon_days=30, config_version_id=None, config_snapshot={},
        )


def test_create_added_candidate_rejects_duplicate_current_plan_and_outer_rollback(db_session):
    _parent, accepted, target = _parent_and_target(db_session)
    plan = models.ProductionPlanHeader(
        name="already planned", period_from=date(2026, 8, 1), period_to=date(2026, 8, 31), status="fixed",
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(models.PlanningRun(
        status="FIXED_SNAPSHOT", ledger_generation_id=accepted.id, source_plan_id=plan.id,
        config_snapshot={},
    ))
    db_session.commit()
    with pytest.raises(PlanningRunCandidateError, match="already has a FIXED_SNAPSHOT"):
        create_added_candidate_run(
            db_session, plan.id, target.id, "worker",
            horizon_days=30, config_version_id=None, config_snapshot={},
        )

    first_plan = models.ProductionPlanHeader(
        name="rollback add", period_from=date(2026, 9, 1), period_to=date(2026, 9, 30), status="fixed",
    )
    db_session.add(first_plan)
    db_session.flush()
    candidate = create_added_candidate_run(
        db_session, first_plan.id, target.id, "worker",
        horizon_days=30, config_version_id=None, config_snapshot={},
    )
    candidate_id = candidate.run_id
    db_session.rollback()
    assert db_session.get(models.PlanningRun, candidate_id) is None
