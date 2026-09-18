from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_rows,
    publish_current_obligation_views_from_generation,
    require_current_execution_scope,
)
from app.routers.production_control import (
    OrdersFromWorkItemsPayload,
    get_orders_journal,
    list_root_products,
    get_order_line_materials,
    post_orders_from_work_items,
)
from app.routers.purchase_control import (
    PurchaseControlSelectionSummaryRequest,
    summarize_purchase_control_selection,
)


def _accepted_generation(db_session):
    batch = models.PhysicalImportBatch(
        batch_key="r9-obligation-views-batch",
        status="completed",
        cutoff=datetime(2026, 9, 10, tzinfo=timezone.utc),
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r9-obligation-views-generation",
        status="accepted",
        cutoff=batch.cutoff,
        source_watermarks={},
        capabilities={"physical_ledger": True, "reservation_replay": True, "assembly_queue": True},
        physical_import_batch=batch,
        algorithm_version="r9-test",
        replay_version="r9-test",
        accepted_at=batch.cutoff,
    )
    db_session.add(generation)
    db_session.flush()
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=generation.id))
    db_session.flush()
    return generation


def _snapshot(db_session, generation, *, consumer, key, rows, meta=None):
    """Register a direct candidate payload for the current publisher."""
    payload = {
        "rows": [
            ({"current_identity": str(row["row_key"])} if consumer != "production_control_journal" else {})
            | {"payload": dict(row["payload"])}
            for row in rows
        ],
        "meta": dict(meta or {}),
    }
    db_session.info.setdefault("r9_current_payloads", {})[consumer] = payload
    from types import SimpleNamespace

    return SimpleNamespace(consumer=consumer, payload=payload)


def _publish_current(db_session, generation):
    """Publish fixture candidates directly through the canonical owner."""
    payloads = db_session.info.get("r9_current_payloads", {})
    empty = {"rows": [], "meta": {"read_only": True, "fact_source": "ledger"}}
    purchase = payloads.get("purchase_control_journal", empty)
    production = payloads.get("production_control_journal", empty)
    return publish_current_obligation_views_from_generation(
        db_session,
        generation.id,
        purchase_payload=purchase,
        production_payload=production,
        mrp_payloads={},
        period_payloads={},
    )


def test_r9_publishes_obligation_and_fact_views_to_stable_current_owner(db_session):
    generation = _accepted_generation(db_session)
    _snapshot(
        db_session,
        generation,
        consumer="production_control_journal",
        key="journal:v1",
        rows=[{"row_key": "order:1", "payload": {
            "journal_row_key": "order:1", "item_id": 10,
            "quantity": 4, "produced_qty": 1, "remaining_qty": 3,
            "status": "open", "available_actions": ["produce"],
        }}],
    )
    _snapshot(
        db_session,
        generation,
        consumer="purchase_control_journal",
        key="journal:v1",
        rows=[{"row_key": "buy:1", "payload": {
            "row_key": "buy:1", "item_id": 11, "required_qty": 5,
            "to_order_qty": 2, "line_status": "to_order",
        }}],
    )
    db_session.commit()

    result = _publish_current(db_session, generation)
    db_session.commit()

    assert result["production_control_journal"].changed_rows == 1
    assert result["purchase_control_journal"].changed_rows == 1
    production = load_current_execution_rows(
        db_session, entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    purchase = load_current_execution_rows(
        db_session, entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )
    assert production[0].business_identity == "order:1"
    assert production[0].payload["available_actions"] == ["produce"]
    assert purchase[0].business_identity == "buy:1"
    assert purchase[0].payload["to_order_qty"] == 2


def test_r9_republishing_same_business_rows_is_idempotent(db_session):
    generation = _accepted_generation(db_session)
    _snapshot(
        db_session, generation, consumer="purchase_control_journal", key="journal:v1",
        rows=[{"row_key": "buy:1", "payload": {"row_key": "buy:1", "to_order_qty": 2}}],
    )
    db_session.commit()
    _publish_current(db_session, generation)
    db_session.commit()
    first = load_current_execution_rows(
        db_session, entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )[0]
    changes_before = db_session.query(models.CurrentExecutionChange).count()

    second = _publish_current(db_session, generation)
    db_session.commit()
    current = load_current_execution_rows(
        db_session, entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )[0]
    assert second["purchase_control_journal"].idempotent
    assert current.id == first.id
    assert db_session.query(models.CurrentExecutionChange).count() == changes_before


def test_r9_current_obligation_scope_is_fail_closed_when_not_published(db_session):
    generation = _accepted_generation(db_session)
    db_session.commit()
    with pytest.raises(CurrentExecutionUnavailable):
        require_current_execution_scope(
            db_session,
            entity_kind="purchase_control_journal",
            scope_key="purchase:all-live-plans",
        )


def test_r9_production_route_does_not_fallback_to_legacy_snapshot(db_session):
    generation = _accepted_generation(db_session)
    _snapshot(
        db_session,
        generation,
        consumer="production_control_journal",
        key="journal:v1",
        rows=[{"row_key": "order:legacy", "payload": {"journal_row_key": "order:legacy"}}],
    )
    db_session.commit()
    with pytest.raises(Exception) as caught:
        get_orders_journal(db=db_session)
    assert getattr(caught.value, "status_code", None) == 503
    assert "current" in str(getattr(caught.value, "detail", "")).lower()


def test_r9_production_current_identity_filters_before_pagination(db_session):
    generation = _accepted_generation(db_session)
    _snapshot(
        db_session,
        generation,
        consumer="production_control_journal",
        key="journal:v1",
        rows=[
            {"row_key": "order:1", "payload": {
                "journal_row_key": "order:1", "item_id": 10, "order_number": "001",
                "order_source": "mrp", "source": "MRP", "item_code": "I-10",
                "item_name": "First", "item_article": "A-10", "unit": "шт",
                "quantity": 4, "produced_qty": 0, "remaining_qty": 4, "status": "open",
                "coverage_status": "unavailable", "coverage_label": "Нет",
                "issue_status": "not_issued", "issue_count": 0, "comment": "",
            }},
            {"row_key": "order:2", "payload": {
                "journal_row_key": "order:2", "item_id": 11, "order_number": "002",
                "order_source": "mrp", "source": "MRP", "item_code": "I-11",
                "item_name": "Second", "item_article": "A-11", "unit": "шт",
                "quantity": 5, "produced_qty": 0, "remaining_qty": 5, "status": "open",
                "coverage_status": "unavailable", "coverage_label": "Нет",
                "issue_status": "not_issued", "issue_count": 0, "comment": "",
            }},
        ],
    )
    db_session.commit()
    _publish_current(db_session, generation)
    db_session.commit()

    result = get_orders_journal(
        current_identity="order:2", planning_contour=None, launch_source=None,
        limit=1, offset=0, db=db_session,
    )

    assert result.total == 1
    assert result.rows[0].current_identity == "order:2"
    assert result.rows[0].item_id == 11

    missing = get_orders_journal(
        current_identity="order:missing", planning_contour=None, launch_source=None,
        limit=1, offset=0, db=db_session,
    )
    assert missing.total == 0
    assert missing.rows == []


def test_r9_production_get_resolves_work_item_through_stable_reservation_owner(
    db_session,
):
    old_generation = _accepted_generation(db_session)
    target = models.LedgerGeneration(
        generation_key="r9-production-current-target",
        status="accepted",
        cutoff=old_generation.cutoff,
        source_watermarks={"parent_generation_id": old_generation.id},
        capabilities=dict(old_generation.capabilities or {}),
        algorithm_version="r9-test-target",
        replay_version="r9-test-target",
        accepted_at=old_generation.cutoff,
        physical_import_batch_id=old_generation.physical_import_batch_id,
    )
    db_session.add(target)
    db_session.flush()
    db_session.get(models.PlanningTruthState, 1).current_generation_id = target.id
    item = models.Item(item_code="R9-CURRENT-WORK", item_name="Current work", status="active")
    plan = models.ProductionPlanHeader(
        name="R9 current work plan", status="fixed",
        period_from=old_generation.cutoff.date(), period_to=old_generation.cutoff.date(),
    )
    db_session.add_all([item, plan])
    db_session.flush()
    run = models.PlanningRun(
        status="FIXED_SNAPSHOT", source_plan_id=plan.id,
        ledger_generation_id=old_generation.id,
        period_from=plan.period_from, period_to=plan.period_to,
        active_freeze_version=1,
    )
    db_session.add(run)
    db_session.flush()
    requirement = models.MrpRequirement(
        run_id=run.run_id, item_id=item.item_id,
        total_required_qty=5, net_required_qty=5,
        period_from=plan.period_from, period_to=plan.period_to,
        bom_level=0, freeze_version=1,
    )
    db_session.add(requirement)
    db_session.flush()
    reservation = models.ReservationEntry(
        ledger_generation_id=target.id, item_id=item.item_id, run_id=run.run_id,
        requirement_id=requirement.id, priority_period_from=plan.period_from,
        priority_period_to=plan.period_to, realization_mode="make",
        reserved_qty=5, replenishment_required_qty=5,
        owner_kind="current", is_current=True,
        current_identity=f"reservation:req:{requirement.id}:mode:make",
    )
    db_session.add(reservation)
    db_session.flush()
    old_work = models.ReplenishmentWorkItem(
        ledger_generation_id=old_generation.id, reservation_id=reservation.id,
        plan_id=plan.id, run_id=run.run_id, requirement_id=requirement.id,
        item_id=item.item_id, replenishment_method="make",
        replenishment_required_qty=5, replenishment_remaining_qty=5,
    )
    db_session.add(old_work)
    db_session.flush()
    revision = "accepted:r9-current-target"
    db_session.add(models.CurrentExecutionScope(
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
        source_revision=revision, source_generation_id=target.id,
        result_ready=True, content_hash="a" * 64, summary={},
    ))
    db_session.add(models.CurrentExecutionRow(
        entity_kind="production_control_journal",
        business_identity="mrp-reservation:stable",
        scope_key="production:all-live-orders",
        source_revision=revision, source_generation_id=target.id,
        result_status="accepted", result_ready=True, content_hash="b" * 64,
        payload={
            "journal_row_key": "work-item:stable", "item_id": item.item_id,
            "source_mrp_requirement_id": requirement.id,
            "source_mrp_allocation_key": "stable", "order_source": "mrp",
            "source": "mrp", "quantity": 5, "remaining_qty": 5,
            "order_number": "MRP-STABLE", "item_code": item.item_code,
            "item_name": item.item_name, "item_article": "R9-ARTICLE",
            "unit": "шт", "produced_qty": 0,
            "status": "shortage", "coverage_status": "shortage",
            "coverage_label": "Дефицит", "issue_status": "not_issued",
            "issue_count": 0, "comment": "",
        },
    ))
    db_session.commit()

    result = get_orders_journal(
        planning_contour=None, launch_source=None, db=db_session
    )
    assert result.total == 1
    assert result.rows[0].work_item_id == old_work.id
    with pytest.raises(Exception) as caught:
        post_orders_from_work_items(
            OrdersFromWorkItemsPayload(
                work_item_ids=[old_work.id + 1000],
                current_identities=["mrp-reservation:stable"],
                expected_source_revision=revision,
            ),
            db=db_session,
        )
    assert getattr(caught.value, "status_code", None) == 409


def test_r9_root_products_read_current_production_rows(db_session):
    generation = _accepted_generation(db_session)
    _snapshot(
        db_session,
        generation,
        consumer="production_control_journal",
        key="journal:v1",
        rows=[{"row_key": "order:root", "payload": {
            "journal_row_key": "order:root", "item_id": 10,
            "item_name": "Root", "item_article": "R-10", "item_code": "10",
        }}],
        meta={"root_product_options": [{
            "item_id": 10, "item_name": "Canonical Root", "item_article": "ROOT-10", "item_code": "ROOT",
        }]},
    )
    db_session.commit()
    _publish_current(db_session, generation)
    db_session.commit()

    result = list_root_products(db=db_session)
    assert result == {
        "rows": [{
            "item_id": 10, "item_name": "Canonical Root", "item_article": "ROOT-10", "item_code": "ROOT",
        }],
        "total": 1,
    }


def test_r9_materials_read_current_payload_without_snapshot_fallback(db_session):
    generation = _accepted_generation(db_session)
    _snapshot(
        db_session,
        generation,
        consumer="production_control_journal",
        key="journal:v1",
        rows=[{"row_key": "order:material", "payload": {
            "journal_row_key": "order:material", "product_id": 77, "item_id": 10,
            "material_coverage_snapshot": {
                "ledger_generation_id": generation.id,
                "components": [{"item_id": 20, "required_qty": 2}],
            },
        }}],
    )
    db_session.commit()
    _publish_current(db_session, generation)
    db_session.commit()

    result = get_order_line_materials(77, db=db_session)
    assert result["components"][0]["item_id"] == 20


def test_r9_materials_missing_current_manifest_returns_503(db_session):
    generation = _accepted_generation(db_session)
    _snapshot(
        db_session,
        generation,
        consumer="production_control_journal",
        key="journal:v1",
        rows=[{"row_key": "legacy:material", "payload": {
            "product_id": 77,
            "material_coverage_snapshot": {"components": []},
        }}],
    )
    db_session.commit()
    with pytest.raises(Exception) as caught:
        get_order_line_materials(77, db=db_session)
    assert getattr(caught.value, "status_code", None) == 503


def test_r9_production_proposal_identity_survives_new_technical_generation(db_session):
    first = _accepted_generation(db_session)
    payload = {
        "journal_row_key": "work-item:701",
        "work_item_id": 701,
        "source_mrp_requirement_id": 900,
        "source_mrp_allocation_key": "alloc:A",
        "item_id": 10,
        "quantity": 5,
        "remaining_qty": 5,
    }
    _snapshot(
        db_session, first, consumer="production_control_journal", key="journal:v1",
        rows=[{"row_key": "work-item:701", "payload": payload}],
    )
    db_session.commit()
    _publish_current(db_session, first)
    db_session.commit()
    first_row = load_current_execution_rows(
        db_session, entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )[0]
    first_row_id = first_row.id
    first_updated_at = first_row.updated_at
    changes_before = db_session.query(models.CurrentExecutionChange).count()

    second_batch = models.PhysicalImportBatch(
        batch_key="r9-obligation-views-batch-2",
        status="completed",
        cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc),
        source_watermarks={},
    )
    second = models.LedgerGeneration(
        generation_key="r9-obligation-views-generation-2",
        status="accepted",
        cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc),
        source_watermarks={},
        capabilities={"physical_ledger": True, "reservation_replay": True, "assembly_queue": True},
        physical_import_batch=second_batch,
        algorithm_version="r9-test",
        replay_version="r9-test",
        accepted_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    db_session.add(second)
    db_session.flush()
    db_session.query(models.PlanningTruthState).filter(models.PlanningTruthState.id == 1).update(
        {"current_generation_id": second.id}
    )
    second_payload = {**payload, "journal_row_key": "work-item:702", "work_item_id": 702}
    _snapshot(
        db_session, second, consumer="production_control_journal", key="journal:v1",
        rows=[{"row_key": "work-item:702", "payload": second_payload}],
    )
    db_session.commit()
    _publish_current(db_session, second)
    db_session.commit()

    current = load_current_execution_rows(
        db_session, entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )[0]
    assert current.id == first_row_id
    assert current.business_identity == "production-mrp-requirement:900:alloc:A"
    assert current.updated_at == first_updated_at
    assert "work_item_id" not in (current.payload or {})
    assert db_session.query(models.CurrentExecutionChange).count() == changes_before


def test_r9_purchase_selection_uses_current_manifest_revision_and_identity(db_session):
    generation = _accepted_generation(db_session)
    _snapshot(
        db_session,
        generation,
        consumer="purchase_control_journal",
        key="journal:v1",
        rows=[],
    )
    payload = db_session.info["r9_current_payloads"]["purchase_control_journal"]
    payload["rows"] = [
        {"current_identity": "buy:1", "payload": {
            "row_key": "buy:1", "item_id": 10, "line_status": "to_order",
            "to_order_qty": 3, "amount": 12, "price": 4,
            "row_generator": "mrp_reservation",
        }},
        {"current_identity": "buy:2", "payload": {
            "row_key": "buy:2", "item_id": 11, "line_status": "to_order",
            "to_order_qty": 2, "amount": 10, "price": 5,
            "row_generator": "mrp_reservation",
        }},
    ]
    payload["meta"] = {"summary": {"total": 1}}
    db_session.commit()
    _publish_current(db_session, generation)
    db_session.commit()
    manifest = require_current_execution_scope(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )
    identities = [row.business_identity for row in load_current_execution_rows(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )]

    result = summarize_purchase_control_selection(
        PurchaseControlSelectionSummaryRequest(
            current_identities=identities,
            expected_source_revision=manifest.source_revision,
        ),
        db=db_session,
    )
    assert result["selected_rows"] == 2
    assert result["current_identities"] == identities
    assert result["source_revision"] == manifest.source_revision

    with pytest.raises(Exception) as stale:
        summarize_purchase_control_selection(
            PurchaseControlSelectionSummaryRequest(
                current_identities=identities,
                expected_source_revision="accepted:stale",
            ),
            db=db_session,
        )
    assert getattr(stale.value, "status_code", None) == 409

    with pytest.raises(Exception) as legacy:
        summarize_purchase_control_selection(
            PurchaseControlSelectionSummaryRequest(
                snapshot_id=999999,
                row_keys=["buy:1"],
            ),
            db=db_session,
        )
    assert getattr(legacy.value, "status_code", None) == 409


def test_r9_technical_snapshot_ids_do_not_churn_current_identity(db_session):
    first = _accepted_generation(db_session)
    _snapshot(
        db_session, first, consumer="purchase_control_journal", key="journal:v1",
        rows=[{"row_key": "buy:stable", "payload": {"row_key": "buy:stable", "to_order_qty": 2}}],
    )
    db_session.commit()
    _publish_current(db_session, first)
    db_session.commit()
    current_id = load_current_execution_rows(
        db_session, entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )[0].id
    changes_before = db_session.query(models.CurrentExecutionChange).count()

    second = models.LedgerGeneration(
        generation_key="r9-obligation-views-generation-2",
        status="accepted",
        cutoff=datetime(2026, 9, 11, tzinfo=timezone.utc),
        source_watermarks={},
        capabilities=first.capabilities,
        physical_import_batch=first.physical_import_batch,
        algorithm_version="r9-test",
        replay_version="r9-test",
        accepted_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )
    db_session.add(second)
    db_session.flush()
    db_session.get(models.PlanningTruthState, 1).current_generation_id = second.id
    _snapshot(
        db_session, second, consumer="purchase_control_journal", key="journal:v1",
        rows=[{"row_key": "buy:stable", "payload": {"row_key": "buy:stable", "to_order_qty": 2}}],
    )
    db_session.commit()
    result = _publish_current(db_session, second)
    db_session.commit()
    current = load_current_execution_rows(
        db_session, entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )[0]
    assert result["purchase_control_journal"].idempotent
    assert current.id == current_id
    assert db_session.query(models.CurrentExecutionChange).count() == changes_before


def test_r9_missing_business_identity_fails_closed(db_session):
    generation = _accepted_generation(db_session)
    _snapshot(
        db_session, generation, consumer="purchase_control_journal", key="journal:v1",
        rows=[{"row_key": "technical-row-only", "payload": {"to_order_qty": 2}}],
    )
    db_session.commit()
    with pytest.raises(CurrentExecutionUnavailable, match="business identity"):
        _publish_current(db_session, generation)
