"""R10 assembly queue current-owner contracts (RED first)."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def test_runtime_assembly_queue_writers_do_not_build_legacy_snapshots():
    lifecycle = (
        REPO / "backend/app/services/item_ledger/generation_lifecycle.py"
    ).read_text(encoding="utf-8")
    refresh = (
        REPO / "backend/app/services/obligation_refresh_orchestrator.py"
    ).read_text(encoding="utf-8")
    assert "build_assembly_queue_snapshot" not in lifecycle
    assert "build_assembly_queue_snapshot" not in refresh
    assert "assembly_queue_snapshot_id" not in lifecycle
    assert "assembly_queue_snapshot_id" not in refresh


def test_runtime_assembly_queue_publisher_uses_current_scope_without_legacy_rows():
    source = (
        REPO / "backend/app/services/item_ledger/current_execution.py"
    ).read_text(encoding="utf-8")
    start = source.index("def publish_current_execution_from_generation")
    runtime = source[start:source.index("def publish_current_purchase_control_from_payload", start)]
    assert "PlanningReadSnapshot" not in runtime
    assert "PlanningReadRow" not in runtime
    assert "publish_current_execution_scope" in runtime
