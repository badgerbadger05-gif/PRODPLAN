"""R3 current-MRP readers must use the explicit live pointer."""

from datetime import date, datetime, timezone

import pytest

from app import models
from app.services import period_plan_service
from app.services.production_control_journal import _accepted_fixed_run_ids
from app.services.item_ledger.r3_contract import (
    CurrentMrpResolutionError,
    current_live_run,
)
from app.services.item_ledger.live_plan_scope import current_live_run_ids


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


def test_current_scope_fails_closed_when_fixed_plan_pointer_is_missing(db_session):
    _plan, _run = _current_plan(db_session)
    db_session.commit()

    with pytest.raises(CurrentMrpResolutionError, match="active MRP pointer"):
        current_live_run_ids(db_session)


def test_current_pointer_rejects_retired_or_mismatched_run(db_session):
    plan, run = _current_plan(db_session)
    run.status = "CLOSED"
    db_session.add(models.PlanningLivePointer(plan_id=plan.id, run_id=run.run_id))
    db_session.commit()

    with pytest.raises(CurrentMrpResolutionError, match="not FIXED_SNAPSHOT"):
        current_live_run(db_session, plan.id)


def test_current_journal_selector_uses_pointer_when_run_is_on_old_generation(db_session):
    plan, run = _current_plan(db_session)
    old = models.LedgerGeneration(
        generation_key="r3-current-reader-old-generation",
        status="accepted",
        cutoff=datetime(2026, 9, 9, tzinfo=timezone.utc),
        accepted_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        algorithm_version="r3-current-reader-old",
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch_id=run.ledger_generation.physical_import_batch_id,
    )
    db_session.add(old)
    db_session.flush()
    run.ledger_generation_id = old.id
    run.ledger_cutoff = old.cutoff
    db_session.add(models.PlanningLivePointer(plan_id=plan.id, run_id=run.run_id))
    db_session.commit()

    current_generation_id = db_session.get(models.PlanningTruthState, 1).current_generation_id
    assert _accepted_fixed_run_ids(
        db_session, ledger_generation_id=int(current_generation_id)
    ) == [int(run.run_id)]


def test_current_journal_fails_closed_when_old_generation_pointer_is_missing(db_session):
    plan, run = _current_plan(db_session)
    old = models.LedgerGeneration(
        generation_key="r3-current-reader-old-generation-missing-pointer",
        status="accepted",
        cutoff=datetime(2026, 9, 9, tzinfo=timezone.utc),
        accepted_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        algorithm_version="r3-current-reader-old-missing-pointer",
        source_watermarks={},
        capabilities={"physical_ledger": True},
        physical_import_batch_id=run.ledger_generation.physical_import_batch_id,
    )
    db_session.add(old)
    db_session.flush()
    run.ledger_generation_id = old.id
    run.ledger_cutoff = old.cutoff
    db_session.commit()

    current_generation_id = db_session.get(models.PlanningTruthState, 1).current_generation_id
    with pytest.raises(CurrentMrpResolutionError, match="active MRP pointer"):
        _accepted_fixed_run_ids(
            db_session, ledger_generation_id=int(current_generation_id)
        )
