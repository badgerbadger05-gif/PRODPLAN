"""R11 A RED contract for the local generative/no-op acceptance harness."""

from __future__ import annotations

from decimal import Decimal
import importlib
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1].parent
BUDGET_PATH = ROOT / "config-test" / "r11_acceptance_budgets.json"
LOCAL_DSN = "postgresql://r2_user:r2_local_only@127.0.0.1:55444/prodplan_r2"


def _budget() -> dict:
    return json.loads(BUDGET_PATH.read_text(encoding="utf-8"))


def _runner():
    # The RED contract intentionally exercises the real runner entry point;
    # a missing import alone is not sufficient unless this call is functional.
    return importlib.import_module("tools.r11_local_acceptance")


def test_budget_is_machine_readable_and_fixed_before_measurement():
    document = _budget()
    assert document["schema_version"] == 1
    assert document["provenance"]["fixed_before_measurement"] is True
    assert document["provenance"]["historical_api_p95_ms"] == [3.8, 4.9]
    budgets = document["budgets"]
    assert budgets["no_op_repetitions"] == 100
    assert budgets["no_op_dml_max"] == 0
    assert budgets["no_op_current_row_growth_max"] == 0
    assert budgets["no_op_current_scope_growth_max"] == 0
    assert budgets["no_op_current_change_growth_max"] == 0
    assert budgets["no_op_wal_bytes_max"] == 0
    assert budgets["no_op_dead_tuple_growth_max"] == 0
    assert "measured" not in json.dumps(document).lower()


def test_acceptance_matrix_maps_all_a01_a18_without_unsupported_pass():
    matrix = _budget()["acceptance_matrix"]
    assert set(matrix) == {f"A{i:02d}" for i in range(1, 19)}
    for evidence in matrix.values():
        assert evidence["status"] in {"planned", "covered", "blocked"}
        assert evidence["status"] != "passed"
        assert "node" in evidence and "::" in evidence["node"]


def test_local_runner_plan_is_functional_and_secret_safe():
    runner = _runner()
    report = runner.run_local_acceptance(
        LOCAL_DSN,
        budget_path=BUDGET_PATH,
        dry_run=True,
    )
    assert report["status"] == "planned"
    assert report["budgets"]["no_op_repetitions"] == 100
    serialized = json.dumps(report, sort_keys=True)
    assert "r2_local_only" not in serialized
    assert "--password" not in serialized
    assert LOCAL_DSN not in serialized


def test_local_dsn_guard_contract_rejects_external_and_default_targets():
    runner = _runner()
    with pytest.raises(ValueError, match="local|R2|database"):
        runner.run_local_acceptance(
            "postgresql://r2_user:secret@db.example.invalid:5432/prodplan_r2",
            budget_path=BUDGET_PATH,
            dry_run=True,
        )
    with pytest.raises(ValueError, match="local|R2|database"):
        runner.run_local_acceptance(
            "postgresql://r2_user:secret@127.0.0.1:55444/postgres",
            budget_path=BUDGET_PATH,
            dry_run=True,
        )


def test_generators_use_independent_decimal_oracle():
    runner = _runner()

    def oracle(events: list[tuple[str, str]]) -> Decimal:
        balance = Decimal("0")
        for kind, quantity in events:
            value = Decimal(quantity)
            balance += value if kind == "receipt" else -value
        return balance

    sequence = [("receipt", "10.125"), ("issue", "2.125"), ("receipt", "1.000")]
    expected = oracle(sequence)
    report = runner.run_generators(
        seeds=(11, 29, 47),
        oracle_balance=str(expected),
        mutation_probe=True,
    )
    assert report["conservation"]["oracle_balance"] == str(expected)
    assert report["mutation_probes"]["sign_flip_detected"] is True
    assert report["mutation_probes"]["address_flip_detected"] is True
    assert report["mutation_probes"]["idempotency_flip_detected"] is True


def test_mutation_sequence_contract():
    runner = _runner()
    report = runner.run_mutation_sequence(
        seed=20260911,
        operations=("replacement", "retained", "closed", "semantic_change", "retry"),
    )
    assert report["final_retry"]["published"] is False
    assert report["retained_unchanged"] is True
    assert report["row_growth_within_budget"] is True
    assert report["audit_growth_matches_semantic_changes"] is True


def test_fault_injection_contract():
    runner = _runner()
    report = runner.run_fault_injection(
        fault_after_consumer="mrp_result",
        independent_reader=True,
    )
    assert report["rolled_back"] is True
    assert report["reader_saw_old_or_new_only"] is True
    assert report["accepted_pointer_switched"] is False


def test_runtime_inventory_contract():
    runner = _runner()
    report = runner.runtime_inventory()
    assert report["legacy_runtime_readers"] == []
    assert report["operational_legacy_sql"] == []


def test_report_compares_fixed_budgets():
    runner = _runner()
    report = runner.compare_budget(
        {"api_p95_ms": 0, "current_publish_p95_ms": 0},
        budget_path=BUDGET_PATH,
    )
    assert report["within_budget"] is True


@pytest.mark.integration
def test_no_op_publication_soak_contract():
    dsn = os.getenv("PRODPLAN_R2_TEST_DSN")
    if not dsn:
        pytest.skip("PRODPLAN_R2_TEST_DSN is not configured")
    report = _runner().run_local_acceptance(dsn, budget_path=BUDGET_PATH)
    no_op = report["no_op"]
    assert no_op["repetitions"] == 100
    assert no_op["dml_count"] == 0
    assert no_op["current_row_growth"] == 0
    assert no_op["current_scope_growth"] == 0
    assert no_op["current_change_growth"] == 0
    assert no_op["stable_scope_ids"] is True
    assert no_op["stable_row_ids"] is True
    assert no_op["wal_bytes"] <= _budget()["budgets"]["no_op_wal_bytes_max"]
    assert no_op["dead_tuple_growth"] <= _budget()["budgets"]["no_op_dead_tuple_growth_max"]
