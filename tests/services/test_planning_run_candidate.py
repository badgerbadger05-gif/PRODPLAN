from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app import models
from app.services.planning_run_candidate import (
    PlanningRunCandidateError,
    create_added_candidate_run,
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
    target.physical_import_batch_id = accepted.physical_import_batch_id
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


def test_create_candidate_accepts_historical_parent_with_reservation_lineage(db_session):
    parent, accepted, target = _parent_and_target(db_session)
    parent.ledger_generation_id = None
    item = models.Item(
        item_code="RES-HIST", item_name="Hist item", unit="шт", replenishment_method="Покупка",
        replenishment_time=3, stock_qty=0, status="active",
    )
    db_session.add(item)
    db_session.flush()
    req = models.MrpRequirement(
        run_id=parent.run_id, item_id=item.item_id, total_required_qty=0,
        net_required_qty=0, period_from=parent.period_from, period_to=parent.period_to,
        bom_level=0,
    )
    db_session.add(req)
    db_session.flush()
    db_session.add(models.ReservationEntry(
        ledger_generation_id=accepted.id, item_id=item.item_id, run_id=parent.run_id,
        freeze_version=0, requirement_id=req.id, priority_period_from=parent.period_from,
        priority_period_to=parent.period_to,
    ))
    candidate = create_candidate_run(db_session, parent.run_id, target.id, "refresh-worker")
    assert candidate.prior_run_id == parent.run_id
    assert candidate.source_plan_id == parent.source_plan_id
    assert candidate.ledger_generation_id == target.id


def test_create_candidate_rejects_ambiguous_reservation_lineage(db_session):
    parent, accepted, target = _parent_and_target(db_session)
    parent.ledger_generation_id = None
    alt_generation = models.LedgerGeneration(
        generation_key="historical-legacy", status="stale", cutoff=accepted.cutoff,
        source_watermarks={}, physical_import_batch=accepted.physical_import_batch,
        capabilities={},
        algorithm_version="tests/1",
    )
    # Keep pointer unchanged; lineage for this parent must remain unambiguous.
    db_session.add(alt_generation)
    db_session.flush()
    item = models.Item(
        item_code="RES-HIST-AMB", item_name="Ambiguous", unit="шт", replenishment_method="Покупка",
        replenishment_time=3, stock_qty=0, status="active",
    )
    db_session.add(item)
    db_session.flush()
    item_b = models.Item(
        item_code="RES-HIST-AMB-2", item_name="Ambiguous item b", unit="шт", replenishment_method="Покупка",
        replenishment_time=3, stock_qty=0, status="active",
    )
    db_session.add(item_b)
    db_session.flush()
    req_a = models.MrpRequirement(
        run_id=parent.run_id, item_id=item.item_id, total_required_qty=0,
        net_required_qty=0, period_from=parent.period_from, period_to=parent.period_to,
        bom_level=0,
    )
    req_b = models.MrpRequirement(
        run_id=parent.run_id, item_id=item_b.item_id, total_required_qty=0,
        net_required_qty=0, period_from=parent.period_from, period_to=parent.period_to,
        bom_level=0,
    )
    db_session.add_all([req_a, req_b])
    db_session.flush()
    db_session.add_all([
        models.ReservationEntry(
            ledger_generation_id=accepted.id, item_id=item.item_id, run_id=parent.run_id,
            freeze_version=0, requirement_id=req_a.id, priority_period_from=parent.period_from,
            priority_period_to=parent.period_to,
        ),
        models.ReservationEntry(
            ledger_generation_id=alt_generation.id, item_id=item_b.item_id, run_id=parent.run_id,
            freeze_version=0, requirement_id=req_b.id, priority_period_from=parent.period_from,
            priority_period_to=parent.period_to,
        ),
    ])
    db_session.flush()

    with pytest.raises(PlanningRunCandidateError, match="ambiguous Ledger lineage"):
        create_candidate_run(db_session, parent.run_id, target.id, "refresh-worker")


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
