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


@pytest.mark.integration
def test_a17_runs_two_rehearsals_and_compares_normalized_subject_state(tmp_path):
    from tools.r11_local_acceptance import run_storage_rehearsal_pair

    assert callable(run_storage_rehearsal_pair)
    # The integration branch is intentionally guarded, but the contract is
    # functional: the helper must return a deterministic comparison payload.
    dsn = __import__("os").getenv("PRODPLAN_R2_TEST_DSN")
    if not dsn:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    report = run_storage_rehearsal_pair(dsn, output_dir=tmp_path)
    assert report["equivalent"] is True
    assert report["normalized_subject_state"] == report["runs"][0]["normalized_subject_state"]
    assert report["normalized_subject_state"] == report["runs"][1]["normalized_subject_state"]
    assert report["runs"][0]["schema"] != report["runs"][1]["schema"]


def test_a17_matrix_points_to_the_pair_rehearsal_node():
    nodes = _budget()["acceptance_matrix"]["A17"]["nodes"]
    assert "tests/r11/test_r11_final_acceptance_contract.py::test_a17_runs_two_rehearsals_and_compares_normalized_subject_state" in nodes


def test_compare_budget_fails_closed_for_missing_or_non_numeric_api_p95():
    from tools.r11_local_acceptance import compare_budget

    missing = compare_budget({"current_publish_p95_ms": 1.0}, budget_path=BUDGET_PATH)
    assert missing["within_budget"] is False
    assert "api_p95_ms" in missing["missing_metrics"]
    with pytest.raises(ValueError, match="api_p95_ms"):
        compare_budget({"api_p95_ms": "not-a-number", "current_publish_p95_ms": 1.0}, budget_path=BUDGET_PATH)
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="api_p95_ms"):
            compare_budget({"api_p95_ms": value, "current_publish_p95_ms": 1.0}, budget_path=BUDGET_PATH)


@pytest.mark.integration
def test_api_measurement_uses_real_r2_baseline_probe():
    from tools.r11_local_acceptance import measure_api_p95

    dsn = __import__("os").getenv("PRODPLAN_R2_TEST_DSN")
    if not dsn:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    report = measure_api_p95(dsn)
    assert report["api_sample_count"] == 9
    assert isinstance(report["api_p95_ms"], float)
    assert report["api_p95_ms"] >= report["api_p50_ms"]


def test_a12_matrix_names_contract_concurrency_and_playwright_nodes():
    evidence = _budget()["acceptance_matrix"]["A12"]
    nodes = evidence["nodes"]
    assert any("current_get_contract_matrix" in node for node in nodes)
    assert any("current_replenishment_transaction" in node or "postgresql" in node for node in nodes)
    assert any(node.endswith(".spec.ts::follows persisted plan → MRP → journal basis → assembly queue links") for node in nodes)


def test_all_matrix_nodes_resolve_to_existing_test_files():
    for evidence in _budget()["acceptance_matrix"].values():
        nodes = evidence.get("nodes", [evidence.get("node")])
        for node in nodes:
            path = node.split("::", 1)[0]
            assert (ROOT / path).is_file(), node


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
    assert categories["technical_debug"]["retention_days_after_closure"] == 30
    assert categories["migration_control_copy"]["retention_days_after_signoff"] == 90


def _planned_or_passed_matrix(**overrides):
    matrix = {
        key: {"status": "planned"}
        for key in _budget()["acceptance_matrix"]
    }
    matrix.update(overrides)
    return matrix


def test_final_evidence_requires_configured_nodes_and_passed_records():
    from tools.r11_local_acceptance import validate_final_evidence

    with pytest.raises(ValueError, match="node|outcome|data"):
        validate_final_evidence(
            {"matrix": _planned_or_passed_matrix(
                A01={"status": "passed", "results": [{"node": "wrong", "outcome": "passed"}]}
            )},
            budget_path=BUDGET_PATH,
        )


def test_final_evidence_requires_finite_budget_metrics_for_a18():
    from tools.r11_local_acceptance import validate_final_evidence

    node = _budget()["acceptance_matrix"]["A18"]["nodes"][0]
    with pytest.raises(ValueError, match="budget"):
        validate_final_evidence(
            {"matrix": _planned_or_passed_matrix(
                A18={"status": "passed", "results": [{"node": node, "command": "pytest", "outcome": "passed", "data": {"ok": True}}]}
            )},
            budget_path=BUDGET_PATH,
        )


def test_final_evidence_validator_is_fail_closed_for_unmeasured_matrix():
    from tools.r11_local_acceptance import validate_final_evidence

    with pytest.raises(ValueError, match="A01|evidence"):
        validate_final_evidence({"matrix": {"A01": {"status": "passed"}}}, budget_path=BUDGET_PATH)
