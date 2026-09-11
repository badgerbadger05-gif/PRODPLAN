"""R10 D contract for operational legacy-path cleanup."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1].parent


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_operational_rebuild_sql_uses_current_owner_after_head():
    for relative in (
        "tools/sql/verify_ledger_rebuild.sql",
        "tools/sql/clear_rebuildable_ledger_projections.sql",
    ):
        source = _read(relative).lower()
        assert "planning_read_snapshot" not in source
        assert "planning_read_row" not in source
        assert "planning_read_root_member" not in source
    clear = _read("tools/sql/clear_rebuildable_ledger_projections.sql")
    verify = _read("tools/sql/verify_ledger_rebuild.sql")
    assert "current_execution_scope" in clear
    assert "current_execution_scope" in verify
    assert "current_execution_row" in verify
    backend_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "backend" / "app").rglob("*.py")
    ).lower()
    assert "planning_read_snapshot" not in backend_sources
    assert "planning_read_row" not in backend_sources
    assert "planning_read_root_member" not in backend_sources


def test_current_production_reader_has_no_legacy_snapshot_entrypoint():
    projection = _read("backend/app/services/production_control_journal_projection.py")
    router = _read("backend/app/routers/production_control.py")
    lifecycle = _read("backend/app/services/item_ledger/generation_lifecycle.py")
    assert "def read_snapshot(" not in projection
    assert "read_snapshot as read_production_control_journal_current" not in router
    assert "_promote_accepted_generation_read_snapshots" not in lifecycle


def test_fork_and_carry_are_retained_with_real_callers_and_tests():
    orchestrator = _read("backend/app/services/obligation_refresh_orchestrator.py")
    generation = _read("backend/app/services/item_ledger/obligation_generation.py")
    lifecycle = _read("backend/app/services/item_ledger/generation_lifecycle.py")
    physical = _read("backend/app/services/item_ledger/physical_refresh_orchestrator.py")
    future_supply = _read("backend/app/services/item_ledger/future_supply_capture.py")
    tests = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "tests" / "services" / "test_obligation_generation.py",
            ROOT / "tests" / "services" / "test_carry_forward_retained_reservations.py",
            ROOT / "tests" / "services" / "test_physical_refresh_orchestrator.py",
            ROOT / "tests" / "services" / "test_physical_refresh_future_supply.py",
        )
    )
    assert "fork_obligation_generation" in orchestrator
    assert "fork_obligation_generation(db" in orchestrator
    assert "carry_forward_retained_reservations" in orchestrator
    assert "def fork_obligation_generation" in generation
    assert "def carry_forward_retained_reservations" in generation
    assert "fork_obligation_generation" in tests
    assert "fork_physical_refresh_generation" in physical
    assert "carry_forward_future_supply" in lifecycle
    assert "carry_forward_future_supply_evidence" in future_supply
    assert "fork_physical_refresh_generation" in tests
    assert "carry_forward_future_supply" in tests


def test_active_inventory_names_current_projection_owner():
    inventory = _read("docs/r1-data-contract-inventory.md")
    assert "current_execution_scope.source_generation_id" in inventory
    assert "planning_read_row.snapshot_id" not in inventory
    assert "planning_read_root_member.snapshot_id" not in inventory
