"""B4: planning run lifecycle — re-run idempotency + failure handling.

Covers:
  * _clear_run_outputs removes every child table of a run (and only that run).
  * A run that fails mid-computation discards partial rows and is marked FAILURE
    instead of being committed half-built (the old `finally: db.commit()` bug).
  * Recomputing an existing run_id clears previous outputs (no doubling).
"""

import datetime

import pytest

from app.models import (
    PlanningRun,
    PlannedOrder,
    PlannedOrderStage,
    PlannedPurchase,
    PlannedRework,
    CapacityLoad,
    PeggingLink,
)
from app.services import planning_service
from app.services.planning_service import run_planning_run, _clear_run_outputs

_CHILD_MODELS = (
    PlannedOrder,
    PlannedOrderStage,
    PlannedPurchase,
    PlannedRework,
    CapacityLoad,
    PeggingLink,
)


def _mk_run(db, status="SUCCESS"):
    run = PlanningRun(
        status=status,
        started_by="test",
        horizon_days=30,
        config_version_id=0,
        config_snapshot={},
        warnings=[],
        kpi={},
        started_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db.add(run)
    db.flush()
    return run


def _seed_outputs(db, run_id, n=2):
    d = datetime.date(2026, 1, 1)
    for i in range(n):
        order = PlannedOrder(
            run_id=run_id, item_id=1, requested_qty=1, planned_qty=1, qty=1,
            need_date=d, bucket_date=d,
        )
        db.add(order)
        db.flush()
        db.add(PlannedOrderStage(run_id=run_id, order_id=order.order_id, stage_id=1, bucket_date=d, hours=1))
        db.add(PlannedPurchase(
            run_id=run_id, item_id=1, requested_qty=1, planned_qty=1, qty=1,
            need_date=d, order_date=d, lead_time_days=1, bucket_date=d,
        ))
        db.add(PlannedRework(
            run_id=run_id, item_id=1, requested_qty=1, planned_qty=1, qty=1,
            need_date=d, order_date=d, lead_time_days=1, bucket_date=d,
        ))
        # area_id varies to respect ux_capacity_load_run_area_date
        db.add(CapacityLoad(run_id=run_id, area_id=i + 1, bucket_date=d))
        db.add(PeggingLink(run_id=run_id, child_item_id=1, qty_contribution=1))
    db.flush()


def test_clear_run_outputs_removes_only_target_run(db_session):
    db = db_session
    run = _mk_run(db)
    other = _mk_run(db)
    _seed_outputs(db, run.run_id, n=2)
    _seed_outputs(db, other.run_id, n=1)
    db.commit()

    _clear_run_outputs(db, run.run_id)
    db.commit()

    for model in _CHILD_MODELS:
        assert db.query(model).filter(model.run_id == run.run_id).count() == 0
        assert db.query(model).filter(model.run_id == other.run_id).count() == 1
    # the run header itself must survive
    assert db.query(PlanningRun).filter(PlanningRun.run_id == run.run_id).first() is not None


def test_failed_run_discards_partial_outputs_and_marks_failure(db_session, monkeypatch):
    db = db_session

    def boom(db_, horizon, snapshot):
        run = db_.query(PlanningRun).order_by(PlanningRun.run_id.desc()).first()
        d = datetime.date(2026, 1, 1)
        db_.add(PlannedOrder(
            run_id=run.run_id, item_id=1, requested_qty=1, planned_qty=1, qty=1,
            need_date=d, bucket_date=d,
        ))
        db_.flush()
        raise RuntimeError("boom")

    monkeypatch.setattr(planning_service, "compute_planning_preview", boom)

    with pytest.raises(RuntimeError):
        run_planning_run(db)

    # partial order written before the crash must be rolled back
    assert db.query(PlannedOrder).count() == 0
    runs = db.query(PlanningRun).all()
    assert len(runs) == 1
    assert runs[0].status == "FAILURE"
    assert runs[0].finished_at is not None


def test_rerun_clears_previous_outputs(db_session, monkeypatch):
    db = db_session
    run = _mk_run(db, status="SUCCESS")
    _seed_outputs(db, run.run_id, n=3)
    db.commit()
    rid = run.run_id

    def boom(db_, horizon, snapshot):
        raise RuntimeError("stop after clear")

    monkeypatch.setattr(planning_service, "compute_planning_preview", boom)

    with pytest.raises(RuntimeError):
        run_planning_run(db, run_id=rid)

    # idempotent re-run cleared the old outputs up front (committed before work)
    for model in _CHILD_MODELS:
        assert db.query(model).filter(model.run_id == rid).count() == 0
    assert db.query(PlanningRun).filter(PlanningRun.run_id == rid).first().status == "FAILURE"
