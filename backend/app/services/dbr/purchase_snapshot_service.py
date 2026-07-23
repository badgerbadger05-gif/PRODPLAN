"""Persisted read boundary for the Ledger-native DBR purchase cockpit."""

from __future__ import annotations

from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session

from app.services.planning_truth import (
    CAPABILITY_DBR_PURCHASE_COCKPIT,
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_RESERVATION_REPLAY,
    PlanningTruthUnavailable,
    get_latest_read_snapshot,
    get_truth_state,
)


CONSUMER = "dbr_purchase_cockpit"
REQUIRED_CAPABILITIES = (
    CAPABILITY_PHYSICAL_LEDGER,
    CAPABILITY_RESERVATION_REPLAY,
    CAPABILITY_PLANNING_SNAPSHOTS,
    CAPABILITY_DBR_PURCHASE_COCKPIT,
)


class DbrPurchaseSnapshotUnavailable(RuntimeError):
    def __init__(self, detail: dict[str, Any]):
        self.detail = detail
        super().__init__(str(detail.get("reason") or "DBR purchase snapshot unavailable"))

    def as_dict(self) -> dict[str, Any]:
        return dict(self.detail)


def _unavailable(db: Session, reason: str, truth_detail: dict[str, Any] | None = None) -> DbrPurchaseSnapshotUnavailable:
    state = get_truth_state(db)
    detail = {
        "code": "dbr_purchase_snapshot_unavailable", "consumer": CONSUMER,
        "status": "unavailable", "truth_status": state.status,
        "ledger_generation": state.generation_id,
        "cutoff": state.cutoff.isoformat() if state.cutoff else None,
        "reason": reason,
    }
    if truth_detail:
        detail["truth"] = jsonable_encoder(truth_detail)
    return DbrPurchaseSnapshotUnavailable(detail)


def read_purchase_snapshot(db: Session) -> dict[str, Any]:
    """Read exactly the accepted generation's saved purchase rows."""
    try:
        snapshot = get_latest_read_snapshot(
            db, consumer=CONSUMER, required_capabilities=REQUIRED_CAPABILITIES,
        )
    except PlanningTruthUnavailable as exc:
        raise _unavailable(db, str(exc), exc.as_dict()) from exc
    if snapshot is None:
        raise _unavailable(db, "No DBR purchase cockpit snapshot for current accepted Ledger")
    if not isinstance(snapshot.payload, dict) or not isinstance(snapshot.payload.get("rows"), list):
        raise _unavailable(db, f"DBR purchase cockpit snapshot {snapshot.id} has invalid payload")
    result = dict(snapshot.payload)
    meta = dict(result.get("meta") or {})
    meta.update({
        "snapshot_id": int(snapshot.id), "ledger_generation": int(snapshot.ledger_generation_id),
        "cutoff": snapshot.cutoff.isoformat(), "truth_status": str(snapshot.truth_status),
        "truth_reason": snapshot.reason,
    })
    result["meta"] = meta
    return result
