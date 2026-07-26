from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app import models


def _plan(db) -> models.ProductionPlanHeader:
    plan = models.ProductionPlanHeader(
        name="snapshot-plan",
        period_from=date(2026, 7, 1),
        period_to=date(2026, 7, 31),
        status="fixed",
    )
    db.add(plan)
    db.flush()
    return plan


def test_fixed_snapshot_source_plan_id_is_singleton_when_not_null(db_session):
    plan = _plan(db_session)
    db_session.add(models.PlanningRun(status="FIXED_SNAPSHOT", source_plan_id=plan.id))
    db_session.commit()

    db_session.add(models.PlanningRun(status="FIXED_SNAPSHOT", source_plan_id=plan.id))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_fixed_snapshot_constraint_allows_other_statuses_and_null_source_plan(db_session):
    plan = _plan(db_session)
    db_session.add(models.PlanningRun(status="FIXED_SNAPSHOT", source_plan_id=plan.id))
    db_session.add(models.PlanningRun(status="BUILDING_SNAPSHOT", source_plan_id=plan.id))
    db_session.add(models.PlanningRun(status="CLOSED", source_plan_id=plan.id))
    db_session.add(models.PlanningRun(status="FIXED_SNAPSHOT", source_plan_id=None))
    db_session.add(models.PlanningRun(status="FIXED_SNAPSHOT", source_plan_id=None))
    db_session.commit()

    rows = db_session.query(models.PlanningRun).filter(
        models.PlanningRun.source_plan_id == plan.id,
    ).all()
    assert {str(row.status) for row in rows} == {"FIXED_SNAPSHOT", "BUILDING_SNAPSHOT", "CLOSED"}

    null_rows = db_session.query(models.PlanningRun).filter(
        models.PlanningRun.source_plan_id.is_(None),
        models.PlanningRun.status == "FIXED_SNAPSHOT",
    ).all()
    assert len(null_rows) == 2
