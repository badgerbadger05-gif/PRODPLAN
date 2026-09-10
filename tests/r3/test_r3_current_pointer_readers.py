"""R3 current-MRP readers must use the explicit live pointer."""

from datetime import date, datetime, timezone

import pytest

from app import models
from app.services import period_plan_service
from app.services.item_ledger.r3_contract import (
    CurrentMrpResolutionError,
    current_live_run,
)


def _current_plan(db):
    cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="r3-current-reader-physical",
        status="completed",
        source_complete=True,
        cutoff=cutoff,
        completed_at=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r3-current-reader-generation",
        status="accepted",
        cutoff=cutoff,
        accepted_at=cutoff,
        algorithm_version="r3-current-reader",
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch=physical,
    )
    plan = models.ProductionPlanHeader(
        name="R3 current reader",
        status="fixed",
        period_from=date(2026, 9, 1),
        period_to=date(2026, 9, 30),
    )
    db.add_all([physical, generation, plan])
    db.flush()
    db.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT",
        source_plan_id=plan.id,
        ledger_generation_id=generation.id,
        ledger_cutoff=cutoff,
        config_snapshot={},
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db.add(run)
    db.flush()
    return plan, run


def test_period_plan_current_reader_uses_pointer_without_lineage_walk(
    db_session, monkeypatch
):
    plan, run = _current_plan(db_session)
    db_session.add(models.PlanningLivePointer(plan_id=plan.id, run_id=run.run_id))
    db_session.commit()

    monkeypatch.setattr(
        period_plan_service,
        "live_plan_run_ids",
        lambda *_args, **_kwargs: pytest.fail("current reader walked generation lineage"),
    )
    result = period_plan_service.list_mrp_runs_for_plan(db_session, plan.id)

    assert [row["run_id"] for row in result["rows"]] == [run.run_id]


def test_period_plan_current_reader_fails_closed_without_pointer(db_session):
    plan, _run = _current_plan(db_session)
    db_session.commit()

    with pytest.raises(CurrentMrpResolutionError, match="active MRP pointer"):
        period_plan_service.list_mrp_runs_for_plan(db_session, plan.id)


def test_current_pointer_rejects_retired_or_mismatched_run(db_session):
    plan, run = _current_plan(db_session)
    run.status = "CLOSED"
    db_session.add(models.PlanningLivePointer(plan_id=plan.id, run_id=run.run_id))
    db_session.commit()

    with pytest.raises(CurrentMrpResolutionError, match="not FIXED_SNAPSHOT"):
        current_live_run(db_session, plan.id)

