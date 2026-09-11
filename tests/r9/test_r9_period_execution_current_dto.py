"""R9 period-execution current DTO and read-only publication contracts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import models
from app.routers.plan import period_plans_execution_journal
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    _build_basis_links,
    _build_queue_links,
    _mrp_current_identity,
    _mrp_current_payload,
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
        "root_item_ids": [700],
        "information_links": {"reservation_events": []},
        "reservation_ids": [3],
        "execution_events": [{
            "event_id": 9,
            "reservation_id": 3,
            "stock_ledger_entry_id": 101,
            "event_kind": "realize",
        }],
        "ledger_links": {
            "item_id": 501,
            "reservation_ids": [3],
            "events": [{
                "event_id": 9,
                "reservation_id": 3,
                "sle_id": 101,
                "fact_ref": "FACT-1",
                "fact_line_ref": "1",
                "match_rule": "exact",
            }],
        },
        "facets": {"bom_levels": [0]},
        "work_items": [{
            "type": "planned_purchase",
            "qty": 5.0,
            "purchase_id": 701,
            "source_mrp_requirement_id": 101,
            "item_id": 501,
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
    db_session.add(models.PlanningReadSnapshot(
        consumer="mrp_result", snapshot_key="run:41:v1",
        ledger_generation_id=generation.id, cutoff=generation.cutoff,
        truth_status="accepted", payload={
            "summary": {"row_counts": {"purchase": 1}, "total_qty": {"purchase": 5}},
        }, published_at=generation.cutoff,
    ))
    db_session.flush()
    mrp_snapshot = db_session.query(models.PlanningReadSnapshot).filter_by(
        consumer="mrp_result", snapshot_key="run:41:v1",
    ).one()
    db_session.add(models.PlanningReadRow(
        snapshot_id=mrp_snapshot.id, row_key="req:101", row_kind="purchase",
        sort_key="2026-09-11|501", item_id=501,
        payload={"run_id": 41, "row_kind": "purchase", "req_id": 101, "item_id": 501, "qty": 5},
    ))
    db_session.commit()

    publish_current_execution_scope(
        db_session,
        source_revision="accepted:g-current:assembly_queue",
        source_generation_id=int(generation.id),
        scope_key="assembly:all-live-plans",
        rows=[{
            "entity_kind": "assembly_queue",
            "business_identity": "plan-line:900",
            "scope_key": "assembly:all-live-plans",
            "payload": {
                "plan_id": 7,
                "plan_line_id": 900,
                "item_id": 700,
                "run_id": 41,
                "period_from": "2026-09-11",
                "period_to": "2026-09-11",
                "assembly_remaining_qty": "4",
            },
        }],
        entity_kinds=("assembly_queue",),
        summary={"total_rows": 1, "total_queue_qty": "4"},
    )

    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()
    [current] = load_current_execution_rows(
        db_session, entity_kind="period_plan_execution",
        scope_key="period-plan:all-live-plans",
    )
    work_item = current.payload["work_items"][0]
    assert current.payload["current_identity"] == current.business_identity
    assert "source_revision" not in current.payload
    assert work_item["assigned_qty"] == 0.0
    assert work_item["unassigned_qty"] == 5.0
    [mrp_current] = load_current_execution_rows(
        db_session, entity_kind="mrp_result", scope_key="mrp:all-live-plans",
    )
    assert work_item["current_identity"] == mrp_current.business_identity
    assert work_item["navigation_href"]
    assert work_item["navigation_reason"] is None
    assert current.payload["basis_links"]["item"] == {
        "label": "Ledger item",
        "href": "#/ledger/items/501",
        "available": True,
        "reason": None,
    }
    assert current.payload["basis_links"]["reservations"] == [{
        "label": "Reservation #3",
        "href": "#/ledger/items/501?tab=reservations&reservation_id=3",
        "available": True,
        "reason": None,
    }]
    assert current.payload["basis_links"]["events"] == [{
        "label": "Ledger event #9",
        "href": "#/ledger/items/501?tab=reservations&reservation_id=3&event_id=9",
        "available": True,
        "reason": None,
    }]
    assert current.payload["queue_links"] == [{
        "label": "Assembly queue",
        "href": "#/production-control?view=assembly-queue&current_identity=plan-line%3A900",
        "available": True,
        "reason": None,
        "current_identity": "plan-line:900",
        "source_revision": "accepted:g-current:assembly_queue",
    }]
    assert current.payload["queue_link_reason"] is None


def test_period_navigation_links_disable_missing_ambiguous_and_mismatched_queue_targets():
    payload = {"plan_id": 7, "root_item_ids": [700]}
    manifest = SimpleNamespace(source_generation_id=11, source_revision="accepted:g11:assembly_queue")
    exact = SimpleNamespace(
        business_identity="plan-line:900",
        payload={"plan_id": 7, "plan_line_id": 900, "item_id": 700},
    )
    link, reason = _build_queue_links(payload, manifest, [exact], expected_generation_id=11)
    assert reason is None
    assert link[0]["available"] is True

    missing, reason = _build_queue_links(payload, manifest, [], expected_generation_id=11)
    assert missing[0]["available"] is False
    assert reason == "assembly queue current target is unavailable for this plan/root scope"

    ambiguous, reason = _build_queue_links(
        payload,
        manifest,
        [exact, SimpleNamespace(
            business_identity="plan-line:901",
            payload={"plan_id": 7, "plan_line_id": 901, "item_id": 700},
        )],
        expected_generation_id=11,
    )
    assert [link["current_identity"] for link in ambiguous] == [
        "plan-line:900", "plan-line:901"
    ]
    assert all(link["available"] for link in ambiguous)
    assert reason is None

    mismatched, reason = _build_queue_links(payload, manifest, [exact], expected_generation_id=12)
    assert mismatched[0]["available"] is False
    assert reason == "assembly queue current source generation does not match period execution source generation"


def test_basis_links_are_disabled_when_persisted_ledger_basis_is_missing():
    basis = _build_basis_links({"item_id": None, "reservation_ids": [], "events": []})
    assert basis["item"]["available"] is False
    assert basis["item"]["reason"] == "ledger item basis is unavailable"


def test_period_current_get_returns_persisted_summary_without_business_recalculation(
    db_session, monkeypatch,
):
    summary = {
        "truth_status": "accepted", "total_items": 1,
        "execution_pct": 0.0, "execution_available_base_qty": 5.0,
    }
    metadata = {
        "plan": {"id": 7, "name": "Plan 7"}, "run_id": 41,
        "summary": summary, "facets": {"bom_levels": [0]},
        "plan_output_rows": [], "truth_status": "accepted",
    }
    row = _period_row()
    row.pop("row_key", None)
    row.pop("facets", None)
    row["work_items"][0].pop("run_id", None)
    row["work_items"][0].pop("source_mrp_requirement_id", None)
    row["work_items"][0].pop("item_id", None)
    row["work_items"][0].update({
        "assigned_qty": 0.0,
        "unassigned_qty": 5.0,
        "current_identity": "mrp-run:41:purchase:requirement:101:item:501:allocation:default",
        "navigation_href": "#/mrp-runs/41?tab=purchases&current_identity=mrp-run%3A41%3Apurchase%3Arequirement%3A101%3Aitem%3A501%3Aallocation%3Adefault",
        "navigation_reason": None,
    })
    row["current_identity"] = "plan:7:req:101"
    row["source_revision"] = "accepted:g-current:period_plan_execution"
    row["basis_links"] = {
        "item": {
            "label": "Ledger item",
            "href": "#/ledger/items/501",
            "available": True,
            "reason": None,
        },
        "reservations": [],
        "events": [],
        "reason": None,
    }
    row["queue_links"] = [{
        "label": "Assembly queue",
        "href": "#/production-control?view=assembly-queue&current_identity=plan-line%3A900",
        "available": True,
        "reason": None,
        "current_identity": "plan-line:900",
        "source_revision": "accepted:g-current:assembly_queue",
    }]
    row["queue_link_reason"] = None
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
    assert response.facets == {"bom_levels": [0]}
    assert response.rows[0].status_label == "Не оформлено"
    assert response.rows[0].work_items[0].assigned_qty == 0.0
    assert response.rows[0].basis_links.item.href == "#/ledger/items/501"
    assert response.rows[0].queue_links[0].current_identity == "plan-line:900"
    assert response.rows[0].queue_links[0].source_revision == "accepted:g-current:assembly_queue"


def test_mrp_republish_technical_locators_do_not_leak_or_churn_current_owner(db_session):
    first = {
        "run_id": 41,
        "row_kind": "purchase",
        "source_mrp_requirement_id": 101,
        "item_id": 501,
        "source_mrp_allocation_key": "supplier:A",
        "qty": 5,
        "row_key": "purchase:701:0",
        "sort_key": "2026-09-11|501|000000",
        "purchase_id": 701,
        "order_id": 801,
    }
    second = {
        **first,
        "row_key": "purchase:902:7",
        "sort_key": "2026-09-11|501|000007",
        "purchase_id": 902,
        "order_id": 1002,
    }
    identity = _mrp_current_identity(first, run_id=41, row_kind="purchase")
    assert identity == _mrp_current_identity(second, run_id=41, row_kind="purchase")

    def publish(payload: dict[str, object], revision: str) -> None:
        publish_current_execution_scope(
            db_session,
            source_revision=revision,
            scope_key="mrp:republish-test",
            entity_kinds=("mrp_result",),
            rows=[{
                "entity_kind": "mrp_result",
                "business_identity": identity,
                "scope_key": "mrp:republish-test",
                "payload": _mrp_current_payload(payload, business_identity=identity),
            }],
        )

    publish(first, "accepted:g1:mrp_result")
    db_session.commit()
    current = load_current_execution_rows(
        db_session, entity_kind="mrp_result", scope_key="mrp:republish-test",
    )[0]
    first_id = int(current.id)
    first_updated_at = current.updated_at
    first_changes = db_session.query(models.CurrentExecutionChange).filter_by(
        current_row_id=first_id,
    ).count()
    assert "purchase_id" not in current.payload
    assert "order_id" not in current.payload

    publish(second, "accepted:g2:mrp_result")
    db_session.commit()
    db_session.expire_all()
    current = load_current_execution_rows(
        db_session, entity_kind="mrp_result", scope_key="mrp:republish-test",
    )[0]
    assert int(current.id) == first_id
    assert current.updated_at == first_updated_at
    assert db_session.query(models.CurrentExecutionChange).filter_by(
        current_row_id=first_id,
    ).count() == first_changes
    assert "purchase_id" not in current.payload
    assert "order_id" not in current.payload


def test_mrp_identity_fails_closed_without_semantic_owner_or_on_duplicate(db_session):
    with pytest.raises(CurrentExecutionUnavailable):
        _mrp_current_identity({"item_id": 501}, run_id=41, row_kind="purchase")

    payload = {
        "source_mrp_requirement_id": 101,
        "item_id": 501,
        "source_mrp_allocation_key": "supplier:A",
    }
    identity = _mrp_current_identity(payload, run_id=41, row_kind="purchase")
    with pytest.raises(CurrentExecutionUnavailable):
        publish_current_execution_scope(
            db_session,
            source_revision="accepted:g1:mrp_result",
            scope_key="mrp:duplicate-test",
            entity_kinds=("mrp_result",),
            rows=[
                {"entity_kind": "mrp_result", "business_identity": identity,
                 "scope_key": "mrp:duplicate-test",
                 "payload": _mrp_current_payload(payload, business_identity=identity)},
                {"entity_kind": "mrp_result", "business_identity": identity,
                 "scope_key": "mrp:duplicate-test",
                 "payload": _mrp_current_payload(payload, business_identity=identity)},
            ],
        )
