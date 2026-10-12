from datetime import datetime, timezone

import pytest

from app import models
from app.services.item_ledger.current_execution import CurrentExecutionUnavailable
from app.services.item_ledger.current_execution import load_current_execution_mvcc_rows
from app.services.production_control_printing import load_current_route_sheet_rows


def _current_row(identity: str, product_id: int) -> models.CurrentExecutionRow:
    return models.CurrentExecutionRow(
        entity_kind="production_control_journal",
        business_identity=identity,
        scope_key="production:all-live-orders",
        source_revision="accepted:g1:test",
        result_status="accepted",
        result_ready=True,
        content_hash=f"hash-{product_id}",
        payload={
            "product_id": product_id,
            "route_sheet_payload": {
                "version": 1,
                "anchor_product_id": product_id,
                "sheet": {"product_id": product_id},
            },
        },
        manual_input={},
    )


def _accepted_generation(key: str) -> models.LedgerGeneration:
    cutoff = datetime(2026, 10, 12, 12, 43, tzinfo=timezone.utc)
    return models.LedgerGeneration(
        generation_key=key,
        status="accepted",
        cutoff=cutoff,
        source_watermarks={},
        capabilities={},
        physical_import_batch=models.PhysicalImportBatch(
            batch_key=f"batch-{key}",
            status="completed",
            cutoff=cutoff,
            source_watermarks={},
            completed_at=cutoff,
        ),
        algorithm_version="test/1",
        accepted_at=cutoff,
    )


def test_current_route_sheet_query_loads_only_requested_rows(db_session):
    db_session.add(
        models.CurrentExecutionScope(
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            source_revision="accepted:g2:current",
            content_hash="scope-hash",
            result_ready=True,
            summary={},
        )
    )
    requested = _current_row("production:requested", 19677)
    db_session.add(requested)
    db_session.add_all(
        _current_row(f"production:other:{index}", index)
        for index in range(200)
    )
    db_session.add(
        models.CurrentExecutionRow(
            entity_kind="production_control_journal",
            business_identity="production:closed",
            scope_key="production:all-live-orders",
            source_revision="accepted:g1:test",
            result_status="closed",
            result_ready=False,
            content_hash="hash-closed",
            payload={"product_id": 19677},
            manual_input={},
        )
    )
    db_session.flush()

    manifest, rows = load_current_route_sheet_rows(
        db_session,
        [" production:requested "],
        product_ids=[19677],
        expected_source_revision="accepted:g2:current",
    )

    assert manifest.source_revision == "accepted:g2:current"
    assert rows[0].source_revision == "accepted:g1:test"
    assert [row.business_identity for row in rows] == ["production:requested"]
    assert rows[0].payload["route_sheet_payload"]["sheet"]["product_id"] == 19677


def test_current_route_sheet_query_requires_identity_and_revision(db_session):
    with pytest.raises(CurrentExecutionUnavailable, match="identity and revision are required"):
        load_current_route_sheet_rows(
            db_session,
            ["", "   "],
            product_ids=[19677],
            expected_source_revision="accepted:g1:test",
        )


def test_current_route_sheet_query_rejects_stale_manifest_revision(db_session):
    db_session.add(
        models.CurrentExecutionScope(
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            source_revision="accepted:g2:current",
            content_hash="scope-hash",
            result_ready=True,
            summary={},
        )
    )
    db_session.add(_current_row("production:requested", 19677))
    db_session.flush()

    with pytest.raises(CurrentExecutionUnavailable, match="source revision is stale"):
        load_current_route_sheet_rows(
            db_session,
            ["production:requested"],
            product_ids=[19677],
            expected_source_revision="accepted:g1:test",
        )


def test_mvcc_current_read_accepts_a_ready_empty_scope(db_session):
    db_session.add(models.CurrentExecutionScope(
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
        source_revision="accepted:empty",
        content_hash="empty-hash",
        result_ready=True,
        summary={},
    ))
    db_session.flush()

    manifest, rows = load_current_execution_mvcc_rows(
        db_session,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
        business_identities=["production:missing"],
    )

    assert manifest.source_revision == "accepted:empty"
    assert rows == []


def test_mvcc_current_read_rejects_not_ready_and_pending_truth(db_session):
    scope = models.CurrentExecutionScope(
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
        source_revision="building:g2",
        content_hash="building-hash",
        result_ready=False,
        summary={},
    )
    db_session.add(scope)
    db_session.flush()

    with pytest.raises(CurrentExecutionUnavailable, match="manifest is not ready"):
        load_current_execution_mvcc_rows(
            db_session,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            business_identities=["production:requested"],
        )

    scope.result_ready = True
    db_session.add(models.PlanningTruthState(id=1, current_generation_id=None))
    with pytest.raises(CurrentExecutionUnavailable, match="pending truth move"):
        load_current_execution_mvcc_rows(
            db_session,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            business_identities=["production:requested"],
        )


def test_mvcc_current_read_rejects_scope_stale_for_truth_pointer(db_session):
    old_generation = _accepted_generation("route-old")
    current_generation = _accepted_generation("route-current")
    db_session.add_all([old_generation, current_generation])
    db_session.flush()
    db_session.add_all([
        models.PlanningTruthState(id=1, current_generation_id=current_generation.id),
        models.CurrentExecutionScope(
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            source_revision="accepted:old",
            source_generation_id=old_generation.id,
            content_hash="old-hash",
            result_ready=True,
            summary={},
        ),
    ])
    db_session.flush()

    with pytest.raises(CurrentExecutionUnavailable, match="stale for accepted truth"):
        load_current_execution_mvcc_rows(
            db_session,
            entity_kind="production_control_journal",
            scope_key="production:all-live-orders",
            business_identities=["production:requested"],
        )

