"""Shared stock helpers for MRP.

Both `period_plan_service._explode_bom_net_first` and
`planning_service.compute_planning_preview` need per-item effective stock
that applies the warehouse availability settings.

`Item.stock_qty` can lag behind the detailed warehouse settings. Using it
directly in MRP may let the planner see stock parked in unchecked warehouses
or in `IgnoredWarehouse`, then production control later blocks the material
issue because the source warehouse cannot be picked.

This helper mirrors the policy used in
`production_control_material_availability._stock_by_item`, but returns the
map for ALL items in one batched query (the MRP entry points need every
item, not a specific list).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    StockBin,
    StockWarehouse,
)


_DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


def _production_supply_qty_expr():
    """Quantity still expected from an open production line.

    Completed 1C orders never provide future supply. Their factual output is
    available to MRP only after the stock sync has put it into warehouse stock.
    """
    return func.coalesce(ProductionProduct.remaining_qty, 0.0)


def effective_stock_by_item_all(db: Session) -> Dict[int, float]:
    """
    Return `{item_id: effective_stock}` for every item with warehouse settings
    applied.

    Resolution order:
      1. If no warehouse settings exist → aggregated `Item.stock_qty`
         (legacy behaviour, single query).
      2. Else use `item_warehouse_stock` filtered to selected warehouses and
         with ignored warehouses excluded. Items with ANY breakdown row are
         authoritative — if all of their stock is in unchecked/ignored
         warehouses they correctly resolve to 0.
      3. Items without any breakdown row fall back to `Item.stock_qty` so a
         partially-synced DB doesn't blank coverage. After a full re-sync
         the breakdown path becomes authoritative for everything.

    Inc5 (design §11): behind the ``STOCK_SOURCE`` feature flag the STOCK
    on-hand source switches from the warehouse tables to the physical ledger's
    ``stock_bin`` pool. The DEFAULT (``STOCK_SOURCE=legacy``) is byte-identical
    to the historical behaviour — the branch below is inert. Only the internal
    source changes; the ``{item_id: qty}`` signature is unchanged. When
    ``stock_bin`` is seeded from the same snapshot that feeds
    ``item_warehouse_stock`` the two sources return identical dicts over the
    same contour (the Inc5 equivalence property).
    """
    # Lazy import: item_ledger.config triggers the item_ledger package __init__,
    # which (via reservation_ledger → mrp_freeze) imports THIS module — a
    # top-level import would be a cycle. The flag is read fresh at call time.
    from .item_ledger.config import use_bin_stock

    if use_bin_stock():
        return _effective_stock_by_item_all_from_bin(db)
    ignored_refs = {
        str(r[0]) for r in db.query(IgnoredWarehouse.warehouse_ref1c).all() if r and r[0]
    }
    warehouse_rows = db.query(StockWarehouse.warehouse_ref1c, StockWarehouse.is_selected).all()
    selected_refs = {
        str(ref)
        for ref, is_selected in warehouse_rows
        if ref and bool(is_selected)
    }
    has_warehouse_settings = bool(warehouse_rows)

    if not has_warehouse_settings and not ignored_refs:
        return {
            int(iid): float(qty or 0.0)
            for iid, qty in db.query(Item.item_id, Item.stock_qty).all()
        }

    sum_query = (
        db.query(
            ItemWarehouseStock.item_id,
            func.sum(ItemWarehouseStock.qty),
        )
    )
    if has_warehouse_settings:
        if selected_refs:
            sum_query = sum_query.filter(ItemWarehouseStock.warehouse_ref1c.in_(selected_refs))
        else:
            sum_query = sum_query.filter(False)
    if ignored_refs:
        sum_query = sum_query.filter(~ItemWarehouseStock.warehouse_ref1c.in_(ignored_refs))
    sum_rows = sum_query.group_by(ItemWarehouseStock.item_id).all()
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


def _effective_stock_by_item_all_from_bin(db: Session) -> Dict[int, float]:
    """Inc5 ledger-sourced twin of :func:`effective_stock_by_item_all`.

    Structurally identical to the legacy function — same 3-tier resolution, same
    ``{item_id: qty}`` shape, same authoritative/fallback semantics — but the
    authoritative per-item breakdown is summed from ``stock_bin.on_hand``
    instead of ``item_warehouse_stock.qty``, and the contour additionally
    excludes ``is_finished_goods`` warehouses (design §2.5: ГП-склады are out of
    the planning pool).

    Equivalence: when ``stock_bin`` is seeded from the same snapshot that feeds
    ``item_warehouse_stock`` (one bin per warehouse row) and no selected,
    non-ignored finished-goods warehouse holds stock, this returns a dict
    identical to the legacy path item-for-item (within EPS).

    The tier-1 (no settings) and tier-3 (item without any breakdown row)
    fallbacks read ``Item.stock_qty`` — the same "partially-synced DB" safety
    nets as legacy — so the dict KEYS match legacy regardless of source.
    """
    ignored_refs = {
        str(r[0]) for r in db.query(IgnoredWarehouse.warehouse_ref1c).all() if r and r[0]
    }
    warehouse_rows = db.query(
        StockWarehouse.warehouse_ref1c,
        StockWarehouse.is_selected,
        StockWarehouse.is_finished_goods,
    ).all()
    # selected minus finished_goods — ГП stays out of the pool even if selected.
    selected_refs = {
        str(ref) for ref, sel, fg in warehouse_rows if ref and bool(sel) and not bool(fg)
    }
    finished_goods_refs = {str(ref) for ref, _sel, fg in warehouse_rows if ref and bool(fg)}
    has_warehouse_settings = bool(warehouse_rows)

    if not has_warehouse_settings and not ignored_refs:
        return {
            int(iid): float(qty or 0.0)
            for iid, qty in db.query(Item.item_id, Item.stock_qty).all()
        }

    sum_query = db.query(StockBin.item_id, func.sum(StockBin.on_hand))
    if has_warehouse_settings:
        if selected_refs:
            sum_query = sum_query.filter(StockBin.warehouse_ref1c.in_(selected_refs))
        else:
            sum_query = sum_query.filter(False)
    if ignored_refs:
        sum_query = sum_query.filter(~StockBin.warehouse_ref1c.in_(ignored_refs))
    if finished_goods_refs:
        sum_query = sum_query.filter(~StockBin.warehouse_ref1c.in_(finished_goods_refs))
    sum_rows = sum_query.group_by(StockBin.item_id).all()
    breakdown_stocks: Dict[int, float] = {
        int(iid): float(qty or 0.0) for iid, qty in sum_rows
    }

    # An item is "authoritative via the breakdown" if it has ANY bin row at all
    # (even in an unselected/ignored/finished_goods warehouse) — mirrors the
    # legacy ItemWarehouseStock rule so the two worlds agree on which items fall
    # through to the stock_qty tier-3 fallback.
    has_any_rows = {
        int(iid) for (iid,) in db.query(StockBin.item_id).distinct().all()
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
      - completed 1C orders are excluded: their output is covered only by
        synced warehouse stock, never by a production-order fallback.
      - effective supply qty > 0

    ETA source:
      - production_order_line_states.planned_finish_date (the operational
        commitment of when the line is expected to be done).
      - When NULL — eta is treated as the very start of time (sorted first),
        so undated WIP behaves like the legacy "always available" pool.
        That keeps results stable for orders that haven't been scheduled yet,
        while letting scheduled orders correctly tie to their bucket.
    """
    supply_qty = _production_supply_qty_expr()
    rows = (
        db.query(
            ProductionProduct.item_id,
            ProductionOrderLineState.planned_finish_date,
            supply_qty.label("remaining_qty"),
        )
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionOrder.deletion_mark.is_(False))
        .filter(func.lower(func.coalesce(ProductionOrder.order_state_key, "")) != _DONE_STATE_KEY)
        .filter(supply_qty > 0)
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


@dataclass
class WipSupplyLine:
    """A single open production-order (WIP) supply line for the freeze pools.

    Unlike the legacy timeless ``(eta, qty)`` tuple, this carries the identity
    of the WIP source so a freeze allocation can name exactly which production
    order/product covered a requirement, and ``fact_at_freeze`` keeps the frozen
    quantity even after ``remaining`` is greedily consumed across the queue.
    """

    eta: Optional[date]
    remaining: float          # mutable — decremented as buckets consume it
    fact_at_freeze: float     # immutable — the qty frozen at build time
    order_id: int
    order_ref1c: Optional[str]
    product_id: int
    source_line_ref: str = ""


def active_wip_supply_by_item(
    db: Session,
    exclude_product_ids: Optional[Iterable[int]] = None,
) -> Dict[int, List[WipSupplyLine]]:
    """
    Identity-carrying variant of :func:`active_wip_eta_by_item` for the freeze
    pools. Same active-WIP filter (non-done, non-deleted, effective supply > 0),
    but each line keeps ``product_id`` / ``order_id`` / ``order_ref1c`` and the
    list is sorted deterministically ``(eta or date.min, order_ref1c or '',
    order_id, product_id)`` so allocation rows are stable across refreezes.

    ``exclude_product_ids`` drops a run's OWN materialised production lines: an
    order created to *execute* a snapshot's net must not double as *coverage*
    of that same net (else refreeze self-destructs — net → 0 → orders vanish →
    net back up …). The legacy ``active_wip_eta_by_item`` is left untouched.
    """
    exclude = {int(p) for p in (exclude_product_ids or [])}
    supply_qty = _production_supply_qty_expr()
    rows = (
        db.query(
            ProductionProduct.item_id,
            ProductionProduct.product_id,
            ProductionOrder.order_id,
            ProductionOrder.order_ref1c,
            ProductionOrderLineState.planned_finish_date,
            supply_qty.label("remaining_qty"),
        )
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionOrder.deletion_mark.is_(False))
        .filter(func.lower(func.coalesce(ProductionOrder.order_state_key, "")) != _DONE_STATE_KEY)
        .filter(supply_qty > 0)
        .all()
    )

    result: Dict[int, List[WipSupplyLine]] = {}
    for iid, product_id, order_id, order_ref1c, eta, remaining in rows:
        try:
            item_id = int(iid)
            qty = float(remaining or 0.0)
            pid = int(product_id)
        except Exception:
            continue
        if qty <= 1e-12 or pid in exclude:
            continue
        eta_date: Optional[date] = eta if isinstance(eta, date) else None
        result.setdefault(item_id, []).append(
            WipSupplyLine(
                eta=eta_date,
                remaining=qty,
                fact_at_freeze=qty,
                order_id=int(order_id),
                order_ref1c=(str(order_ref1c) if order_ref1c else None),
                product_id=pid,
            )
        )

    sentinel = date.min
    for lines in result.values():
        lines.sort(
            key=lambda w: (
                w.eta if w.eta is not None else sentinel,
                w.order_ref1c or "",
                int(w.order_id),
                int(w.product_id),
            )
        )
    return result


def consume_wip_detailed(
    wip_lines: List[WipSupplyLine],
    bucket_date: date,
    qty_needed: float,
) -> Tuple[float, List[Tuple[WipSupplyLine, float]]]:
    """
    Greedy chronological consumer over :class:`WipSupplyLine` (the identity-aware
    twin of :func:`consume_wip_at_or_before`). Mutates ``line.remaining`` in
    place and returns ``(residual, [(line, used), ...])`` — the residual after
    consuming every line available by ``bucket_date`` plus the per-line split so
    the caller can record a freeze allocation.
    """
    used_lines: List[Tuple[WipSupplyLine, float]] = []
    if qty_needed <= 1e-12 or not wip_lines:
        return max(0.0, float(qty_needed)), used_lines

    residual = float(qty_needed)
    for line in wip_lines:
        if residual <= 1e-12:
            break
        avail = float(line.remaining)
        if avail <= 1e-12:
            continue
        if line.eta is not None and line.eta > bucket_date:
            # Sorted asc — no later entry can be earlier; stop.
            break
        used = min(avail, residual)
        line.remaining = avail - used
        residual -= used
        used_lines.append((line, used))
    return max(0.0, residual), used_lines


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
