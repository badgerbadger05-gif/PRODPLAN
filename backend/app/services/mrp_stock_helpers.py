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
    LedgerGeneration,
    PlanningTruthState,
    StockBin,
    StockLedgerEntry,
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


def _stock_bin_related_generation_ids(
    db: Session,
    *,
    requested_generation_id: int,
    provenance_ids: Set[int],
) -> Set[int]:
    """Resolve compact-owner provenance compatible with one truth boundary.

    A bounded physical publish updates affected compact rows before the CAS
    pointer, so the transaction can briefly contain ``parent`` and its one
    BUILDING child.  After the CAS, unchanged rows may still retain an
    ancestor provenance; rewriting thousands of stable owners solely to stamp
    a technical generation would defeat the compact-owner contract.  Accept
    only that exact lineage (plus one in-flight BUILDING child), never an
    unrelated generation.
    """
    cache = db.info.setdefault("_stock_bin_lineage_cache", {})
    cache_key = (int(requested_generation_id), tuple(sorted(provenance_ids)))
    cached = cache.get(cache_key)
    if cached is not None:
        return set(cached)

    allowed: Set[int] = {int(requested_generation_id)}
    cursor = db.get(LedgerGeneration, int(requested_generation_id))
    while cursor is not None:
        watermarks = cursor.source_watermarks or {}
        raw_parent = watermarks.get("parent_generation_id")
        try:
            parent_id = int(raw_parent) if raw_parent not in (None, "") else None
        except (TypeError, ValueError):
            parent_id = None
        if parent_id is None or parent_id in allowed:
            break
        allowed.add(parent_id)
        cursor = db.get(LedgerGeneration, parent_id)

    pending = set(provenance_ids) - allowed
    building_descendants: Set[int] = set()
    changed = True
    while pending and changed:
        changed = False
        for generation_id in tuple(pending):
            generation = db.get(LedgerGeneration, int(generation_id))
            if generation is None or str(generation.status or "") != "building":
                continue
            raw_parent = (generation.source_watermarks or {}).get("parent_generation_id")
            try:
                parent_id = int(raw_parent) if raw_parent not in (None, "") else None
            except (TypeError, ValueError):
                parent_id = None
            if parent_id in allowed:
                allowed.add(int(generation_id))
                building_descendants.add(int(generation_id))
                pending.remove(generation_id)
                changed = True
    # Two simultaneous BUILDING owners are not a valid compact projection,
    # even if both claim the same parent.
    if len(building_descendants) > 1:
        allowed = set()
    cache[cache_key] = tuple(sorted(allowed))
    return allowed


def invalidate_current_stock_bin_provenance_cache(db: Session) -> None:
    """Invalidate compact StockBin provenance metadata after an in-tx write."""
    db.info.pop("_stock_bin_provenance_cache", None)
    db.info["_stock_bin_provenance_revision"] = int(
        db.info.get("_stock_bin_provenance_revision", 0)
    ) + 1
    db.info.pop("_stock_bin_lineage_cache", None)


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


def current_stock_bin_query(db: Session, ledger_generation_id: int, *entities: Any):
    """The one accepted current ``StockBin`` read, for the exact truth pointer.

    Canon R6: ``StockBin.ledger_generation_id`` is provenance, not
    membership.  An obligation refresh never restamps bins and a bounded
    refresh restamps only the keys it touched, so a filter on
    ``ledger_generation_id == pointer`` silently drops every other key.  The
    current set is ``is_current``; the provenance of those rows is validated
    against the requested generation's lineage instead (stale or ambiguous
    fails closed).  An accepted caller must name the exact truth pointer; a
    BUILDING generation - a publication building its candidate payloads -
    reads the same current set, bounded by its own lineage.
    """
    generation = db.get(LedgerGeneration, int(ledger_generation_id))
    building = generation is not None and str(generation.status or "") == "building"
    if building:
        # A BUILDING reader is a publication descending from the pointer; a
        # candidate forked from anything else must not read the current set.
        # A genesis build has no parent and no pointer yet; a build already
        # named by the pointer is its own lineage.
        pointer = db.get(PlanningTruthState, 1)
        raw_parent = (generation.source_watermarks or {}).get("parent_generation_id")
        try:
            parent_id = int(raw_parent) if raw_parent not in (None, "") else None
        except (TypeError, ValueError):
            parent_id = None
        pointer_id = (
            int(pointer.current_generation_id)
            if pointer is not None and pointer.current_generation_id is not None
            else None
        )
        descends = (
            pointer_id == int(ledger_generation_id)
            or (parent_id is not None and parent_id == pointer_id)
            or (parent_id is None and pointer_id is None)
        )
        if not descends:
            raise ValueError(
                "BUILDING StockBin reader is not a child of the truth pointer "
                f"(generation={int(ledger_generation_id)}, parent={parent_id}, "
                f"pointer={pointer_id})"
            )
    _require_current_stock_bin_provenance(
        db, int(ledger_generation_id), require_pointer=not building,
    )
    return db.query(*entities).filter(StockBin.is_current.is_(True))


def _require_current_stock_bin_provenance(
    db: Session, ledger_generation_id: int, *, require_pointer: bool = True,
) -> None:
    # Current StockBin is compact, but the requested provenance must still be
    # the live truth pointer.  Never silently return a newer compact row to a
    # stale generation-bound caller.
    pointer = db.get(PlanningTruthState, 1)
    current_generation_id = (
        int(pointer.current_generation_id)
        if pointer is not None and pointer.current_generation_id is not None
        else None
    )
    if require_pointer and current_generation_id != int(ledger_generation_id):
        raise ValueError(
            "current StockBin provenance does not match requested Ledger generation"
        )
    # A malformed compact projection must fail closed even if its rows happen
    # to satisfy the requested query filters.
    provenance_revision = int(db.info.get("_stock_bin_provenance_revision", 0))
    provenance_cache = db.info.get("_stock_bin_provenance_cache")
    if provenance_cache is not None and provenance_cache[0] == provenance_revision:
        provenance_ids = set(provenance_cache[1])
    else:
        provenance_ids = {
            int(generation_id)
            for (generation_id,) in db.query(StockBin.ledger_generation_id)
            .filter(StockBin.is_current.is_(True))
            .distinct()
            .all()
        }
        db.info["_stock_bin_provenance_cache"] = (
            provenance_revision,
            tuple(sorted(provenance_ids)),
        )
    related_ids = _stock_bin_related_generation_ids(
        db,
        requested_generation_id=int(ledger_generation_id),
        provenance_ids=provenance_ids,
    )
    if provenance_ids and not provenance_ids.issubset(related_ids):
        raise ValueError(
            "current StockBin provenance contains a stale or ambiguous generation "
            f"(requested={int(ledger_generation_id)}, pointer={current_generation_id}, "
            f"stored={sorted(provenance_ids)})"
        )


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
    query = current_stock_bin_query(
        db, int(ledger_generation_id), StockBin.item_id, func.sum(StockBin.on_hand),
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


def historical_stock_by_item(
    db: Session,
    ledger_generation_id: int,
    *,
    item_ids: Optional[Set[int]] = None,
    organization_ref: Optional[str] = DEFAULT_ORGANIZATION_REF1C,
) -> Dict[int, float]:
    """Read an explicit generation candidate for historical/building work.

    This is intentionally separate from ``planning_stock_by_item``: it is not
    a current read and never participates in an accepted MRP/availability GET.
    """
    if item_ids is not None and not item_ids:
        return {}
    generation = db.get(LedgerGeneration, int(ledger_generation_id))
    if generation is None or str(generation.status) != "building":
        raise ValueError(
            "historical StockBin staging requires an existing building generation"
        )
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


def historical_ledger_stock_by_item(
    db: Session,
    ledger_generation_id: int,
    *,
    item_ids: Optional[Set[int]] = None,
    organization_ref: Optional[str] = DEFAULT_ORGANIZATION_REF1C,
) -> Dict[int, float]:
    """Fold immutable SLEs for an explicitly pinned accepted history read."""
    generation = db.get(LedgerGeneration, int(ledger_generation_id))
    if generation is None or str(generation.status) != "accepted":
        raise ValueError(
            "historical Ledger read requires an existing accepted generation"
        )
    if generation.physical_import_batch_id is None or generation.cutoff is None:
        raise ValueError("historical accepted generation lacks physical provenance")
    if item_ids is not None and not item_ids:
        return {}
    from .item_ledger.physical_visibility import visible_sle_query

    scope = planning_warehouse_scope(db)
    query = visible_sle_query(
        db,
        physical_import_batch_id=int(generation.physical_import_batch_id),
        cutoff=generation.cutoff,
    ).with_entities(
        StockLedgerEntry.item_id,
        func.sum(StockLedgerEntry.qty),
    )
    if item_ids is not None:
        query = query.filter(StockLedgerEntry.item_id.in_(sorted(item_ids)))
    query = apply_planning_warehouse_scope(
        query,
        scope,
        warehouse_column=StockLedgerEntry.warehouse_ref1c,
        organization_column=StockLedgerEntry.organization_ref,
        organization_ref=organization_ref,
    )
    return {
        int(item_id): float(quantity or 0)
        for item_id, quantity in query.group_by(StockLedgerEntry.item_id).all()
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


def effective_free_stock_by_item_all(db: Session) -> Dict[int, float]:
    """Accepted planning stock minus material custody held for other orders.

    The physical Ledger includes kits that are still on a source warehouse and
    kits already issued to a workshop.  Both remain physical stock, but neither
    is free for a new release-feasibility check.  Custody is filtered through
    the same planning warehouse scope as the physical balance so excluded
    warehouses cannot reduce stock counted elsewhere.
    """
    from .planning_truth import require_accepted
    from .production_material_custody_projection import (
        load_compact_current_material_custody,
    )

    truth = require_accepted(db)
    physical = planning_stock_by_item(db, int(truth.generation_id))
    _generation_id, custody = load_compact_current_material_custody(
        db, consumer="mrp.stock.free"
    )
    scope = planning_warehouse_scope(db)
    reserved: Dict[int, float] = {}
    for (warehouse_ref, item_id), quantity in custody.by_warehouse_item.items():
        ref = str(warehouse_ref or "")
        if scope.has_warehouse_rows and ref not in scope.selected_refs:
            continue
        if ref in scope.ignored_refs or ref in scope.finished_refs:
            continue
        iid = int(item_id)
        reserved[iid] = reserved.get(iid, 0.0) + max(float(quantity or 0.0), 0.0)

    return {
        item_id: max(float(quantity or 0.0) - reserved.get(item_id, 0.0), 0.0)
        for item_id, quantity in physical.items()
    }


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
