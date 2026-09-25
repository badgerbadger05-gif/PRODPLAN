"""Closed period plans are served by their immutable history, not the live scope.

`.docs/period_plan_target.md` («Закрытие») keeps the closed plan in a saved
snapshot, and the current execution scope is built only from live plan runs
(`period-plan:all-live-plans`).  Reading a closed plan out of the live scope can
therefore never succeed, so the journal must fall back to nothing — it must ask
the closed-history owner.  A live fixed run whose scope entry is missing stays
fail closed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app import models
from app.routers.plan import period_plans_execution_journal
from app.services.item_ledger.current_execution import (
    publish_current_obligation_views_from_generation,
)


def _generation(db_session, key="closed-journal"):
    cutoff = datetime(2026, 9, 11, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key=f"{key}-batch", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key=f"{key}-generation", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=batch, algorithm_version="closed-journal-test",
        replay_version="closed-journal-test",
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def _plan(db_session, plan_id, *, status):
    plan = models.ProductionPlanHeader(
        id=plan_id,
        name=f"Plan {plan_id}",
        status=status,
        period_from=datetime(2026, 9, 1).date(),
        period_to=datetime(2026, 9, 30).date(),
    )
    db_session.add(plan)
    db_session.flush()
    return plan


def _run(db_session, run_id, plan, generation, *, status):
    run = models.PlanningRun(
        run_id=run_id,
        source_plan_id=plan.id,
        ledger_generation_id=generation.id,
        ledger_cutoff=generation.cutoff,
        status=status,
        period_from=plan.period_from,
        period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.flush()
    return run


def _publish_live_scope(db_session, generation, plan, run):
    publish_current_obligation_views_from_generation(
        db_session,
        generation.id,
        purchase_payload={"rows": []},
        production_payload={"rows": [], "meta": {"row_count": 0}},
        mrp_payloads={},
        period_payloads={
            f"plan:{int(plan.id)}:run:{int(run.run_id)}": {
                "plan": {"id": int(plan.id), "name": plan.name},
                "run_id": int(run.run_id),
                "truth_status": "accepted",
                "rows": [],
                "plan_output_rows": [],
                "facets": {"bom_levels": [], "flows": []},
                "summary": {"truth_status": "accepted", "total_items": 0},
            }
        },
    )
    db_session.commit()


def test_closed_plan_without_live_run_serves_an_empty_journal(db_session):
    generation = _generation(db_session)
    live_plan = _plan(db_session, 71, status="fixed")
    live_run = _run(db_session, 411, live_plan, generation, status="FIXED_SNAPSHOT")
    closed_plan = _plan(db_session, 72, status="closed")
    _run(db_session, 412, closed_plan, generation, status="CLOSED")
    _publish_live_scope(db_session, generation, live_plan, live_run)

    result = asyncio.run(period_plans_execution_journal(plan_id=72, db=db_session))

    assert result.run_id == 412
    assert result.rows == []
    assert result.total == 0
    assert result.summary.total_items == 0


def test_live_fixed_run_without_published_scope_stays_fail_closed(db_session):
    generation = _generation(db_session)
    live_plan = _plan(db_session, 81, status="fixed")
    live_run = _run(db_session, 421, live_plan, generation, status="FIXED_SNAPSHOT")
    other_plan = _plan(db_session, 82, status="fixed")
    # A fixed run anchored outside this generation's sealed lineage is not part
    # of the published live scope, so its journal has no current answer.
    _run(
        db_session, 422, other_plan,
        _generation(db_session, key="closed-journal-foreign"),
        status="FIXED_SNAPSHOT",
    )
    _publish_live_scope(db_session, generation, live_plan, live_run)

    with pytest.raises(HTTPException) as caught:
        asyncio.run(period_plans_execution_journal(plan_id=82, db=db_session))

    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "period_plan_execution_unavailable"
