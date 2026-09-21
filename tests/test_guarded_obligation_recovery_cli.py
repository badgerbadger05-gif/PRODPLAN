from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.scripts import guarded_obligation_recovery as driver
from app.services.obligation_refresh_orchestrator import ObligationRefreshOrchestrationResult


def test_source_inputs_are_canonical_and_reject_drift():
    source = driver.normalize_source_map({"571": 1713, "570": -1})
    fingerprints = driver.normalize_fingerprints({"570": ["b", "a", "a"], "571": ["c"]})
    assert source == {570: -1, 571: 1713}
    assert fingerprints == {570: ["a", "b"], 571: ["c"]}
    driver.validate_source_inputs((570, 571), source, fingerprints)
    with pytest.raises(driver.RecoveryDriverError, match="run set mismatch"):
        driver.validate_source_inputs((570, 571, 572), source, fingerprints)


def test_receipt_json_is_stable_and_does_not_contain_credentials(tmp_path):
    value = {"z": 1, "a": {"token": "must-not-be-added"}}
    path = tmp_path / "receipt.json"
    driver.write_json(path, value)
    assert json.loads(path.read_text()) == value
    assert driver.sha256_json(value) == driver.sha256_json({"a": value["a"], "z": 1})


def test_recovery_key_is_distinct_from_parent_key():
    owner_hash = driver.sha256_json({"570": ["fp"]})
    recovery_key = f"guarded-recovery:1713:{owner_hash[:24]}"
    assert recovery_key != "parent-generation-key"
    assert recovery_key.startswith("guarded-recovery:1713:")


def test_driver_result_contract_matches_service_dataclass():
    result = ObligationRefreshOrchestrationResult(10, 11, (570,), True)
    assert result.target_generation_id == 11
    assert result.published is True
    assert list(result.candidate_run_ids) == [570]


class _FakeSession:
    def __init__(self):
        self.calls = []

    def rollback(self):
        self.calls.append("rollback")

    def begin(self):
        self.calls.append("begin")

    def commit(self):
        self.calls.append("commit")


def test_execute_simulation_rolls_back_and_never_commits(monkeypatch):
    db = _FakeSession()
    receipt = {
        "parent_generation_id": 10,
        "recovery_generation_key": "guarded-recovery:10:test",
        "source_generation_by_run": {"1": -1},
        "owner_fingerprints": {"1": ["fp"]},
        "owner_fingerprint_sha256": driver.sha256_json({"1": ["fp"]}),
    }
    monkeypatch.setattr(driver, "verify_receipt", lambda _db, _receipt: ({1: -1}, {1: ["fp"]}))
    monkeypatch.setattr(
        driver,
        "run_guarded_obligation_recovery",
        lambda *_args, **_kwargs: SimpleNamespace(target_generation_id=11, published=True, candidate_run_ids=(1,)),
    )
    result = driver.execute(db, receipt, commit=False, started_by="test")
    assert result["status"] == "simulated_rolled_back"
    assert "commit" not in db.calls
    assert db.calls.count("rollback") >= 2


def test_execute_apply_commits_only_explicitly(monkeypatch):
    db = _FakeSession()
    receipt = {
        "parent_generation_id": 10,
        "recovery_generation_key": "guarded-recovery:10:test",
        "source_generation_by_run": {"1": -1},
        "owner_fingerprints": {"1": ["fp"]},
        "owner_fingerprint_sha256": driver.sha256_json({"1": ["fp"]}),
    }
    monkeypatch.setattr(driver, "verify_receipt", lambda _db, _receipt: ({1: -1}, {1: ["fp"]}))
    monkeypatch.setattr(
        driver,
        "run_guarded_obligation_recovery",
        lambda *_args, **_kwargs: SimpleNamespace(target_generation_id=11, published=True, candidate_run_ids=(1,)),
    )
    result = driver.execute(db, receipt, commit=True, started_by="operator")
    assert result["status"] == "applied"
    assert db.calls.count("commit") == 1


def test_service_exception_rolls_back_without_commit(monkeypatch):
    db = _FakeSession()
    receipt = {
        "parent_generation_id": 10,
        "recovery_generation_key": "guarded-recovery:10:test",
        "source_generation_by_run": {"1": -1},
        "owner_fingerprints": {"1": ["fp"]},
        "owner_fingerprint_sha256": driver.sha256_json({"1": ["fp"]}),
    }
    monkeypatch.setattr(driver, "verify_receipt", lambda _db, _receipt: ({1: -1}, {1: ["fp"]}))
    def fail(*_args, **_kwargs):
        raise RuntimeError("service failure")
    monkeypatch.setattr(driver, "run_guarded_obligation_recovery", fail)
    with pytest.raises(RuntimeError, match="service failure"):
        driver.execute(db, receipt, commit=True, started_by="test")
    assert "commit" not in db.calls
    assert db.calls.count("rollback") >= 2


def test_receipt_verification_failure_precedes_service_call(monkeypatch):
    db = _FakeSession()
    receipt = {"database_name": "expected"}
    called = []
    monkeypatch.setattr(driver, "database_name", lambda _db: "actual")
    monkeypatch.setattr(driver, "run_guarded_obligation_recovery", lambda *_a, **_k: called.append(True))
    with pytest.raises(driver.RecoveryDriverError, match="another database"):
        driver.verify_receipt(db, receipt)
    assert called == []
