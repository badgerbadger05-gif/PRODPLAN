from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app import models
from app.services.planning_run_candidate import (
    PlanningRunCandidateError,
    create_candidate_run,
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
    db.commit()
    return parent, accepted, target


def test_create_candidate_copies_header_without_mutating_parent_or_pointer(db_session):
    parent, accepted, target = _parent_and_target(db_session)
    parent_config = parent.config_snapshot

    candidate = create_candidate_run(db_session, parent.run_id, target.id, "refresh-worker")

    assert candidate.status == "BUILDING_SNAPSHOT"
    assert candidate.prior_run_id == parent.run_id
    assert candidate.ledger_generation_id == target.id
    assert candidate.source_plan_id == parent.source_plan_id
    assert (candidate.period_from, candidate.period_to, candidate.horizon_days) == (
        parent.period_from, parent.period_to, parent.horizon_days,
    )
    assert candidate.config_version_id == parent.config_version_id
    assert candidate.config_snapshot == parent_config
    assert candidate.config_snapshot is not parent_config
    candidate.config_snapshot["nested"]["pool"].append("B")
    assert parent.config_snapshot == {"nested": {"pool": ["A"]}}
    assert candidate.started_by == "refresh-worker"
    assert candidate.started_at is not None
    assert candidate.finished_at is None and candidate.fixed_at is None
    assert candidate.warnings == {} and candidate.kpi == {}
    assert candidate.active_freeze_version is None and candidate.pinned is False
    assert parent.status == "FIXED_SNAPSHOT"
    assert parent.ledger_generation_id == accepted.id
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id


def test_create_candidate_is_idempotent_only_for_exact_lineage(db_session):
    parent, _accepted, target = _parent_and_target(db_session)
    first = create_candidate_run(db_session, parent.run_id, target.id, "worker-a")
    db_session.commit()
    again = create_candidate_run(db_session, parent.run_id, target.id, "worker-b")
    assert again.run_id == first.run_id

    first.prior_run_id = None
    db_session.flush()
    with pytest.raises(PlanningRunCandidateError, match="conflicting"):
        create_candidate_run(db_session, parent.run_id, target.id, "worker-c")


def test_candidate_identity_ignores_historical_run_but_rejects_second_building(db_session):
    parent, _accepted, target = _parent_and_target(db_session)
    historical = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        ledger_generation_id=target.id,
        source_plan_id=parent.source_plan_id,
        config_snapshot={},
    )
    db_session.add(historical)
    db_session.commit()

    candidate = create_candidate_run(db_session, parent.run_id, target.id, "worker")
    assert candidate.status == "BUILDING_SNAPSHOT"
    assert candidate.run_id != historical.run_id
    assert create_candidate_run(db_session, parent.run_id, target.id, "retry").run_id == candidate.run_id

    duplicate = models.PlanningRun(
        status="BUILDING_SNAPSHOT",
        ledger_generation_id=target.id,
        source_plan_id=parent.source_plan_id,
        config_snapshot={},
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


@pytest.mark.parametrize("mutation", ["parent_status", "target_status", "wrong_parent", "wrong_cutoff"])
def test_create_candidate_rejects_bad_parent_or_target_lineage(db_session, mutation):
    parent, accepted, target = _parent_and_target(db_session)
    if mutation == "parent_status":
        parent.status = "IN_PROGRESS"
    elif mutation == "target_status":
        target.status = "accepted"
    elif mutation == "wrong_parent":
        target.source_watermarks = {"generation_kind": "obligation_refresh", "parent_generation_id": 999}
    elif mutation == "wrong_cutoff":
        target.cutoff = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
    db_session.flush()

    with pytest.raises(PlanningRunCandidateError):
        create_candidate_run(db_session, parent.run_id, target.id, "worker")
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id


def test_create_candidate_is_removed_by_outer_rollback(db_session):
    parent, accepted, target = _parent_and_target(db_session)
    candidate = create_candidate_run(db_session, parent.run_id, target.id, "worker")
    candidate_id = candidate.run_id
    db_session.rollback()

    assert db_session.get(models.PlanningRun, candidate_id) is None
    assert db_session.get(models.PlanningTruthState, 1).current_generation_id == accepted.id
