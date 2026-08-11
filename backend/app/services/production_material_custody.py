"""Per-order material custody derived from physical transfer documents.

This is an execution-location projection, not a planning reservation engine.
Planning reservations belong exclusively to Item Ledger ``ReservationEntry``.

* direction='issue', status in draft/requested/issued/exported — the kit is
  still on the source warehouse, waiting for the storekeeper. Reserves
  ``max(0, required_qty - issued_qty)`` at ``source_warehouse_ref1c``.
* direction='issue', status='posted' — components that already were on the
  workshop warehouse and were claimed by a workshop-local posted issue. Reserved
  at ``warehouse_ref1c`` until consumed by production.
* direction='return', status='posted' — leftovers physically moved back to
  the source warehouse; subtracts from the workshop reservation. A pending
  (non-posted) return does NOT subtract: the items are still on the workshop
  and must stay invisible to other orders until they physically leave.

Custody is released only by a posted return or by terminal order state.
Production counters are document metadata and are never treated as physical
consumption facts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from ..models import ProductionProduct
from .production_control_common import DONE_STATE_KEY
from .production_output_truth import accepted_product_output

# Issue statuses where the kit has not physically moved yet.
TRANSIT_STATUSES = ("draft", "requested", "issued", "exported")
_RESERVATION_CLOSED_LINE_STATUSES = {"completed", "cancelled", "produced"}


@dataclass
class ProductMaterialCustody:
    """Reservation picture of one production line, per component item."""

    in_transit: Dict[int, float] = field(default_factory=dict)
    at_workshop: Dict[int, float] = field(default_factory=dict)

    def total(self, component_item_id: int) -> float:
        cid = int(component_item_id)
        return self.in_transit.get(cid, 0.0) + self.at_workshop.get(cid, 0.0)


@dataclass
class MaterialCustodyState:
    """Aggregated reservations across all active production lines."""

    by_product: Dict[int, ProductMaterialCustody] = field(default_factory=dict)
    # (warehouse_ref1c, component_item_id) -> reserved qty located there.
    by_warehouse_item: Dict[Tuple[str, int], float] = field(default_factory=dict)

    def total_by_item(self, *, exclude_product_id: Optional[int] = None) -> Dict[int, float]:
        result: Dict[int, float] = {}
        for product_id, res in self.by_product.items():
            if exclude_product_id is not None and int(product_id) == int(exclude_product_id):
                continue
            for cid, qty in res.in_transit.items():
                result[cid] = result.get(cid, 0.0) + qty
            for cid, qty in res.at_workshop.items():
                result[cid] = result.get(cid, 0.0) + qty
        return result

    def reserved_at_warehouse(self, warehouse_ref1c: Optional[str], component_item_id: int) -> float:
        if not warehouse_ref1c:
            return 0.0
        return self.by_warehouse_item.get((str(warehouse_ref1c), int(component_item_id)), 0.0)

    def for_product(self, product_id: int) -> ProductMaterialCustody:
        return self.by_product.get(int(product_id), ProductMaterialCustody())



def is_product_custody_active(product: ProductionProduct) -> bool:
    """Whether a production line may still hold component reservations."""
    order = getattr(product, "order", None)
    if order is not None:
        if bool(getattr(order, "deletion_mark", False)):
            return False
        state_key = str(getattr(order, "order_state_key", "") or "").lower()
        if state_key == DONE_STATE_KEY:
            return False

    control_state = getattr(product, "control_state", None)
    line_status = str(getattr(control_state, "status", "") or "").lower()
    if line_status in _RESERVATION_CLOSED_LINE_STATUSES:
        return False

    if accepted_product_output(product).remaining_qty <= 0:
        return False

    return True
