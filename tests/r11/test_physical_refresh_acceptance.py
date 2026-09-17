from __future__ import annotations

import pytest


def _evidence(**overrides):
    value = {
        "duration_ms": 120_000,
        "replayed_rows": 366,
        "input_delta_rows": 366,
        "affected_scopes": ["42::::selected:make"],
        "database_ledger_rows": 146_196,
    }
    value.update(overrides)
    return value


def test_physical_refresh_acceptance_requires_production_scale_and_budget():
    from tools.physical_refresh_acceptance import evaluate_physical_refresh_evidence

    accepted = evaluate_physical_refresh_evidence(_evidence())
    assert accepted["within_budget"] is True
    assert accepted["checks"]["production_scale"] is True

    undersized = evaluate_physical_refresh_evidence(
        _evidence(database_ledger_rows=99_999)
    )
    assert undersized["within_budget"] is False
    assert undersized["checks"]["production_scale"] is False

    replay_over_budget = evaluate_physical_refresh_evidence(
        _evidence(replayed_rows=10_001)
    )
    assert replay_over_budget["within_budget"] is False
    assert replay_over_budget["checks"]["replayed_rows"] is False


def test_physical_refresh_noop_requires_zero_replayed_rows():
    from tools.physical_refresh_acceptance import evaluate_physical_refresh_evidence

    result = evaluate_physical_refresh_evidence(
        _evidence(input_delta_rows=0, replayed_rows=1, affected_scopes=[])
    )
    assert result["within_budget"] is False
    assert result["checks"]["replayed_rows"] is False


def test_physical_refresh_acceptance_fails_closed_for_missing_or_malformed_evidence():
    from tools.physical_refresh_acceptance import evaluate_physical_refresh_evidence

    missing = evaluate_physical_refresh_evidence({})
    assert missing["within_budget"] is False
    with pytest.raises(ValueError, match="duration_ms"):
        evaluate_physical_refresh_evidence(_evidence(duration_ms=float("nan")))
    with pytest.raises(ValueError, match="affected_scopes"):
        evaluate_physical_refresh_evidence(_evidence(affected_scopes="scope"))

