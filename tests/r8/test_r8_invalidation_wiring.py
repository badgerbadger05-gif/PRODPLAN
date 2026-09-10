from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_r8_reference_mutations_invalidate_affected_current_scopes():
    planning_rates = (ROOT / "backend/app/routers/planning_rates.py").read_text(encoding="utf-8")
    resources = (ROOT / "backend/app/routers/resources.py").read_text(encoding="utf-8")
    manual = (ROOT / "backend/app/services/item_ledger/drum_manual_move.py").read_text(encoding="utf-8")
    assert "invalidate_current_execution_scope" in planning_rates
    assert "invalidate_current_execution_scope" in resources
    assert "invalidate_current_execution_scope" in manual
    assert planning_rates.count('scope_key="shelf:all-live-mrps"') >= 3
    assert planning_rates.count('scope_key="drum:all-live-plans"') >= 2
    assert resources.count('scope_key="drum:all-live-plans"') >= 1
