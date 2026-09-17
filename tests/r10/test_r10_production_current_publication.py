"""R10 production journal current publication owns the runtime read model."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    _production_snapshot_identity,
    get_current_execution_scope,
    load_current_execution_rows,
    publish_current_production_control_from_payload,
)
from app.services.production_control_journal_projection import _candidate_business_identity


def _accepted_generation(db_session):
    imported = models.PhysicalImportBatch(
        batch_key="r10-production-direct-physical",
        status="completed",
        cutoff=datetime(2026, 9, 11, 8, tzinfo=timezone.utc),
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r10-production-direct-generation",
        status="accepted",
        cutoff=imported.cutoff,
        accepted_at=datetime(2026, 9, 11, 9, tzinfo=timezone.utc),
        source_watermarks={},
        capabilities={},
        physical_import_batch=imported,
        algorithm_version="tests/r10-production-direct",
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def _payload(*rows):
    return {
        "meta": {
            "ledger_generation_id": 1,
            "truth_status": "accepted",
            "read_only": True,
            "row_count": len(rows),
            "latest_run_id": 7,
            "root_product_options": [{"item_id": 900, "item_name": "Root"}],
        },
        "rows": list(rows),
        "summary": {"total_rows": len(rows), "fact_status": "available"},
    }


def _row(identity="production-order-line:42:501"):
    return {
        "current_identity": identity,
        "journal_row_key": "generation-local-row-1",
        "order_id": 42,
        "product_id": 501,
        "item_id": 501,
        "quantity": 4,
        "produced_qty": 1,
        "remaining_qty": 3,
        "status": "open",
        "root_item_ids": [900],
        "available_actions": ["produce"],
    }


def test_production_current_publisher_uses_structured_payload_without_snapshot_rows(
    db_session,
):
    generation = _accepted_generation(db_session)

    result = publish_current_production_control_from_payload(
        db_session, generation.id, _payload(_row())
    )
    db_session.flush()

    assert result.changed_rows == 1
    manifest = get_current_execution_scope(
        db_session,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    assert manifest is not None
    assert manifest.source_generation_id == generation.id
    assert manifest.source_revision == (
        f"accepted:g{generation.id}:production_control_journal"
    )
    current = load_current_execution_rows(
        db_session,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    assert current[0].business_identity == "production-order-line:42:501"
    assert current[0].payload["root_item_ids"] == [900]
    assert db_session.query(models.CurrentExecutionScope).count() == 1


def test_production_current_payload_drops_nested_generation_refs_and_retries_noop(
    db_session,
):
    generation = _accepted_generation(db_session)
    row = _row()
    row.update({
        "ledger_generation_id": generation.id,
        "material_coverage_snapshot": {
            "ledger_generation_id": generation.id,
            "source_generation_id": generation.id,
            "components": [{
                "item_id": 20,
                "generation_id": generation.id,
                "planning_read_snapshot_id": 991,
                "required_qty": 2,
            }],
            "nested": {
                "parent_generation_id": generation.id,
                "snapshot_id": 992,
                "current_generation_id": generation.id,
            },
        },
    })
    payload = _payload(row)

    first = publish_current_production_control_from_payload(
        db_session, generation.id, payload
    )
    db_session.flush()
    current = load_current_execution_rows(
        db_session,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    assert len(current) == 1
    stored = current[0].payload

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key
                yield from walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from walk(child)

    assert not set(walk(stored)) & {
        "snapshot_id",
        "planning_read_snapshot_id",
        "generation_id",
        "ledger_generation_id",
        "parent_generation_id",
        "source_generation_id",
        "truth_generation_id",
        "current_generation_id",
    }
    manifest = get_current_execution_scope(
        db_session,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    assert manifest.source_generation_id == generation.id
    changes = db_session.query(models.CurrentExecutionChange).count()

    second = publish_current_production_control_from_payload(
        db_session, generation.id, payload
    )
    db_session.flush()
    assert first.changed_rows == 1
    assert second.idempotent is True
    assert db_session.query(models.CurrentExecutionChange).count() == changes


def test_production_current_publisher_empty_retry_is_exact_and_audit_free(db_session):
    generation = _accepted_generation(db_session)
    payload = _payload()

    first = publish_current_production_control_from_payload(db_session, generation.id, payload)
    db_session.flush()
    changes = db_session.query(models.CurrentExecutionChange).count()
    second = publish_current_production_control_from_payload(db_session, generation.id, payload)
    db_session.flush()

    assert first.changed_rows == 0
    assert second.idempotent is True
    assert db_session.query(models.CurrentExecutionChange).count() == changes == 0
    assert load_current_execution_rows(
        db_session,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    ) == []


def test_production_current_publisher_rejects_duplicate_identity_before_dml(db_session):
    generation = _accepted_generation(db_session)

    with pytest.raises(CurrentExecutionUnavailable, match="duplicate"):
        publish_current_production_control_from_payload(
            db_session, generation.id, _payload(_row(), _row())
        )

    assert get_current_execution_scope(
        db_session,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    ) is None
    assert db_session.query(models.CurrentExecutionRow).count() == 0


def test_mrp_allocation_key_is_disambiguated_by_stable_product_identity(db_session):
    generation = _accepted_generation(db_session)
    allocation_key = "mrp_requirement:120520:order:1"
    residual = {
        "current_identity": f"production-mrp-requirement:120520:{allocation_key}",
        "source_mrp_requirement_id": 120520,
        "source_mrp_allocation_key": allocation_key,
        "order_id": 16117,
        "product_id": 17499,
        "item_id": 17499,
        "quantity": 540,
        "remaining_qty": 540,
        "root_item_ids": [17499],
    }
    exported = {
        **residual,
        "order_id": 16118,
        "product_id": 17500,
        "item_id": 17500,
        "quantity": 300,
        "remaining_qty": 0,
        "root_item_ids": [17500],
    }
    expected_residual = (
        "production-mrp-requirement:120520:"
        "mrp_requirement:120520:order:1:product:17499"
    )
    expected_exported = expected_residual.replace("17499", "17500")
    assert _production_snapshot_identity(residual) == expected_residual
    assert _production_snapshot_identity(exported) == expected_exported
    assert _candidate_business_identity(residual) == expected_residual
    assert _candidate_business_identity(exported) == expected_exported

    result = publish_current_production_control_from_payload(
        db_session,
        generation.id,
        _payload(residual, exported),
    )
    db_session.flush()
    assert result.changed_rows == 2
    current = load_current_execution_rows(
        db_session,
        entity_kind="production_control_journal",
        scope_key="production:all-live-orders",
    )
    assert {row.business_identity for row in current} == {
        expected_residual,
        expected_exported,
    }
    changes = db_session.query(models.CurrentExecutionChange).count()
    retry = publish_current_production_control_from_payload(
        db_session,
        generation.id,
        _payload(residual, exported),
    )
    db_session.flush()
    assert retry.idempotent is True
    assert db_session.query(models.CurrentExecutionChange).count() == changes


def test_runtime_production_publication_has_no_snapshot_fallback_or_builder_writer():
    repo = Path(__file__).resolve().parents[2]
    lifecycle = (
        repo / "backend/app/services/item_ledger/generation_lifecycle.py"
    ).read_text(encoding="utf-8")
    refresh = (
        repo / "backend/app/services/obligation_refresh_orchestrator.py"
    ).read_text(encoding="utf-8")
    current = (
        repo / "backend/app/services/item_ledger/current_execution.py"
    ).read_text(encoding="utf-8")
    assert "build_production_journal_candidate" not in lifecycle
    assert "build_production_journal_candidate" not in refresh
    assert "production_payload" in current
    runtime = current.split("def publish_current_obligation_views_from_generation", 1)[0]
    runtime = runtime.split("def _publish_current_obligation_views", 1)[1]
    assert 'consumer == "production_control_journal"' not in runtime
    assert "production.id" not in runtime
