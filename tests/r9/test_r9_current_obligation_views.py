from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    load_current_execution_rows,
    publish_current_obligation_views_from_generation,
    publish_current_obligation_views_from_snapshots,
    require_current_execution_scope,
)
from app.routers.production_control import get_orders_journal, list_root_products, get_order_line_materials
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


def _publish_current(db_session, generation):
    """Use direct payloads, adapting only immutable fixture evidence."""
    def _payload(snapshot):
        if snapshot is None:
            return {
                "rows": [],
                "meta": {
                    "ledger_generation_id": generation.id,
                    "truth_status": "accepted",
                    "read_only": True,
                    "fact_source": "ledger",
                },
            }
        payload = dict(snapshot.payload or {})
        rows = list(payload.get("rows") or [])
        if snapshot.consumer == "production_control_journal":
            rows = []
        for persisted in db_session.query(models.PlanningReadRow).filter(
            models.PlanningReadRow.snapshot_id == int(snapshot.id),
        ).order_by(models.PlanningReadRow.sort_key.asc(), models.PlanningReadRow.id.asc()).all():
            row = dict(persisted.payload or {})
            if snapshot.consumer == "production_control_journal":
                row["root_item_ids"] = [
                    int(member.root_item_id)
                    for member in db_session.query(models.PlanningReadRootMember).filter(
                        models.PlanningReadRootMember.snapshot_id == int(snapshot.id),
                        models.PlanningReadRootMember.row_id == int(persisted.id),
                    ).order_by(models.PlanningReadRootMember.root_item_id.asc()).all()
                ]
            if snapshot.consumer == "production_control_journal":
                rows.append(row)
        payload["rows"] = rows
        return payload

    purchase = db_session.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == "purchase_control_journal",
        models.PlanningReadSnapshot.snapshot_key == "journal:v1",
        models.PlanningReadSnapshot.ledger_generation_id == generation.id,
        models.PlanningReadSnapshot.truth_status == "accepted",
    ).one_or_none()
    production = db_session.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == "production_control_journal",
        models.PlanningReadSnapshot.snapshot_key == "journal:v1",
        models.PlanningReadSnapshot.ledger_generation_id == generation.id,
        models.PlanningReadSnapshot.truth_status == "accepted",
    ).one_or_none()
    return publish_current_obligation_views_from_generation(
        db_session,
        generation.id,
        purchase_payload=_payload(purchase),
        production_payload=_payload(production),
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
    snapshot = db_session.query(models.PlanningReadSnapshot).filter(
        models.PlanningReadSnapshot.consumer == "purchase_control_journal",
    ).one()
    snapshot.payload = {
        "rows": [{
            "row_key": "buy:1", "item_id": 10, "line_status": "to_order",
            "to_order_qty": 3, "amount": 12, "price": 4,
            "row_generator": "mrp_reservation",
        }, {
            "row_key": "buy:2", "item_id": 11, "line_status": "to_order",
            "to_order_qty": 2, "amount": 10, "price": 5,
            "row_generator": "mrp_reservation",
        }],
        "meta": {"summary": {"total": 1}},
    }
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
