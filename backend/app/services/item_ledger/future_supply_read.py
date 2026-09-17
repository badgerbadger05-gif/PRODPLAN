"""Canonical source selection for future-supply readers.

``ledger_future_supply`` is bounded BUILDING staging only.  Once a generation
is accepted, the planning-truth pointer is the only authority that selects
the compact current owner.  This module deliberately has no latest/max
fallback: missing or mismatched truth is unavailable.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app import models


class FutureSupplyUnavailable(RuntimeError):
    """Future-supply truth is absent, stale, or outside the read contract."""


def future_supply_model(
    db: Session,
    generation_id: int,
    *,
    allow_building_read: bool = True,
):
    """Return the sole ORM owner for one exact generation context.

    BUILDING captures are readable only as bounded staging.  ACCEPTED rows are
    readable only from ``ledger_future_supply_current`` while the singleton
    planning-truth pointer names that exact generation.  Every other state is
    unavailable rather than silently falling back to historical evidence.
    """

    generation = db.get(models.LedgerGeneration, int(generation_id))
    if generation is None:
        raise FutureSupplyUnavailable(
            f"future_supply_unavailable: generation {generation_id} is missing"
        )
    status = str(generation.status or "")
    if status == "building":
        if not allow_building_read:
            raise FutureSupplyUnavailable(
                "future_supply_unavailable: BUILDING future supply is not readable here"
            )
        return models.LedgerFutureSupply
    if status != "accepted":
        raise FutureSupplyUnavailable(
            f"future_supply_unavailable: generation {generation_id} is {status or 'untyped'}"
        )
    pointer = db.get(models.PlanningTruthState, 1)
    if pointer is None or pointer.current_generation_id is None:
        raise FutureSupplyUnavailable(
            "future_supply_unavailable: accepted planning truth pointer is missing"
        )
    if int(pointer.current_generation_id) != int(generation.id):
        raise FutureSupplyUnavailable(
            "future_supply_unavailable: accepted generation is not the current truth"
        )
    return models.LedgerFutureSupplyCurrent


def future_supply_query(
    db: Session,
    generation_id: int,
    *,
    allow_building_read: bool = True,
):
    """Return a query rooted in the canonical owner for ``generation_id``."""

    return db.query(
        future_supply_model(
            db,
            int(generation_id),
            allow_building_read=allow_building_read,
        )
    )

