"""Reusable truth-tracking response metadata.

The planning stack requires request-captured truth lineage on many read endpoints.
The same schema is intentionally centralized to avoid drift between routers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..services.planning_truth import PlanningTruthReadiness


class TruthMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ledger_generation: int
    cutoff: str
    truth_status: str
    truth_reason: str | None = None


def build_truth_meta(readiness: PlanningTruthReadiness) -> TruthMeta:
    cutoff: datetime | None = readiness.cutoff
    return TruthMeta(
        ledger_generation=int(readiness.ledger_generation) if readiness.ledger_generation is not None else 0,
        cutoff=(cutoff.isoformat() if cutoff is not None else ""),
        truth_status=str(readiness.truth_status),
        truth_reason=readiness.reason,
    )


def build_truth_meta_from_snapshot(meta: dict[str, Any]) -> TruthMeta:
    """Build response lineage from the same immutable snapshot as its rows."""
    return TruthMeta(
        ledger_generation=int(meta["ledger_generation"]),
        cutoff=str(meta["cutoff"]),
        truth_status=str(meta["truth_status"]),
        truth_reason=meta.get("truth_reason"),
    )
