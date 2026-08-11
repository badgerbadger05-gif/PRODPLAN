"""Shared stock helpers for MRP.

Both `period_plan_service._explode_bom_net_first` and MRP entry points
need per-item effective stock that applies the warehouse availability settings.

This helper mirrors the policy used in
`production_control_material_availability._stock_by_item`, but returns the
map for ALL items in one batched query (the MRP entry points need every
item, not a specific list).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    IgnoredWarehouse,
    Item,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    StockBin,
    StockWarehouse,
)
from .one_c_export_common import DEFAULT_ORGANIZATION_REF1C
from .production_control_common import DONE_STATE_KEY as _DONE_STATE_KEY
from .production_output_truth import accepted_product_remaining_expr


@dataclass(frozen=True)
class PlanningWarehouseScope:
    has_warehouse_rows: bool
    selected_refs: Set[str]
    ignored_refs: Set[str]
    finished_refs: Set[str]
    organization_ref: str = DEFAULT_ORGANIZATION_REF1C


def planning_warehouse_scope(db: Session) -> PlanningWarehouseScope:
    ignored_refs = {
        str(ref) for (ref,) in db.query(IgnoredWarehouse.warehouse_ref1c).all() if ref
    }
    warehouse_rows = db.query(
        StockWarehouse.warehouse_ref1c,
        StockWarehouse.is_selected,
        StockWarehouse.is_finished_goods,
    ).all()
    return PlanningWarehouseScope(
        has_warehouse_rows=bool(warehouse_rows),
        selected_refs={
            str(ref) for ref, selected, finished in warehouse_rows
            if ref and bool(selected) and not bool(finished)
        },
        ignored_refs=ignored_refs,
        finished_refs={
            str(ref) for ref, _selected, finished in warehouse_rows
            if ref and bool(finished)
        },
    )


def apply_planning_warehouse_scope(
    query: Any,
    scope: PlanningWarehouseScope,
    *,
    warehouse_column: Any,
    organization_column: Any,
    organization_ref: Optional[str] = DEFAULT_ORGANIZATION_REF1C,
) -> Any:
    if scope.has_warehouse_rows:
        query = (
            query.filter(warehouse_column.in_(scope.selected_refs))
            if scope.selected_refs
            else query.filter(False)
        )
    if scope.ignored_refs:
        query = query.filter(~warehouse_column.in_(scope.ignored_refs))
    if scope.finished_refs:
        query = query.filter(~warehouse_column.in_(scope.finished_refs))
    if organization_ref is not None:
        query = query.filter(organization_column == organization_ref)
    return query


def planning_stock_by_item(
    db: Session,
    ledger_generation_id: int,
    *,
    item_ids: Optional[Set[int]] = None,
    organization_ref: Optional[str] = DEFAULT_ORGANIZATION_REF1C,
) -> Dict[int, float]:
    if item_ids is not None and not item_ids:
        return {}
    scope = planning_warehouse_scope(db)
    query = db.query(StockBin.item_id, func.sum(StockBin.on_hand)).filter(
        StockBin.ledger_generation_id == int(ledger_generation_id)
    )
    if item_ids is not None:
        query = query.filter(StockBin.item_id.in_(sorted(item_ids)))
    query = apply_planning_warehouse_scope(
        query,
        scope,
        warehouse_column=StockBin.warehouse_ref1c,
        organization_column=StockBin.organization_ref,
        organization_ref=organization_ref,
    )
    return {
        int(item_id): float(quantity or 0)
        for item_id, quantity in query.group_by(StockBin.item_id).all()
    }


def _production_supply_qty_expr():
    """Quantity still expected from an open production line.

    Completed 1C orders never provide future supply. Their factual output is
    available to MRP only after the stock sync has put it into warehouse stock.
    """
    return accepted_product_remaining_expr(
        ProductionProduct.quantity,
        ProductionProduct.produced_qty,
    )


def effective_stock_by_item_all(db: Session) -> Dict[int, float]:
    """Read the accepted physical Item Ledger; no mutable-stock fallback."""
    from .planning_truth import require_accepted

    truth = require_accepted(db)
    return planning_stock_by_item(db, int(truth.generation_id))


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
