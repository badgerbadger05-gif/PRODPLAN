"""Build one complete future-supply capture for an obligation candidate.

WIP and supplier orders are two projections of the same planning contour.  A
capture is therefore deliberately *combined*: calling the replacement core
once per kind would let the latter call erase the former.  This module owns no
commit or rollback; its caller keeps the candidate generation and its planning
snapshot in one outer transaction.
"""

from __future__ import annotations

from typing import Mapping

from sqlalchemy.orm import Session

from app import models

from .future_supply_capture import (
    FutureSupplyEvidence,
    replace_future_supply_capture,
)
from .supplier_future_supply import supplier_future_supply_evidence
from .wip_future_supply import collect_wip_future_supply_evidence


_OBLIGATION_REFRESH = "obligation_refresh"


class CandidateFutureSupplyError(ValueError):
    """The proposed obligation generation is not a valid capture target."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _require_lineage(
    db: Session,
    *,
    accepted_generation_id: int,
    target_generation_id: int,
) -> tuple[models.LedgerGeneration, models.LedgerGeneration]:
    """Require the candidate to reuse exactly the accepted physical prefix."""
    source = db.get(models.LedgerGeneration, int(accepted_generation_id))
    if source is None or _text(source.status) != "accepted" or source.cutoff is None:
        raise CandidateFutureSupplyError(
            "future-supply source must be an accepted Ledger generation with cutoff"
        )
    target = db.get(models.LedgerGeneration, int(target_generation_id))
    if target is None or _text(target.status) != "building" or target.cutoff is None:
        raise CandidateFutureSupplyError(
            "future-supply target must be a BUILDING Ledger generation with cutoff"
        )
    watermarks = dict(target.source_watermarks or {})
    if watermarks.get("generation_kind") != _OBLIGATION_REFRESH:
        raise CandidateFutureSupplyError(
            "future-supply target is not an obligation_refresh generation"
        )
    if watermarks.get("parent_generation_id") != int(source.id):
        raise CandidateFutureSupplyError(
            "future-supply target does not name the accepted source as parent"
        )
    if int(target.physical_import_batch_id) != int(source.physical_import_batch_id):
        raise CandidateFutureSupplyError(
            "future-supply target must share the accepted physical import batch"
        )
    if target.cutoff != source.cutoff:
        raise CandidateFutureSupplyError(
            "future-supply target must share the accepted cutoff"
        )
    return source, target


def capture_candidate_future_supply(
    db: Session,
    accepted_generation_id: int,
    target_generation_id: int,
    capture_batch_id: int,
    *,
    planning_pool_by_warehouse: Mapping[str, str],
    explicit_make_transfer_recorders: set[str] | None = None,
) -> dict[str, object]:
    """Capture WIP and supplier evidence atomically into one candidate batch.

    The two source adapters retain rejected/ambiguous evidence themselves.  No
    source is downgraded to a fabricated quantity here: qualification errors
    fail the candidate before a partial replacement can be persisted.
    """
    source, target = _require_lineage(
        db,
        accepted_generation_id=accepted_generation_id,
        target_generation_id=target_generation_id,
    )
    wip: list[FutureSupplyEvidence] = collect_wip_future_supply_evidence(
        db,
        int(source.id),
        planning_pool_by_warehouse=planning_pool_by_warehouse,
        explicit_make_transfer_recorders=explicit_make_transfer_recorders,
    )
    supplier: tuple[FutureSupplyEvidence, ...] = supplier_future_supply_evidence(
        db,
        int(source.id),
        planning_pool_by_warehouse=planning_pool_by_warehouse,
    )
    # One call is an accounting invariant: replacement is scoped to a capture
    # batch, not to a future-supply kind.
    return dict(replace_future_supply_capture(
        db,
        int(target.id),
        int(capture_batch_id),
        [*wip, *supplier],
    ))
