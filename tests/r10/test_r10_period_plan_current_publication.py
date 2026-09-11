"""R10 period-plan execution current-owner contracts (RED first)."""

from datetime import date, datetime, timezone

import pytest

from app import models
from app.services.item_ledger import current_execution
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_rows,
    publish_current_obligation_views_from_generation,
)


def _generation(db_session):
    cutoff = datetime(2026, 9, 12, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key="r10-period-current-batch", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r10-period-current-generation", status="accepted", cutoff=cutoff,
        accepted_at=cutoff, source_watermarks={}, capabilities={},
        physical_import_batch=batch, algorithm_version="r10-period-test",
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.flush()
    return generation


def _period_payload(plan_id: int, run_id: int) -> dict:
    return {
        "plan": {"id": plan_id, "name": "Current plan"},
        "run_id": run_id,
        "truth_status": "accepted",
        "truth_generation_id": 11,
        "truth_cutoff": "2026-09-12T00:00:00+00:00",
        "truth_reason": None,
        "summary": {
            "truth_status": "accepted",
            "total_items": 1,
            "execution_pct": 0.0,
            "planned_output_qty": 4.0,
            "accepted_plan_output_qty": 0.0,
            "assembly_remaining_qty": 4.0,
        },
        "plan_output_rows": [{
            "plan_line_id": 901,
            "item_id": 501,
            "bucket_date": "2026-09-12",
            "planned_output_qty": 4.0,
            "accepted_plan_output_qty": 0.0,
            "assembly_remaining_qty": 4.0,
        }],
        "facets": {"bom_levels": [0], "flows": ["purchase"]},
        "rows": [{
            "req_id": 1001,
            "run_id": run_id,
            "plan_id": plan_id,
            "item_id": 501,
            "item_code": "ITEM-501",
            "item_name": "Item 501",
            "flow": "purchase",
            "bom_level": 0,
            "net_qty": 3.0,
            "remaining_qty": 3.0,
            "status": "none",
            "root_item_ids": [501],
            "ledger_links": {"item_id": 501, "reservation_ids": [], "events": []},
            "work_items": [],
        }],
    }


def _mrp_empty_payload(run_id: int) -> dict:
    return {
        "run_id": run_id,
        "rows": [],
        "row_counts": {"production": 0, "purchase": 0, "rework": 0, "capacity": 0},
    }


def test_period_current_publisher_requires_explicit_payload_and_never_reads_snapshot(
    db_session,
):
    generation = _generation(db_session)
    plan = models.ProductionPlanHeader(
        name="Current plan", status="fixed",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
    )
    db_session.add(plan)
    db_session.flush()
    run = models.PlanningRun(
        source_plan_id=plan.id, ledger_generation_id=generation.id,
        status="FIXED_SNAPSHOT", period_from=plan.period_from, period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.flush()

    with pytest.raises(CurrentExecutionUnavailable, match="period"):
        publish_current_obligation_views_from_generation(
            db_session,
            generation.id,
            purchase_payload={"rows": []},
            production_payload={"rows": [], "meta": {"row_count": 0}},
            mrp_payloads={str(run.run_id): _mrp_empty_payload(run.run_id)},
        )

    assert db_session.query(models.CurrentExecutionScope).count() == 0
    assert db_session.query(models.CurrentExecutionScope).count() == 0


def test_period_current_payloads_require_exact_live_run_set_before_dml():
    payload = _period_payload(7, 41)
    with pytest.raises(CurrentExecutionUnavailable, match="period.*extra"):
        current_execution._require_period_current_payloads(
            {
                "plan:7:run:41": payload,
                "plan:99:run:999": _period_payload(99, 999),
            },
            required_run_ids=[41],
        )


def test_period_current_publisher_persists_direct_payload_and_retry_is_noop(db_session):
    generation = _generation(db_session)
    plan = models.ProductionPlanHeader(
        name="Current plan", status="fixed",
        period_from=date(2026, 9, 1), period_to=date(2026, 9, 30),
    )
    db_session.add(plan)
    db_session.flush()
    run = models.PlanningRun(
        source_plan_id=plan.id, ledger_generation_id=generation.id,
        status="FIXED_SNAPSHOT", period_from=plan.period_from, period_to=plan.period_to,
    )
    db_session.add(run)
    db_session.flush()
    payload = _period_payload(int(plan.id), int(run.run_id))
    result = publish_current_obligation_views_from_generation(
        db_session,
        generation.id,
        purchase_payload={"rows": []},
        production_payload={"rows": [], "meta": {"row_count": 0}},
        mrp_payloads={str(run.run_id): _mrp_empty_payload(run.run_id)},
        period_payloads={f"plan:{plan.id}:run:{run.run_id}": payload},
    )
    db_session.commit()

    [row] = load_current_execution_rows(
        db_session,
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    )
    assert row.business_identity == f"plan:{plan.id}:req:1001"
    scope = db_session.query(models.CurrentExecutionScope).filter_by(
        entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    ).one()
    metadata = scope.summary["snapshots"][f"plan:{plan.id}:run:{run.run_id}"]
    assert metadata["plan_output_rows"][0]["assembly_remaining_qty"] == 4.0
    assert metadata["facets"] == {"bom_levels": [0], "flows": ["purchase"]}
    assert result["period_plan_execution"].idempotent is False

    before_changes = db_session.query(models.CurrentExecutionChange).count()
    before_scopes = db_session.query(models.CurrentExecutionScope).count()
    retry = publish_current_obligation_views_from_generation(
        db_session,
        generation.id,
        purchase_payload={"rows": []},
        production_payload={"rows": [], "meta": {"row_count": 0}},
        mrp_payloads={str(run.run_id): _mrp_empty_payload(run.run_id)},
        period_payloads={f"plan:{plan.id}:run:{run.run_id}": payload},
    )
    db_session.commit()
    assert retry["period_plan_execution"].idempotent is True
    assert db_session.query(models.CurrentExecutionChange).count() == before_changes
    assert db_session.query(models.CurrentExecutionScope).count() == before_scopes


def test_period_runtime_source_has_no_legacy_snapshot_lookup():
    import inspect
    from app.services import period_plan_service

    current_source = inspect.getsource(current_execution._publish_current_obligation_views)
    service_source = "\n".join(
        inspect.getsource(getattr(period_plan_service, name))
        for name in (
            "get_period_plan_execution_journal",
            "_saved_plan_output_payload",
            "_read_period_plan_execution_payload_for_run",
            "close_fixed_plan",
        )
    )
    assert "PlanningReadSnapshot" not in current_source
    assert "PlanningReadSnapshot" not in service_source
    assert "get_latest_read_snapshot" not in service_source


def test_mrp_repair_selection_accepts_empty_and_multi_run_current_manifest(monkeypatch):
    from types import SimpleNamespace
    from app.services import period_plan_service

    scope = SimpleNamespace(
        source_generation_id=17,
        summary={"runs": {"41": {"row_counts": {}}, "42": {"row_counts": {}}}},
    )
    monkeypatch.setattr(
        current_execution,
        "load_current_execution_coherent",
        lambda *_args, **_kwargs: (scope, []),
    )

    assert period_plan_service._has_mrp_result_current(None, 41, 17) is True
    assert period_plan_service._has_mrp_result_current(None, 42, 17) is True
    assert period_plan_service._has_mrp_result_current(None, 99, 17) is False
    assert period_plan_service._has_mrp_result_current(None, 41, 18) is False
