"""Shared stock helpers for MRP.

Both `period_plan_service._explode_bom_net_first` and
`planning_service.compute_planning_preview` need per-item effective stock
that excludes warehouses listed in `ignored_warehouses` (e.g., brak isolator).

`Item.stock_qty` is aggregated across all warehouses with
`StockWarehouse.is_selected=True` — but it does NOT subtract stock parked in
`IgnoredWarehouse`. Using `Item.stock_qty` directly in MRP makes the planner
believe items in defective-isolator warehouses are available, then production
control later blocks the material issue because the source warehouse cannot
be picked.

This helper mirrors the policy used in
`production_control_material_availability._stock_by_item`, but returns the
map for ALL items in one batched query (the MRP entry points need every
item, not a specific list).
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
)


# Mirrors planning_service.DONE_STATE_KEY — duplicated to avoid a circular
# import (planning_service itself imports this helper).
_DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


def effective_stock_by_item_all(db: Session) -> Dict[int, float]:
    """
    Return `{item_id: effective_stock}` for every item, excluding stock that
    sits in `ignored_warehouses`.

    Resolution order:
      1. If `ignored_warehouses` is empty → aggregated `Item.stock_qty`
         (legacy behaviour, single query).
      2. Else use `item_warehouse_stock` filtered by `warehouse_ref1c NOT IN
         (ignored)`. Items with ANY breakdown row are authoritative — if all
         of their stock is in ignored warehouses they correctly resolve to 0.
      3. Items without any breakdown row fall back to `Item.stock_qty` so a
         partially-synced DB doesn't blank coverage. After a full re-sync
         the breakdown path becomes authoritative for everything.
    """
    ignored_refs = {
        str(r[0]) for r in db.query(IgnoredWarehouse.warehouse_ref1c).all() if r and r[0]
    }

    if not ignored_refs:
        return {
            int(iid): float(qty or 0.0)
            for iid, qty in db.query(Item.item_id, Item.stock_qty).all()
        }

    sum_rows = (
        db.query(
            ItemWarehouseStock.item_id,
            func.sum(ItemWarehouseStock.qty),
        )
        .filter(~ItemWarehouseStock.warehouse_ref1c.in_(ignored_refs))
        .group_by(ItemWarehouseStock.item_id)
        .all()
    )
    breakdown_stocks: Dict[int, float] = {
        int(iid): float(qty or 0.0) for iid, qty in sum_rows
    }

    has_any_rows = {
        int(iid)
        for (iid,) in db.query(ItemWarehouseStock.item_id).distinct().all()
    }

    result: Dict[int, float] = {}
    for iid, qty in db.query(Item.item_id, Item.stock_qty).all():
        iid_int = int(iid)
        if iid_int in has_any_rows:
            result[iid_int] = breakdown_stocks.get(iid_int, 0.0)
        else:
            result[iid_int] = float(qty or 0.0)
    return result


def active_wip_eta_by_item(db: Session) -> Dict[int, List[Tuple[Optional[date], float]]]:
    """
    Return `{item_id: [(eta_date, remaining_qty), ...]}` for every active
    production order line, sorted by `eta_date` ascending (None first — see
    below).

    Why time-aware:
      The previous implementations of MRP-availability lumped WIP into a
      single timeless pool per item. A WIP order finishing 2026-09-01 would
      then "cover" an MRP demand bucket dated 2026-07-15, even though the
      item is not physically available until September. This systematically
      under-planned production for early buckets.

    Active filter:
      - production_orders.deletion_mark = false
      - LOWER(COALESCE(order_state_key, '')) != DONE_STATE_KEY
      - production_products.remaining_qty > 0

    ETA source:
      - production_order_line_states.planned_finish_date (the operational
        commitment of when the line is expected to be done).
      - When NULL — eta is treated as the very start of time (sorted first),
        so undated WIP behaves like the legacy "always available" pool.
        That keeps results stable for orders that haven't been scheduled yet,
        while letting scheduled orders correctly tie to their bucket.
    """
    rows = (
        db.query(
            ProductionProduct.item_id,
            ProductionOrderLineState.planned_finish_date,
            func.coalesce(ProductionProduct.remaining_qty, 0.0).label("remaining_qty"),
        )
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionOrder.deletion_mark.is_(False))
        .filter(
            func.lower(func.coalesce(ProductionOrder.order_state_key, "")) != _DONE_STATE_KEY
        )
        .filter(func.coalesce(ProductionProduct.remaining_qty, 0.0) > 0)
        .all()
    )

    result: Dict[int, List[Tuple[Optional[date], float]]] = {}
    for iid, eta, remaining in rows:
        try:
            item_id = int(iid)
            qty = float(remaining or 0.0)
        except Exception:
            continue
        if qty <= 1e-12:
            continue
        eta_date: Optional[date] = eta if isinstance(eta, date) else None
        result.setdefault(item_id, []).append((eta_date, qty))

    # Sort each list with None (= "available immediately") first, then by date asc.
    sentinel = date.min
    for item_id, entries in result.items():
        entries.sort(key=lambda x: (x[0] if x[0] is not None else sentinel))
    return result


def consume_wip_at_or_before(
    wip_entries: List[Tuple[Optional[date], float]],
    bucket_date: date,
    qty_needed: float,
) -> float:
    """
    Greedy chronological consumer for the time-aware WIP list returned by
    `active_wip_eta_by_item`. Mutates `wip_entries` in place: each tuple is
    re-written as `(eta_date, remaining)` after subtraction.

    Returns the residual `qty_needed` after consuming all WIP rows with
    `eta_date is None` or `eta_date <= bucket_date` (i.e., available by the
    bucket).
    """
    if qty_needed <= 1e-12 or not wip_entries:
        return max(0.0, float(qty_needed))

    residual = float(qty_needed)
    for idx, (eta, avail) in enumerate(wip_entries):
        if residual <= 1e-12:
            break
        if avail <= 1e-12:
            continue
        if eta is not None and eta > bucket_date:
            # Sorted asc — no later entries can be earlier; stop.
            break
        used = min(avail, residual)
        wip_entries[idx] = (eta, avail - used)
        residual -= used
    return max(0.0, residual)
