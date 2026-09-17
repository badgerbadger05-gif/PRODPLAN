"""Fail-closed evidence evaluator for bounded physical refreshes.

The evaluator is intentionally independent of the refresh worker.  It accepts
only persisted machine-readable evidence and the fixed R11 budget file; a
synthetic small database cannot be presented as production-scale proof.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUDGET_PATH = ROOT / "config-test" / "r11_acceptance_budgets.json"
MIN_PRODUCTION_LEDGER_ROWS = 100_000


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be finite numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite numeric") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{field} must be finite non-negative numeric")
    return parsed


def evaluate_physical_refresh_evidence(
    evidence: Mapping[str, Any],
    *,
    budget_path: str | Path = DEFAULT_BUDGET_PATH,
) -> dict[str, Any]:
    """Evaluate one real refresh result against fixed R11 budgets.

    Required fields deliberately use the names returned by
    ``run_physical_refresh`` (with ``duration_ms`` converted to seconds).
    Missing, malformed, undersized, or ambiguous evidence never passes.
    """
    if not isinstance(evidence, Mapping):
        raise ValueError("physical refresh evidence must be a mapping")
    required = (
        "duration_ms", "replayed_rows", "input_delta_rows",
        "affected_scopes", "database_ledger_rows",
    )
    missing = [field for field in required if field not in evidence]
    if missing:
        return {"within_budget": False, "missing_fields": missing, "checks": {}}

    duration_ms = _finite_number(evidence["duration_ms"], "duration_ms")
    replayed_rows = _finite_number(evidence["replayed_rows"], "replayed_rows")
    input_delta_rows = _finite_number(evidence["input_delta_rows"], "input_delta_rows")
    database_rows = _finite_number(evidence["database_ledger_rows"], "database_ledger_rows")
    scopes = evidence["affected_scopes"]
    if not isinstance(scopes, (list, tuple)):
        raise ValueError("affected_scopes must be a list")
    if any(not str(scope).strip() for scope in scopes):
        raise ValueError("affected_scopes must contain nonblank identities")
    if any(value != int(value) for value in (replayed_rows, input_delta_rows, database_rows)):
        raise ValueError("row counters must be integers")

    document = json.loads(Path(budget_path).read_text(encoding="utf-8"))
    budget = document["budgets"]
    duration_seconds = duration_ms / 1000.0
    no_op = int(input_delta_rows) == 0
    replay_limit = float(
        budget["physical_refresh_no_op_replayed_rows_max"]
        if no_op else budget["physical_refresh_replayed_rows_max"]
    )
    checks = {
        "production_scale": int(database_rows) >= MIN_PRODUCTION_LEDGER_ROWS,
        "duration_seconds": duration_seconds <= float(budget["physical_refresh_seconds_max"]),
        "replayed_rows": replayed_rows <= replay_limit,
        "delta_rows_nonnegative": input_delta_rows >= 0,
        "affected_scopes_shape": True,
    }
    return {
        "within_budget": all(checks.values()),
        "checks": checks,
        "missing_fields": [],
        "production_scale_min_rows": MIN_PRODUCTION_LEDGER_ROWS,
        "database_ledger_rows": int(database_rows),
        "duration_seconds": duration_seconds,
        "replayed_rows": int(replayed_rows),
        "input_delta_rows": int(input_delta_rows),
        "affected_scopes": [str(scope) for scope in scopes],
    }

