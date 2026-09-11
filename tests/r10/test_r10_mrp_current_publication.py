"""R10 MRP result current-owner contract (RED first)."""

from types import SimpleNamespace

import pytest

from app.services import mrp_result_snapshot
from app.services.item_ledger import current_execution
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
        mrp_result_snapshot,
        "_collect_snapshot_payload",
        lambda db, actual_run: (
            {"production": [{"item_id": 10, "agg_key": "item:10|start:2026-09-01|unit:шт", "qty": 2}],
             "purchase": [], "rework": [], "capacity": []},
            {"run_id": 42, "summary": {}, "row_counts": {"production": 1, "purchase": 0, "rework": 0, "capacity": 0}, "total_qty": {"production": 2.0}},
        ),
    )
    monkeypatch.setattr(mrp_result_snapshot, "_row_specs", lambda rows: [
        ("production:item:10:0", "production", 10, "2026-09-01|000000000010|000000000000", rows["production"][0]),
    ])
    monkeypatch.setattr(mrp_result_snapshot, "_frozen_root_membership", lambda *args, **kwargs: {10: {1}})

    payload = mrp_result_snapshot.build_mrp_result_current_payload(FakeDb(), 42)

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


def test_runtime_obligation_publisher_requires_explicit_mrp_payloads():
    with pytest.raises(current_execution.CurrentExecutionUnavailable, match="mrp_payloads"):
        current_execution.publish_current_obligation_views_from_generation(
            object(),
            1,
            purchase_payload={},
            production_payload={},
        )
