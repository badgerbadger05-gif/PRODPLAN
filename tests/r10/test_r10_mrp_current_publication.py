"""R10 MRP result current-owner contract (RED first)."""

from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app.services import mrp_result_projection
from app.services.item_ledger import current_execution
from app import models
from app.services.obligation_refresh_publish import (
    ObligationRefreshPublishError,
    _require_mrp_current_payloads,
)


def test_direct_mrp_builder_returns_current_payload_without_snapshot_rows(monkeypatch):
    run = SimpleNamespace(
        run_id=42,
        status="FIXED_SNAPSHOT",
        ledger_generation_id=7,
        active_freeze_version=1,
    )

    class FakeDb:
        def get(self, model, key):
            return run

    monkeypatch.setattr(
        mrp_result_projection,
        "_collect_mrp_payload",
        lambda db, actual_run: (
            {"production": [{"item_id": 10, "agg_key": "item:10|start:2026-09-01|unit:шт", "qty": 2}],
             "purchase": [], "rework": [], "capacity": []},
            {"run_id": 42, "summary": {}, "row_counts": {"production": 1, "purchase": 0, "rework": 0, "capacity": 0}, "total_qty": {"production": 2.0}},
        ),
    )
    monkeypatch.setattr(mrp_result_projection, "_row_specs", lambda rows: [
        ("production:item:10:0", "production", 10, "2026-09-01|000000000010|000000000000", rows["production"][0]),
    ])
    monkeypatch.setattr(mrp_result_projection, "_frozen_root_membership", lambda *args, **kwargs: {10: {1}})

    payload = mrp_result_projection.build_mrp_result_current_payload(FakeDb(), 42)

    assert payload["run_id"] == 42
    assert payload["rows"][0]["current_identity"].startswith("mrp-run:42:production:")
    assert payload["rows"][0]["payload"]["root_item_ids"] == [1]
    assert payload["meta"]["row_count"] == 1


def test_mrp_current_publisher_rejects_duplicate_identity_before_dml():
    payload = {
        "42": {
            "run_id": 42,
            "row_counts": {"production": 2, "purchase": 0, "rework": 0, "capacity": 0},
            "rows": [
                {"current_identity": "same", "payload": {"row_kind": "production", "item_id": 1}},
                {"current_identity": "same", "payload": {"row_kind": "production", "item_id": 1}},
            ],
        }
    }

    with pytest.raises(ObligationRefreshPublishError, match="duplicate identity"):
        _require_mrp_current_payloads(payload, required_run_ids=[42])


def test_mrp_current_payloads_reject_foreign_run_before_publication():
    empty = {
        "run_id": 42,
        "row_counts": {"production": 0, "purchase": 0, "rework": 0, "capacity": 0},
        "rows": [],
    }
    foreign = {**empty, "run_id": 99}

    with pytest.raises(ObligationRefreshPublishError, match="extra.*99"):
        _require_mrp_current_payloads(
            {"42": empty, "99": foreign},
            required_run_ids=[42],
        )


def test_runtime_obligation_publisher_requires_explicit_mrp_payloads():
    with pytest.raises(current_execution.CurrentExecutionUnavailable, match="mrp_payloads"):
        current_execution.publish_current_obligation_views_from_generation(
            object(),
            1,
            purchase_payload={},
            production_payload={},
        )


def _accepted_generation(db_session):
    cutoff = datetime(2026, 9, 10, tzinfo=timezone.utc)
    batch = models.PhysicalImportBatch(
        batch_key="r10-mrp-current-batch", status="completed", cutoff=cutoff,
        source_watermarks={},
    )
    generation = models.LedgerGeneration(
        generation_key="r10-mrp-current-generation", status="accepted", cutoff=cutoff,
        source_watermarks={}, capabilities={}, physical_import_batch=batch,
        algorithm_version="r10", accepted_at=cutoff,
    )
    db_session.add(generation)
    db_session.flush()
    return generation


def test_direct_mrp_publisher_owns_scope_rows_and_retry_is_audit_free(db_session):
    generation = _accepted_generation(db_session)
    payloads = {
        "41": {
            "run_id": 41,
            "summary": {"planned": 7},
            "row_counts": {"production": 1, "purchase": 0, "rework": 0, "capacity": 0},
            "total_qty": {"production": 7.0},
            "rows": [{
                "current_identity": "mrp-run:41:production:item:10|start:2026-09-10|unit:шт",
                "payload": {
                    "run_id": 41, "row_kind": "production", "item_id": 10,
                    "qty": 7, "root_item_ids": [1], "sort_key": "2026-09-10|000000000010",
                },
            }],
        },
        "42": {
            "run_id": 42,
            "summary": {},
            "row_counts": {"production": 0, "purchase": 0, "rework": 0, "capacity": 0},
            "total_qty": {},
            "rows": [],
        },
    }
    first = current_execution.publish_current_mrp_results_from_payloads(
        db_session, generation.id, payloads
    )
    db_session.commit()
    before = db_session.query(models.CurrentExecutionChange).count()
    second = current_execution.publish_current_mrp_results_from_payloads(
        db_session, generation.id, payloads
    )
    db_session.commit()

    scope = current_execution.require_current_execution_scope(
        db_session, entity_kind="mrp_result", scope_key="mrp:all-live-plans"
    )
    rows = current_execution.load_current_execution_rows(
        db_session, entity_kind="mrp_result", scope_key="mrp:all-live-plans"
    )
    assert {row.payload["run_id"] for row in rows} == {41}
    assert rows[0].payload["root_item_ids"] == [1]
    assert first.changed_rows == 1
    assert second.changed_rows == 0
    assert db_session.query(models.PlanningReadSnapshot).filter_by(consumer="mrp_result").count() == 0
    assert db_session.query(models.PlanningReadRow).count() == 0
    assert before == db_session.query(models.CurrentExecutionChange).count()
    assert scope.summary["runs"]["42"]["row_counts"]["production"] == 0


def test_runtime_mrp_sources_do_not_call_legacy_snapshot_builder():
    from pathlib import Path

    root = Path(__file__).parents[2] / "backend" / "app" / "services"
    for relative in (
        "item_ledger/current_execution.py",
        "item_ledger/generation_lifecycle.py",
        "obligation_refresh_orchestrator.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "build_mrp_result_snapshot(" not in source
