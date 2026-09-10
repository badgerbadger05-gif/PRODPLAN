"""R9 period-execution current DTO and read-only publication contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app import models
from app.routers.plan import period_plans_execution_journal
from app.services.item_ledger.current_execution import (
    load_current_execution_rows,
    publish_current_obligation_views_from_generation,
    publish_current_execution_scope,
)


def _generation(db_session):
    cutoff = datetime(2026, 9, 11, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key="r9-period-dto-batch", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r9-period-dto-generation", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=batch, algorithm_version="r9-test",
        replay_version="r9-test",
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def _period_row():
    return {
        "row_key": "req:101",
        "req_id": 101,
        "run_id": 41,
        "plan_id": 7,
        "item_id": 501,
        "item_code": "ITEM-501",
        "item_name": "Item 501",
        "flow": "purchase",
        "bom_level": 0,
        "gross_qty": 5.0,
        "net_qty": 5.0,
        "ordered_qty": 0.0,
        "completed_qty": 0.0,
        "coverage_pct": 0.0,
        "remaining_qty": 5.0,
        "status": "none",
        "status_label": "Не оформлено",
        "explanations": ["Требуется закупка"],
        "information_links": {"reservation_events": []},
        "facets": {"bom_levels": [0], "flows": ["purchase"]},
        "work_items": [{
            "type": "planned_purchase",
            "qty": 5.0,
            "purchase_id": 701,
            "source_mrp_requirement_id": 101,
            "run_id": 41,
            "one_c_opened": False,
            "completed_qty": 0.0,
            "remaining_qty": 5.0,
        }],
    }


def test_period_publication_persists_stable_work_item_dto_and_navigation(db_session):
    generation = _generation(db_session)
    payload = {
        "plan": {"id": 7, "name": "Plan 7"},
        "run_id": 41,
        "truth_status": "accepted",
        "rows": [_period_row()],
        "facets": {"bom_levels": [0], "flows": ["purchase"]},
        "summary": {
            "truth_status": "accepted", "total_items": 1,
            "execution_pct": 0.0, "execution_available_base_qty": 5.0,
        },
    }
    snapshot = models.PlanningReadSnapshot(
        consumer="period_plan_execution", snapshot_key="plan:7:run:41",
        ledger_generation_id=generation.id, cutoff=generation.cutoff,
        truth_status="accepted", payload=payload, published_at=generation.cutoff,
    )
    db_session.add(snapshot)
    db_session.commit()

    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()
    [current] = load_current_execution_rows(
        db_session, entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    )
    work_item = current.payload["work_items"][0]
    assert current.payload["current_identity"] == current.business_identity
    assert current.payload["source_revision"] == f"accepted:g{generation.id}:period_plan_execution"
    assert work_item["assigned_qty"] == 0.0
    assert work_item["unassigned_qty"] == 5.0
    assert work_item["current_identity"] == "mrp-run:41:requirement:101:planned-purchase:701"
    assert work_item["navigation_href"]
    assert work_item["navigation_reason"] is None


def test_period_current_get_returns_persisted_summary_without_business_recalculation(
    db_session, monkeypatch,
):
    summary = {
        "truth_status": "accepted", "total_items": 1,
        "execution_pct": 0.0, "execution_available_base_qty": 5.0,
    }
    metadata = {
        "plan": {"id": 7, "name": "Plan 7"}, "run_id": 41,
        "summary": summary, "facets": {"bom_levels": [0], "flows": ["purchase"]},
        "plan_output_rows": [], "truth_status": "accepted",
    }
    row = _period_row()
    row["current_identity"] = "plan:7:req:101"
    row["source_revision"] = "accepted:g-current:period_plan_execution"
    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g-current:period_plan_execution",
        scope_key="period-plan:all-live-plans",
        rows=[{
            "entity_kind": "period_plan_execution",
            "business_identity": "plan:7:req:101",
            "scope_key": "period-plan:all-live-plans",
            "payload": row,
        }],
        entity_kinds=("period_plan_execution",),
        summary={"snapshots": {"plan:7:run:41": metadata}},
    )
    db_session.commit()

    import app.services.period_plan_service as service
    monkeypatch.setattr(service, "_get_plan", lambda db, plan_id: SimpleNamespace(id=plan_id))
    monkeypatch.setattr(service, "_resolve_execution_run", lambda db, plan, run_id: SimpleNamespace(run_id=run_id or 41))
    monkeypatch.setattr(
        service, "_finalize_execution_payload",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("GET recalculated period execution business totals")
        ),
    )

    response = asyncio.run(period_plans_execution_journal(plan_id=7, db=db_session))
    assert response.summary.execution_pct == 0.0
    assert response.facets == {"bom_levels": [0], "flows": ["purchase"]}
    assert response.rows[0].status_label == "Не оформлено"
    assert response.rows[0].work_items[0].assigned_qty == 0.0
