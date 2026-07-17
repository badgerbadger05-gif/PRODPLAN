"""Feedback from the 1С production-order sync into DBR slots/signals — Фаза 3.

After ``production_order_sync`` has refreshed orders and produced_qty from 1С,
this closes the loop: it finds the DBR-materialized orders via ``sync_link``
(source_system='dbr') and moves the owning slot/signal forward by the actual
output.

This runs as a *best-effort* tail of the sync: the caller wraps it in
try/except so a feedback failure never fails the production-order sync itself
(the sync is the load-bearing shared job; feedback is advisory DBR bookkeeping).
The diff into the shared sync file is a single guarded hook call.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ...models import (
    DbrDrumSlot,
    DbrFeederSignal,
    ProductionOrder,
    ProductionProduct,
    SyncLink,
)
from .core.feeder import signal_identity
from .materialize_service import (
    PRODUCTION_ORDER_ENTITY,
    SIGNAL_DOCTYPE,
    SLOT_DOCTYPE,
    SOURCE_SYSTEM,
)


def _produced_for_ref(db: Session, ref_key: str, item_id: int) -> tuple[float, bool]:
    """(produced_qty, order_found) for the DBR order identified by ref_key.

    produced_qty is summed over the order's products matching item_id (the
    slot/signal item); order_found tells apart "not synced yet" from "0 made".
    """
    order = (
        db.query(ProductionOrder)
        .filter(ProductionOrder.order_ref1c == ref_key)
        .one_or_none()
    )
    if order is None:
        return 0.0, False
    produced = 0.0
    for product in (
        db.query(ProductionProduct)
        .filter(
            ProductionProduct.order_id == order.order_id,
            ProductionProduct.item_id == int(item_id),
        )
        .all()
    ):
        produced += float(product.produced_qty or 0)
    return produced, True


def apply_order_feedback(db: Session) -> dict[str, Any]:
    """Push actual output from synced 1С orders onto DBR slots/signals.

    Returns a small stats dict. Commits its own changes so a wrapping
    try/except in the sync can swallow failures without touching sync state.
    """
    links = (
        db.query(SyncLink)
        .filter(
            SyncLink.source_system == SOURCE_SYSTEM,
            SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
            SyncLink.status == "success",
            SyncLink.target_ref_key.isnot(None),
        )
        .all()
    )
    slots_updated = signals_updated = 0
    for link in links:
        ref_key = str(link.target_ref_key or "").strip()
        if not ref_key:
            continue
        if link.source_doctype == SLOT_DOCTYPE:
            slot = db.get(DbrDrumSlot, int(link.source_id))
            if slot is None:
                continue
            produced, found = _produced_for_ref(db, ref_key, int(slot.item_id))
            if not found:
                continue
            changed = float(slot.produced_qty or 0) != produced
            slot.produced_qty = produced
            # Fully produced closes the tile; partial output leaves it released.
            if produced >= float(slot.qty or 0) and slot.release_status == "released":
                slot.release_status = "completed"
                changed = True
            if changed:
                slots_updated += 1
        elif link.source_doctype == SIGNAL_DOCTYPE:
            signal = db.get(DbrFeederSignal, int(link.source_id))
            if signal is None:
                continue
            produced, found = _produced_for_ref(db, ref_key, int(signal.item_id))
            if not found:
                continue
            suggested = float(signal.suggested_qty or 0)
            if suggested > 0 and produced >= suggested:
                new_status = signal_identity.DONE
            elif produced > 0:
                new_status = signal_identity.IN_WORK
            else:
                new_status = signal.status  # stays Order Created
            if new_status != signal.status:
                signal.status = new_status
                signals_updated += 1

    db.commit()
    return {"slots_updated": slots_updated, "signals_updated": signals_updated}
