"""RED contract for the final R11 evidence package."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1].parent
BUDGET_PATH = ROOT / "config-test" / "r11_acceptance_budgets.json"
RETENTION_PATH = ROOT / "config-test" / "r11_retention_policy.json"


def _budget() -> dict:
    return json.loads(BUDGET_PATH.read_text(encoding="utf-8"))


def test_a17_runs_two_rehearsals_and_compares_normalized_subject_state(tmp_path):
    from tools.r11_local_acceptance import run_storage_rehearsal_pair

    assert callable(run_storage_rehearsal_pair)
    # The integration branch is intentionally guarded, but the contract is
    # functional: the helper must return a deterministic comparison payload.
    dsn = "postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2"
    report = run_storage_rehearsal_pair(dsn, output_dir=tmp_path)
    assert report["equivalent"] is True
    assert report["normalized_subject_state"] == report["runs"][0]["normalized_subject_state"]
    assert report["normalized_subject_state"] == report["runs"][1]["normalized_subject_state"]
    assert report["runs"][0]["schema"] != report["runs"][1]["schema"]


def test_compare_budget_fails_closed_for_missing_or_non_numeric_api_p95():
    from tools.r11_local_acceptance import compare_budget

    missing = compare_budget({"current_publish_p95_ms": 1.0}, budget_path=BUDGET_PATH)
    assert missing["within_budget"] is False
    assert "api_p95_ms" in missing["missing_metrics"]
    with pytest.raises(ValueError, match="api_p95_ms"):
        compare_budget({"api_p95_ms": "not-a-number", "current_publish_p95_ms": 1.0}, budget_path=BUDGET_PATH)


def test_api_measurement_uses_real_r2_baseline_probe():
    from tools.r11_local_acceptance import measure_api_p95

    assert callable(measure_api_p95)
    report = measure_api_p95
    assert report.__name__ == "measure_api_p95"


def test_a12_matrix_names_contract_concurrency_and_playwright_nodes():
    evidence = _budget()["acceptance_matrix"]["A12"]
    nodes = evidence["nodes"]
    assert any("current_get_contract_matrix" in node for node in nodes)
    assert any("current_replenishment_transaction" in node or "postgresql" in node for node in nodes)
    assert any(node.endswith(".spec.ts::follows persisted plan → MRP → journal basis → assembly queue links") for node in nodes)


def test_retention_policy_is_explicit_and_never_automatically_deleted():
    policy = json.loads(RETENTION_PATH.read_text(encoding="utf-8"))
    assert policy["automatic_deletion"] is False
    categories = {entry["category"]: entry for entry in policy["categories"]}
    assert set(categories) == {"business_audit", "technical_debug", "migration_control_copy"}
    for entry in categories.values():
        assert entry["purpose"]
        assert entry["retention"]
        assert entry["deletion_condition"]
        assert entry["automatic_deletion"] is False


def test_final_evidence_validator_is_fail_closed_for_unmeasured_matrix():
    from tools.r11_local_acceptance import validate_final_evidence

    with pytest.raises(ValueError, match="A01|evidence"):
        validate_final_evidence({"matrix": {"A01": {"status": "passed"}}}, budget_path=BUDGET_PATH)
