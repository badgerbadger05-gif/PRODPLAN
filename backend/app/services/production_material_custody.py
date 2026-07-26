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
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy.orm import Session, joinedload

from ..models import (
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionProduct,
)
from .production_control_common import to_float as _to_float

# 1C state for completed production orders. Duplicated locally to keep this
# low-level reservation module independent from journal/planning services.
DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"

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

    if _to_float(getattr(product, "remaining_qty", 0.0)) <= 1e-9:
        return False

    return True


def load_material_custody(
    db: Session,
    *,
    item_ids: Optional[Iterable[int]] = None,
) -> MaterialCustodyState:
    """
    Build the reservation picture from material-issue documents.

    ``item_ids`` narrows the result to the given component items (the
    consuming queries usually care about one BOM); product-level consumption
    is still computed correctly because it is per-component.
    """
    wanted: Optional[Set[int]] = None
    if item_ids is not None:
        wanted = {int(x) for x in item_ids if x is not None}
        if not wanted:
            return MaterialCustodyState()

    query = (
        db.query(ProductionMaterialIssue)
        .options(joinedload(ProductionMaterialIssue.lines))
        .filter(
            ProductionMaterialIssue.status.in_(TRANSIT_STATUSES + ("posted",)),
        )
    )
    if wanted is not None:
        product_ids = [
            int(row[0])
            for row in (
                db.query(ProductionMaterialIssue.product_id)
                .join(
                    ProductionMaterialIssueLine,
                    ProductionMaterialIssueLine.issue_id == ProductionMaterialIssue.issue_id,
                )
                .filter(ProductionMaterialIssueLine.component_item_id.in_(wanted))
                .filter(ProductionMaterialIssue.status.in_(TRANSIT_STATUSES + ("posted",)))
                .distinct()
                .all()
            )
        ]
        if not product_ids:
            return MaterialCustodyState()
        query = query.filter(ProductionMaterialIssue.product_id.in_(product_ids))
    issues: List[ProductionMaterialIssue] = query.all()
    if not issues:
        return MaterialCustodyState()

    product_ids = sorted({int(issue.product_id) for issue in issues})
    products = (
        db.query(ProductionProduct)
        .options(
            joinedload(ProductionProduct.order),
            joinedload(ProductionProduct.control_state),
        )
        .filter(ProductionProduct.product_id.in_(product_ids))
        .all()
    )
    products_by_id = {
        int(p.product_id): p
        for p in products
        if is_product_custody_active(p)
    }
    issues = [issue for issue in issues if int(issue.product_id) in products_by_id]
    if not issues:
        return MaterialCustodyState()

    state = MaterialCustodyState()

    # ------------------------------------------------------------------
    # Pass 1: per product, aggregate delivered/returned/in-transit amounts.
    # ------------------------------------------------------------------
    # delivered[(product_id, comp)] -> list of (issue_id, dest_wh, qty)
    delivered: Dict[Tuple[int, int], List[Tuple[int, str, float]]] = {}
    returned: Dict[Tuple[int, int], float] = {}
    transit: Dict[Tuple[int, int, str], float] = {}

    for issue in issues:
        pid = int(issue.product_id)
        direction = str(issue.direction or "issue")
        status = str(issue.status or "")
        for line in issue.lines or []:
            cid = int(line.component_item_id)
            if direction == "issue" and status == "posted":
                # 1C posts the whole document, so required_qty is the amount
                # that physically moved. issued_qty may lag behind: the
                # posted-transfer sync historically did not stamp it.
                qty = _to_float(line.required_qty)
                if qty > 1e-9:
                    dest = str(issue.warehouse_ref1c or "")
                    delivered.setdefault((pid, cid), []).append(
                        (int(issue.issue_id), dest, qty)
                    )
            elif direction == "issue" and status in TRANSIT_STATUSES:
                qty = max(0.0, _to_float(line.required_qty) - _to_float(line.issued_qty))
                if qty > 1e-9:
                    source = str(issue.source_warehouse_ref1c or "")
                    transit[(pid, cid, source)] = transit.get((pid, cid, source), 0.0) + qty
            elif direction == "return" and status == "posted":
                qty = _to_float(line.required_qty)
                if qty > 1e-9:
                    returned[(pid, cid)] = returned.get((pid, cid), 0.0) + qty

    # ------------------------------------------------------------------
    # Pass 2: net workshop reservations = delivered - returned - consumed.
    # Consumption and returns are charged against deliveries oldest-first so
    # the per-warehouse map stays deterministic.
    # ------------------------------------------------------------------
    for (pid, cid), parts in delivered.items():
        to_subtract = returned.get((pid, cid), 0.0)

        parts.sort(key=lambda row: row[0])
        for _issue_id, dest, qty in parts:
            take = min(qty, to_subtract)
            to_subtract -= take
            remaining = qty - take
            if remaining <= 1e-9:
                continue
            res = state.by_product.setdefault(pid, ProductMaterialCustody())
            res.at_workshop[cid] = res.at_workshop.get(cid, 0.0) + remaining
            if dest:
                key = (dest, cid)
                state.by_warehouse_item[key] = state.by_warehouse_item.get(key, 0.0) + remaining

    for (pid, cid, source), qty in transit.items():
        res = state.by_product.setdefault(pid, ProductMaterialCustody())
        res.in_transit[cid] = res.in_transit.get(cid, 0.0) + qty
        if source:
            key = (source, cid)
            state.by_warehouse_item[key] = state.by_warehouse_item.get(key, 0.0) + qty

    if wanted is not None:
        for res in state.by_product.values():
            res.in_transit = {cid: q for cid, q in res.in_transit.items() if cid in wanted}
            res.at_workshop = {cid: q for cid, q in res.at_workshop.items() if cid in wanted}
        state.by_warehouse_item = {
            key: q for key, q in state.by_warehouse_item.items() if key[1] in wanted
        }
    return state


def committed_material_by_item(
    db: Session,
    item_ids: Sequence[int],
    *,
    exclude_product_id: Optional[int] = None,
) -> Dict[int, float]:
    """
    Pool-level reservations per component item: kits in transit plus kits
    already delivered to workshops and not yet consumed by production.

    Replaces the old "draft..exported only" view that let a posted kit lying
    on a workshop re-enter the free pool and cover a second order.
    """
    state = load_material_custody(db, item_ids=item_ids)
    return state.total_by_item(exclude_product_id=exclude_product_id)
