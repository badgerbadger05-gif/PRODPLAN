"""Direct purchase projection publication contracts."""

from datetime import datetime, timezone

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    get_current_execution_scope,
    load_current_execution_rows,
    publish_current_purchase_control_from_payload,
)
from app.services.purchase_control_projection import validate_purchase_control_journal_row


def _generation(db_session):
    cutoff = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
    physical = models.PhysicalImportBatch(
        batch_key="purchase-direct-physical", status="completed",
        cutoff=cutoff, source_watermarks={}, completed_at=cutoff,
    )
    generation = models.LedgerGeneration(
        physical_import_batch=physical, generation_key="purchase-direct-generation",
        status="accepted", cutoff=cutoff, accepted_at=cutoff,
        source_watermarks={}, capabilities={}, algorithm_version="tests/purchase-direct",
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def _payload(*rows):
    return {
        "meta": {"ledger_generation_id": 1, "truth_status": "accepted", "read_only": True},
        "rows": [
            {"current_identity": f"purchase:{index}", "payload": dict(row)}
            for index, row in enumerate(rows, 1)
        ],
        "cards": {},
        "summary": {"total_rows": len(rows)},
    }


def _buy_row(key="buy:1"):
    return {
        "row_generator": "mrp_reservation", "row_key": key,
        "required_qty": 12.0, "realized_qty": 4.0,
        "received_qty": 4.0, "open_order_covered_qty": 2.0,
        "to_order_qty": 6.0, "quantity": 12.0, "remaining_qty": 6.0,
        "reservation_ids": [1], "requirement_ids": [1],
    }


def test_direct_payload_publishes_current_rows_without_legacy_model(db_session):
    generation = _generation(db_session)
    result = publish_current_purchase_control_from_payload(
        db_session, generation.id, _payload(_buy_row())
    )
    scope = get_current_execution_scope(
        db_session, entity_kind="purchase_control_journal", scope_key="purchase:all-live-plans"
    )
    rows = load_current_execution_rows(
        db_session, entity_kind="purchase_control_journal", scope_key="purchase:all-live-plans"
    )
    assert scope is not None
    assert scope.source_generation_id == generation.id
    assert result.changed_rows == 1
    assert rows[0].business_identity == "purchase:1"
    assert db_session.query(models.CurrentExecutionScope).count() == 1


def test_empty_purchase_payload_is_a_valid_current_scope(db_session):
    generation = _generation(db_session)
    publish_current_purchase_control_from_payload(db_session, generation.id, _payload())
    scope = get_current_execution_scope(
        db_session, entity_kind="purchase_control_journal", scope_key="purchase:all-live-plans"
    )
    assert scope is not None and scope.result_ready is True
    assert load_current_execution_rows(
        db_session, entity_kind="purchase_control_journal", scope_key="purchase:all-live-plans"
    ) == []


def test_retry_is_idempotent_and_malformed_row_fails_before_publish(db_session):
    generation = _generation(db_session)
    payload = _payload(_buy_row())
    first = publish_current_purchase_control_from_payload(db_session, generation.id, payload)
    second = publish_current_purchase_control_from_payload(db_session, generation.id, payload)
    assert first.changed_rows == 1
    assert second.idempotent is True
    malformed = {"row_generator": "mrp_reservation"}
    with pytest.raises(ValueError):
        validate_purchase_control_journal_row(malformed)


def test_purchase_validator_remains_business_owned():
    with pytest.raises(ValueError, match="received_qty"):
        validate_purchase_control_journal_row({"row_generator": "mrp_reservation", "row_key": "buy:1"})
