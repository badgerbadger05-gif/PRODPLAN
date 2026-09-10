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
from app.routers.production_control import get_orders_journal, list_root_products, get_order_line_materials


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
    snapshot = models.PlanningReadSnapshot(
        consumer=consumer,
        snapshot_key=key,
        ledger_generation_id=generation.id,
        cutoff=generation.cutoff,
        truth_status="accepted",
        payload={"rows": rows, **({"meta": meta} if meta is not None else {})},
        published_at=generation.cutoff,
    )
    db_session.add(snapshot)
    db_session.flush()
    for index, row in enumerate(rows):
        db_session.add(models.PlanningReadRow(
            snapshot_id=snapshot.id,
            row_key=str(row["row_key"]),
            row_kind=str(row.get("row_kind") or "result"),
            item_id=row.get("item_id"),
            sort_key=f"{index:08d}",
            payload=dict(row["payload"]),
        ))
    db_session.flush()
    return snapshot


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

    result = publish_current_obligation_views_from_generation(db_session, generation.id)
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
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()
    first = load_current_execution_rows(
        db_session, entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )[0]
    changes_before = db_session.query(models.CurrentExecutionChange).count()

    second = publish_current_obligation_views_from_generation(db_session, generation.id)
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
    snapshot = db_session.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == "production_control_journal",
    ).one()
    row = db_session.query(models.PlanningReadRow).filter(
        models.PlanningReadRow.snapshot_id == snapshot.id,
    ).one()
    db_session.add(models.PlanningReadRootMember(
        snapshot_id=snapshot.id, row_id=row.id, root_key="root:10", root_item_id=10,
    ))
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, generation.id)
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
    publish_current_obligation_views_from_generation(db_session, generation.id)
    db_session.commit()

    result = get_order_line_materials(77, db=db_session)
    assert result["components"][0]["item_id"] == 20


def test_r9_technical_snapshot_ids_do_not_churn_current_identity(db_session):
    first = _accepted_generation(db_session)
    _snapshot(
        db_session, first, consumer="purchase_control_journal", key="journal:v1",
        rows=[{"row_key": "buy:stable", "payload": {"row_key": "buy:stable", "to_order_qty": 2}}],
    )
    db_session.commit()
    publish_current_obligation_views_from_generation(db_session, first.id)
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
    result = publish_current_obligation_views_from_generation(db_session, second.id)
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
        publish_current_obligation_views_from_generation(db_session, generation.id)
