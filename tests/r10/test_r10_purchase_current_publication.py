"""R10 purchase publication must own current state without read snapshots."""

from datetime import datetime, timezone

import pytest

from app import models
from app.services.item_ledger.current_execution import (
    CurrentExecutionUnavailable,
    get_current_execution_scope,
    load_current_execution_rows,
    publish_current_purchase_control_from_payload,
)


def _accepted_generation(db_session):
    imported = models.PhysicalImportBatch(
        batch_key="r10-purchase-direct-physical",
        status="completed",
        cutoff=datetime(2026, 9, 11, 8, tzinfo=timezone.utc),
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r10-purchase-direct-generation",
        status="accepted",
        cutoff=imported.cutoff,
        accepted_at=datetime(2026, 9, 11, 9, tzinfo=timezone.utc),
        source_watermarks={},
        capabilities={},
        physical_import_batch=imported,
        algorithm_version="tests/r10-purchase-direct",
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
            "fact_source": "ledger",
        },
        "rows": list(rows),
        "summary": {"total_rows": len(rows), "fact_status": "available"},
        "cards": {},
    }


def _row(identity="buy:req:101"):
    return {
        "row_key": identity,
        "current_identity": identity,
        "line_status": "to_order",
        "item_id": 501,
        "to_order_qty": 2,
        "fact_source": "ledger",
    }


def test_purchase_current_publisher_uses_candidate_payload_without_snapshot_rows(db_session):
    generation = _accepted_generation(db_session)

    result = publish_current_purchase_control_from_payload(
        db_session,
        generation.id,
        _payload(_row()),
    )
    db_session.flush()

    assert result.changed_rows == 1
    manifest = get_current_execution_scope(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )
    assert manifest is not None
    assert manifest.source_generation_id == generation.id
    assert manifest.source_revision == f"accepted:g{generation.id}:purchase_control_journal"
    assert load_current_execution_rows(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    )[0].business_identity == "buy:req:101"
    assert db_session.query(models.PlanningReadSnapshot).filter_by(
        consumer="purchase_control_journal",
        ledger_generation_id=generation.id,
    ).count() == 0


def test_purchase_current_publisher_supports_empty_scope_and_exact_retry_without_audit(
    db_session,
):
    generation = _accepted_generation(db_session)
    payload = _payload()

    first = publish_current_purchase_control_from_payload(db_session, generation.id, payload)
    db_session.flush()
    changes_after_first = db_session.query(models.CurrentExecutionChange).count()
    second = publish_current_purchase_control_from_payload(db_session, generation.id, payload)
    db_session.flush()

    assert first.changed_rows == 0
    assert second.idempotent is True
    assert changes_after_first == 0
    assert load_current_execution_rows(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    ) == []
    assert get_current_execution_scope(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    ).summary["total_rows"] == 0
    assert db_session.query(models.PlanningReadSnapshot).filter_by(
        consumer="purchase_control_journal",
        ledger_generation_id=generation.id,
    ).count() == 0


def test_purchase_current_publisher_rejects_duplicate_identity_before_dml(db_session):
    generation = _accepted_generation(db_session)
    duplicate_payload = _payload(_row(), _row())

    with pytest.raises(CurrentExecutionUnavailable, match="duplicate"):
        publish_current_purchase_control_from_payload(
            db_session,
            generation.id,
            duplicate_payload,
        )

    assert get_current_execution_scope(
        db_session,
        entity_kind="purchase_control_journal",
        scope_key="purchase:all-live-plans",
    ) is None
    assert db_session.query(models.CurrentExecutionRow).count() == 0
    assert db_session.query(models.PlanningReadSnapshot).count() == 0
