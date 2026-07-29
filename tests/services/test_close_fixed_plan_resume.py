"""Resume of an interrupted «Закрыть план» must finish, not restart or double-publish.

``close_fixed_plan`` drives one obligation refresh: fork → manifest → carry
forward → freeze/reservations → replenishment work items → snapshots →
publication.  A worker that reaches the database and then dies leaves a BUILDING
generation behind under a deterministic ``generation_key``.  Repeating the very
same close must pick that generation up and play it to the end — exactly once.

The fault injection below commits at each boundary before raising: that is how a
partially advanced generation survives the crash of its worker.  Without a
commit the whole refresh would simply roll back and there would be nothing to
resume.
"""

from datetime import date, datetime as dt, timezone
from decimal import Decimal

import pytest

from app import models
from app.services import obligation_refresh_orchestrator as workflow
from app.services.period_plan_service import (
    _close_refresh_generation_key,
    close_fixed_plan,
    fix_period_plan,
)


CUTOFF = dt(2026, 7, 23, 12, tzinfo=timezone.utc)


class InjectedFault(RuntimeError):
    """The simulated crash of the refresh worker."""


def _accepted_world(db):
    physical = models.PhysicalImportBatch(
        batch_key="resume-physical", status="completed", cutoff=CUTOFF,
        source_watermarks={"opening_at": "2025-01-01T00:00:00+00:00"},
        completed_at=CUTOFF,
    )
    accepted = models.LedgerGeneration(
        generation_key="resume-accepted", status="accepted", cutoff=CUTOFF,
        source_watermarks={"replay_from": "2026-07-01T00:00:00+00:00"},
        capabilities={
            "physical_ledger": True,
            "reservation_replay": True,
            "execution_allocations": True,
        },
        physical_import_batch=physical, algorithm_version="test", accepted_at=CUTOFF,
    )
    item = models.Item(
        item_code="RESUME-BUY", item_name="покупная деталь",
        replenishment_method="Покупка", replenishment_time=3, status="active",
    )
    db.add_all([physical, accepted, item])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=accepted.id))
    resource = models.ProductionResource(
        resource_name="Close resume assembly",
        planning_range=30,
        capacity=Decimal("100"),
    )
    db.add(resource)
    db.flush()
    db.add(models.AssemblyRate(
        resource_id=int(resource.resource_id),
        item_id=int(item.item_id),
        qty_per_capacity=Decimal("1"),
    ))
    db.commit()
    return accepted, item


def _fixed_plan_with_run(db, item, *, name="resume me"):
    plan = models.ProductionPlanHeader(
        name=name, status="draft",
        period_from=date(2026, 8, 1), period_to=date(2026, 8, 31),
    )
    db.add(plan)
    db.flush()
    db.add(models.ProductionPlanLine(
        plan_id=plan.id, item_id=item.item_id,
        bucket_date=date(2026, 8, 1), qty=Decimal("5"),
    ))
    db.commit()
    fix_period_plan(db, plan.id, fixed_by="tester")
    db.commit()
    run = db.query(models.PlanningRun).filter_by(
        source_plan_id=plan.id, status="FIXED_SNAPSHOT",
    ).one()
    return plan, run


def _install_fault(monkeypatch, db, name, *, after=True):
    original = getattr(workflow, name)

    def wrapper(*args, **kwargs):
        if after:
            original(*args, **kwargs)
        db.commit()
        raise InjectedFault(name)

    monkeypatch.setattr(workflow, name, wrapper)


# stage name -> (orchestrator symbol, run the original first?)
FAULTS = {
    "after_fork": ("fork_obligation_generation", True),
    "after_manifest": ("create_obligation_refresh_manifest", True),
    "after_freeze": ("materialize_replenishment_work_items", False),
    "after_work_items": ("replay_candidate_realizations", False),
    "after_snapshots": ("build_assembly_queue_snapshot", True),
    "after_snapshot_seal": ("validate_obligation_refresh_build", False),
    "before_publish": ("publish_obligation_refresh_batch", False),
}

@pytest.mark.parametrize("stage", sorted(FAULTS))
def test_close_fixed_plan_resumes_after_a_fault_at_every_stage(
    db_session, monkeypatch, stage
):
    accepted, item = _accepted_world(db_session)
    plan, run = _fixed_plan_with_run(db_session, item)
    parent_generation_id = int(
        db_session.query(models.PlanningTruthState.current_generation_id).scalar()
    )
    key = _close_refresh_generation_key(
        run_id=int(run.run_id), parent_generation_id=parent_generation_id,
    )

    symbol, after = FAULTS[stage]
    _install_fault(monkeypatch, db_session, symbol, after=after)
    with pytest.raises(InjectedFault):
        close_fixed_plan(db_session, int(run.run_id))
    monkeypatch.undo()

    db_session.expire_all()
    assert str(db_session.get(models.PlanningRun, run.run_id).status) == "FIXED_SNAPSHOT"
    assert str(db_session.get(models.ProductionPlanHeader, plan.id).status) == "fixed"

    result = close_fixed_plan(db_session, int(run.run_id))

    assert result["status"] == "closed"
    db_session.expire_all()
    targets = db_session.query(models.LedgerGeneration).filter_by(
        generation_key=key
    ).all()
    assert len(targets) == 1
    target = targets[0]
    assert str(target.status) == "accepted"
    assert int(result["published_generation_id"]) == int(target.id)
    assert str(db_session.get(models.PlanningRun, run.run_id).status) == "CLOSED"
    assert str(db_session.get(models.ProductionPlanHeader, plan.id).status) == "closed"
    assert int(
        db_session.query(models.PlanningTruthState.current_generation_id).scalar()
    ) == int(target.id)
    assert db_session.query(models.ClosedPlanSnapshot).filter_by(
        plan_id=plan.id, run_id=run.run_id,
    ).count() == 1
    # exactly three generations exist: the seed truth, the one «Зафиксировать»
    # published, and the single one this closure published.
    assert db_session.query(models.LedgerGeneration).count() == 3
    assert db_session.query(models.LedgerGeneration).filter_by(
        status="accepted"
    ).count() == 3
