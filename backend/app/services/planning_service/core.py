from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Set, DefaultDict, Callable

from sqlalchemy.orm import Session, load_only
from sqlalchemy import func, and_, asc, desc
from collections import defaultdict
import json
import re
import math
import logging
from ...models import (
    PlanningConfigVersion,
    PlanningRun,
    PlannedOrder,
    PlannedOrderStage,
    PlannedPurchase,
    PlannedRework,
    CapacityLoad,
    PeggingLink,
    Item,
    Unit,
    ProductionPlanHeader,
    DefaultSpecification,
    SpecComponent,
    ProductionPlanEntry,
    ProductionResource,
    ResourceStage,
    ProductionStage,
    SpecOperation,
    Operation,
    ProductionKind,
    ResourceProductionKind,
    Specification,
    ProductionOrder,
    ProductionProduct,
    SupplierOrder,
    SupplierOrderItem,
    Supplier,
    ItemCategory,
)
from ...models import RootProduct
from ..stage_logic import determine_parent_stage_and_norm, pick_area_for_stage
from ..order_quantity_calculator import OrderQuantityCalculator
from ..priority_manager import PriorityManager
from ..capacity_scheduler import CapacityScheduler
from ..mrp_stock_helpers import (
    active_wip_eta_by_item as _active_wip_eta_by_item,
    consume_wip_at_or_before as _consume_wip_at_or_before,
    effective_stock_by_item_all as _effective_stock_by_item_all,
)
from ..pegging_builder import PeggingBuilder
from ..replenishment import (
    REPLENISHMENT_FLOW_PURCHASE,
    REPLENISHMENT_FLOW_REWORK,
    classify_replenishment_flow,
)
from ..warnings import make_warning, log_warning
from ..supplier_order_status import (
    STATE_TO_PHASE,
    NETTING_PHASES,
    state_counts_in_mrp as _supplier_order_counts_in_mrp,
)


from .constants import DEFAULT_PLANNING_CONFIG, logger
from .config import get_active_planning_config
from .helpers import _build_component_reservations_from_active_1c, _deep_merge, _get_active_production_remaining_by_item, _get_active_supplier_remaining_by_item_date, _read_last_stock_sync_at, _to_date
from .runs import _clear_run_outputs, _get_or_create_run

def compute_gross_requirements(
    db: Session,
    horizon_days: Optional[int] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        cfg_id, cfg = get_active_planning_config(db)
    except Exception:
        cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)

    overrides: Dict[str, Any] = {}
    if horizon_days is not None:
        overrides["planning_horizon_days"] = int(horizon_days)
    if config_overrides:
        overrides = _deep_merge(overrides, config_overrides)

    snapshot = _deep_merge(cfg, overrides)

    horizon = int(snapshot.get("planning_horizon_days", 90))
    ss_percent = float(snapshot.get("safety_stock_percent", 1) or 0.0)

    d0: date = date.today()
    dmax: date = d0 + timedelta(days=max(1, horizon) - 1)

    limits_cfg = snapshot.get("planning", {}).get("limits", {})
    max_bom_depth = int(limits_cfg.get("max_bom_depth", 200))

    mps_rows: List[ProductionPlanEntry] = (
        db.query(ProductionPlanEntry)
        .filter(ProductionPlanEntry.date >= d0, ProductionPlanEntry.date <= dmax)
        .all()
    )

    defaults: List[DefaultSpecification] = db.query(DefaultSpecification).all()
    default_spec_map: Dict[int, int] = {int(rec.item_id): int(rec.spec_id) for rec in defaults}

    spec_ids: Set[int] = set(default_spec_map.values())
    specs: List[Specification] = (
        db.query(Specification).filter(Specification.spec_id.in_(spec_ids)).all()
        if spec_ids
        else []
    )
    spec_by_id: Dict[int, Specification] = {s.spec_id: s for s in specs}

    kind_ids: Set[int] = {int(s.production_kind_id) for s in specs if s.production_kind_id}
    resource_kind_cache: Dict[int, List[ResourceProductionKind]] = defaultdict(list)
    if kind_ids:
        for rk in (
            db.query(ResourceProductionKind)
            .filter(ResourceProductionKind.production_kind_id.in_(kind_ids))
            .all()
        ):
            resource_kind_cache[int(rk.production_kind_id)].append(rk)

    resource_ids: Set[int] = {int(rk.resource_id) for lst in resource_kind_cache.values() for rk in lst}
    res_by_id: Dict[int, ProductionResource] = {}
    if resource_ids:
        resources = db.query(ProductionResource).filter(ProductionResource.resource_id.in_(resource_ids)).all()
        res_by_id = {int(res.resource_id): res for res in resources}

    components_cache: Dict[int, List[SpecComponent]] = {}

    def get_components_for_spec(spec_id: int) -> List[SpecComponent]:
        if spec_id in components_cache:
            return components_cache[spec_id]
        comps = db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()
        components_cache[spec_id] = comps
        return comps

    buffer_days_cache: Dict[int, int] = {}

    def resolve_buffer_days(item_id: int) -> int:
        if item_id in buffer_days_cache:
            return buffer_days_cache[item_id]
        spec_id = default_spec_map.get(item_id)
        buffer_val = 0
        if spec_id:
            spec = spec_by_id.get(spec_id)
            if spec and spec.production_kind_id:
                for rk in resource_kind_cache.get(int(spec.production_kind_id), []):
                    res = res_by_id.get(int(rk.resource_id))
                    if res and res.buffer_days:
                        try:
                            buffer_raw = float(res.buffer_days or 0.0)
                        except Exception:
                            buffer_raw = 0.0
                        if buffer_raw > 0:
                            buffer_val = int(buffer_raw)
                            break
        buffer_days_cache[item_id] = max(0, int(buffer_val))
        return buffer_days_cache[item_id]

    def clamp_to_horizon(dt: date) -> date:
        return d0 if dt < d0 else dt

    gross: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))

    def add_to_bucket(item_id: int, dt: date, qty: float) -> None:
        if qty <= 1e-9:
            return
        gross[item_id][dt] += qty

    def expand_bom(item_id: int, qty: float, bucket_date: date, path: Set[int], depth: int = 0) -> None:
        if qty <= 1e-9 or depth > max_bom_depth or item_id in path:
            return
        spec_id = default_spec_map.get(item_id)
        if not spec_id:
            return
        new_path = path | {item_id}
        for c in get_components_for_spec(spec_id):
            child_id, comp_qty = int(c.item_id), float(c.quantity or 0.0)
            if comp_qty <= 1e-9:
                continue
            child_qty = qty * comp_qty
            buffer_days = resolve_buffer_days(child_id)
            child_date = bucket_date
            if buffer_days > 0:
                child_date = clamp_to_horizon(bucket_date - timedelta(days=buffer_days))
            add_to_bucket(child_id, child_date, child_qty)
            expand_bom(child_id, child_qty, child_date, new_path, depth + 1)

    for r in mps_rows:
        root_qty = float(r.planned_qty or 0.0)
        if root_qty <= 1e-9:
            continue
        bucket_dt = r.date.date()
        root_item_id = int(r.item_id)
        add_to_bucket(root_item_id, bucket_dt, root_qty)
        expand_bom(root_item_id, root_qty, bucket_dt, set(), 0)

    factor = 1.0 + (ss_percent / 100.0)
    if abs(factor - 1.0) > 1e-9:
        for dmap in gross.values():
            for dt in dmap:
                dmap[dt] *= factor

    def serialize_bucket(bmap: DefaultDict[int, DefaultDict[date, float]]) -> Dict[str, Dict[str, float]]:
        return {
            str(iid): {dt.isoformat(): q for dt, q in sorted(dtmap.items())}
            for iid, dtmap in bmap.items()
        }

    gross_ser = serialize_bucket(gross)

    return {
        "meta": {
            "asOf": _read_last_stock_sync_at(),
            "d0": d0.isoformat(),
            "dmax": dmax.isoformat(),
        },
        "config": {
            "horizon_days": horizon,
            "safety_stock_percent": ss_percent,
            "config_version_id": int(cfg_id),
        },
        "snapshot": snapshot,
        "gross": gross_ser,
        "stats": {
            "items": len(gross),
            "buckets": sum(len(v) for v in gross.values()),
        },
    }


def compute_planning_preview(
    db: Session,
    horizon_days: Optional[int] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute gross+net requirements with *net-first BOM explosion*.

    Why:
      The previous implementation exploded BOM from *gross* demand, then netted each item independently.
      This can incorrectly generate net demand for components even when their parent is fully covered by stock/WIP
      (classic multi-level netting issue).

    New approach:
      1) Read root demand from production_plan_entries (MPS) for the horizon.
      2) For each BOM level:
         - net current level demand against stock/WIP
         - explode ONLY the residual (net) to components (with buffer_days shift)
      3) Accumulate gross/net maps across all levels.
    """

    # --- Resolve planning snapshot (same as compute_gross_requirements) ---
    try:
        cfg_id, cfg = get_active_planning_config(db)
    except Exception:
        cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)

    overrides: Dict[str, Any] = {}
    if horizon_days is not None:
        overrides["planning_horizon_days"] = int(horizon_days)
    if config_overrides:
        overrides = _deep_merge(overrides, config_overrides)
    snapshot = _deep_merge(cfg, overrides)

    horizon = int(snapshot.get("planning_horizon_days", 90))
    ss_percent = float(snapshot.get("safety_stock_percent", 1) or 0.0)

    d0: date = date.today()
    dmax: date = d0 + timedelta(days=max(1, horizon) - 1)

    limits_cfg = snapshot.get("planning", {}).get("limits", {})
    max_bom_depth = int(limits_cfg.get("max_bom_depth", 200))

    include_wip = bool(snapshot.get("toggles", {}).get("include_wip", True))

    # --- Root demand (MPS) ---
    # Note: we aggregate per (item_id, date) to avoid double-counting.
    mps_rows = (
        db.query(
            ProductionPlanEntry.item_id,
            func.date(ProductionPlanEntry.date).label("d"),
            func.sum(func.coalesce(ProductionPlanEntry.planned_qty, 0.0)).label("qty"),
        )
        .filter(ProductionPlanEntry.date >= d0, ProductionPlanEntry.date <= dmax)
        .group_by(ProductionPlanEntry.item_id, func.date(ProductionPlanEntry.date))
        .all()
    )

    factor = 1.0 + (ss_percent / 100.0)

    # demand_by_level: item_id -> {date -> qty}
    demand_map: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))
    for iid, dval, qty in mps_rows:
        try:
            q = float(qty or 0.0)
        except Exception:
            q = 0.0
        if q <= 1e-9:
            continue
        try:
            # func.date returns datetime.date in PG; keep robust fallback
            dt = dval if isinstance(dval, date) else _to_date(str(dval))
        except Exception:
            continue
        if abs(factor - 1.0) > 1e-9:
            q *= factor
        demand_map[int(iid)][dt] += q

    if not demand_map:
        return {
            "meta": {"asOf": _read_last_stock_sync_at(), "d0": d0.isoformat(), "dmax": dmax.isoformat()},
            "config": {"horizon_days": horizon, "safety_stock_percent": ss_percent, "config_version_id": int(cfg_id)},
            "snapshot": snapshot,
            "gross": {},
            "net": {},
            "stats": {"items": 0, "buckets": 0},
        }

    # --- Caches for BOM + buffer days ---
    defaults: List[DefaultSpecification] = db.query(DefaultSpecification).all()
    default_spec_map: Dict[int, int] = {int(rec.item_id): int(rec.spec_id) for rec in defaults}

    spec_ids: Set[int] = set(default_spec_map.values())
    specs: List[Specification] = (
        db.query(Specification).filter(Specification.spec_id.in_(spec_ids)).all() if spec_ids else []
    )
    spec_by_id: Dict[int, Specification] = {int(s.spec_id): s for s in specs}

    kind_ids: Set[int] = {int(s.production_kind_id) for s in specs if getattr(s, "production_kind_id", None)}
    resource_kind_cache: Dict[int, List[ResourceProductionKind]] = defaultdict(list)
    if kind_ids:
        for rk in (
            db.query(ResourceProductionKind)
            .filter(ResourceProductionKind.production_kind_id.in_(kind_ids))
            .all()
        ):
            resource_kind_cache[int(rk.production_kind_id)].append(rk)

    resource_ids: Set[int] = {int(rk.resource_id) for lst in resource_kind_cache.values() for rk in lst}
    res_by_id: Dict[int, ProductionResource] = {}
    if resource_ids:
        resources = db.query(ProductionResource).filter(ProductionResource.resource_id.in_(resource_ids)).all()
        res_by_id = {int(res.resource_id): res for res in resources}

    components_cache: Dict[int, List[SpecComponent]] = {}

    def get_components_for_spec(spec_id: int) -> List[SpecComponent]:
        if int(spec_id) in components_cache:
            return components_cache[int(spec_id)]
        comps = db.query(SpecComponent).filter(SpecComponent.spec_id == int(spec_id)).all()
        components_cache[int(spec_id)] = comps
        return comps

    stage_ids: Set[int] = set()
    try:
        for comp in db.query(SpecComponent.stage_id).filter(SpecComponent.stage_id.isnot(None)).all():
            try:
                stage_ids.add(int(comp[0] if isinstance(comp, (tuple, list)) else comp.stage_id))
            except Exception:
                continue
    except Exception:
        stage_ids = set()
    stage_name_by_id: Dict[int, str] = {}
    if stage_ids:
        try:
            for st in db.query(ProductionStage).filter(ProductionStage.stage_id.in_(list(stage_ids))).all():
                stage_name_by_id[int(st.stage_id)] = str(st.stage_name or "")
        except Exception:
            stage_name_by_id = {}

    kind_names: Dict[int, str] = {}
    if kind_ids:
        try:
            for kind in db.query(ProductionKind).filter(ProductionKind.id.in_(list(kind_ids))).all():
                kind_names[int(kind.id)] = str(kind.name or "")
        except Exception:
            kind_names = {}

    turning_item_cache: Dict[int, bool] = {}

    def is_turning_item(item_id: int) -> bool:
        item_key = int(item_id)
        if item_key in turning_item_cache:
            return turning_item_cache[item_key]
        result = False
        spec_id = default_spec_map.get(item_key)
        spec = spec_by_id.get(int(spec_id)) if spec_id else None
        kind_id = int(spec.production_kind_id) if spec and getattr(spec, "production_kind_id", None) else None
        if kind_id is not None:
            kind_name = str(kind_names.get(kind_id, "") or "").strip().casefold()
            if "токар" in kind_name:
                result = True
            if not result:
                for rk in resource_kind_cache.get(kind_id, []):
                    res = res_by_id.get(int(rk.resource_id))
                    res_name = str(getattr(res, "resource_name", "") or "").strip().casefold() if res else ""
                    if "токар" in res_name:
                        result = True
                        break
        turning_item_cache[item_key] = result
        return result

    def select_turning_blank_components(comps: List[SpecComponent]) -> List[SpecComponent]:
        staged = []
        for comp in comps or []:
            try:
                stage_id = getattr(comp, "stage_id", None)
                stage_name = stage_name_by_id.get(int(stage_id), "") if stage_id is not None else ""
            except Exception:
                stage_name = ""
            if "заготов" in str(stage_name or "").casefold():
                staged.append(comp)
        return staged or list(comps or [])

    buffer_days_cache: Dict[int, int] = {}

    def resolve_buffer_days(item_id: int) -> int:
        if int(item_id) in buffer_days_cache:
            return buffer_days_cache[int(item_id)]
        spec_id = default_spec_map.get(int(item_id))
        buffer_val = 0
        if spec_id:
            spec = spec_by_id.get(int(spec_id))
            if spec and getattr(spec, "production_kind_id", None):
                for rk in resource_kind_cache.get(int(spec.production_kind_id), []):
                    res = res_by_id.get(int(rk.resource_id))
                    if res and getattr(res, "buffer_days", None):
                        try:
                            buffer_raw = float(res.buffer_days or 0.0)
                        except Exception:
                            buffer_raw = 0.0
                        if buffer_raw > 0:
                            buffer_val = int(buffer_raw)
                            break
        buffer_days_cache[int(item_id)] = max(0, int(buffer_val))
        return buffer_days_cache[int(item_id)]

    def clamp_to_horizon(dt: date) -> date:
        return d0 if dt < d0 else dt

    # --- Availability (stock + WIP) ---
    # Effective stock with `ignored_warehouses` excluded — Item.stock_qty
    # alone would let the MRP see stock parked in brak/isolator warehouses,
    # which production control later refuses as a source for material issues.
    stock_by_item: Dict[int, float] = _effective_stock_by_item_all(db)

    # WIP keyed by planned_finish_date so the netting respects when the WIP
    # is physically available. A WIP order finishing in September must NOT
    # cover a July demand bucket. The earlier implementation used .quantity
    # (not remaining_qty), without any active-state filter, and treated WIP
    # as timeless — leading to systematic under-planning.
    wip_eta_by_item: Dict[int, list] = {}
    if include_wip:
        try:
            wip_eta_by_item = _active_wip_eta_by_item(db)
        except Exception:
            wip_eta_by_item = {}

    # Per-item working pools that are mutated during the netting loop.
    avail_stock: Dict[int, float] = {}
    avail_wip: Dict[int, list] = {}

    def ensure_availability(item_ids: Set[int]) -> None:
        for i in item_ids:
            iid = int(i)
            if iid not in avail_stock:
                avail_stock[iid] = float(stock_by_item.get(iid, 0.0) or 0.0)
            if include_wip and iid not in avail_wip:
                avail_wip[iid] = list(wip_eta_by_item.get(iid, []))

    # --- Multi-level net-first explosion ---
    gross_map: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))
    net_map: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))
    warnings: List[Dict[str, Any]] = []

    for depth in range(max(1, max_bom_depth)):
        if not demand_map:
            break

        current_item_ids: Set[int] = set(int(i) for i in demand_map.keys())
        ensure_availability(current_item_ids)

        next_demand: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))

        for iid in sorted(current_item_ids):
            buckets = demand_map.get(int(iid), {}) or {}
            if not buckets:
                continue

            # netting in chronological order:
            #   1) consume free stock (timeless),
            #   2) then WIP whose planned_finish_date <= bucket_date.
            iid_int = int(iid)
            stock_left = float(avail_stock.get(iid_int, 0.0) or 0.0)
            wip_list = avail_wip.setdefault(iid_int, [])
            net_buckets: List[Tuple[date, float]] = []

            for bucket_date, bucket_qty in sorted(buckets.items(), key=lambda x: x[0]):
                q = float(bucket_qty or 0.0)
                if q <= 1e-9:
                    continue
                gross_map[iid_int][bucket_date] += q
                # 1) Stock first.
                if stock_left >= q:
                    stock_left -= q
                    continue
                residual = q - stock_left
                stock_left = 0.0
                # 2) Then WIP whose ETA is at or before this bucket.
                if include_wip:
                    residual = _consume_wip_at_or_before(wip_list, bucket_date, residual)
                if residual <= 1e-9:
                    continue
                net_buckets.append((bucket_date, residual))

            avail_stock[iid_int] = stock_left

            if not net_buckets:
                continue

            turning_parent = is_turning_item(int(iid))
            if turning_parent and len(net_buckets) > 1:
                first_date = min(bucket_date for bucket_date, _ in net_buckets)
                total_net_qty = sum(float(q or 0.0) for _, q in net_buckets)
                net_buckets = [(first_date, total_net_qty)]

            for bucket_date, net_q in net_buckets:
                net_map[int(iid)][bucket_date] += float(net_q or 0.0)

            # explode only residual/net demand
            spec_id = default_spec_map.get(int(iid))
            if not spec_id:
                continue
            comps = get_components_for_spec(int(spec_id))
            if not comps:
                continue
            priority_blank_comps = select_turning_blank_components(comps) if turning_parent else []
            priority_blank_ids = {int(getattr(comp, "item_id")) for comp in priority_blank_comps if getattr(comp, "item_id", None) is not None}

            for bucket_date, net_q in net_buckets:
                for comp in comps:
                    try:
                        child_id = int(getattr(comp, "item_id"))
                        per_unit = float(getattr(comp, "quantity", 0.0) or 0.0)
                    except Exception:
                        continue
                    if per_unit <= 1e-12:
                        continue
                    child_qty = float(net_q) * float(per_unit)
                    if child_qty <= 1e-9:
                        continue
                    child_date = bucket_date
                    is_priority_blank = turning_parent and child_id in priority_blank_ids
                    if not is_priority_blank:
                        # Classical MRP lead-time offset: shift the child's
                        # need_date back by the PARENT's production time
                        # (`resolve_buffer_days(int(iid))`). The child's
                        # own lead time will apply when the child is itself
                        # exploded one BFS level deeper — the BFS accumulates
                        # the buffer chain across the BOM correctly.
                        # Earlier this used `resolve_buffer_days(child_id)`,
                        # which shifted by the wrong link and dropped the
                        # parent's lead time at every level. Turning blanks
                        # remain pinned to the parent's bucket — they ARE
                        # the parent's first operation.
                        buf = resolve_buffer_days(int(iid))
                        if buf > 0:
                            child_date = clamp_to_horizon(bucket_date - timedelta(days=int(buf)))
                    next_demand[int(child_id)][child_date] += child_qty
                    if is_priority_blank:
                        warnings.append(
                            make_warning(
                                "TURNING_BLANK_PRIORITY",
                                "Заготовка под токарный участок",
                                item_id=int(child_id),
                                parent_item_id=int(iid),
                                qty=float(child_qty),
                                need_date=child_date.isoformat(),
                                parent_need_date=bucket_date.isoformat(),
                            )
                        )

        demand_map = next_demand

    def serialize_bucket(bmap: DefaultDict[int, DefaultDict[date, float]]) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for iid, dtmap in bmap.items():
            if not dtmap:
                continue
            out[str(int(iid))] = {dt.isoformat(): float(q or 0.0) for dt, q in sorted(dtmap.items()) if float(q or 0.0) > 1e-9}
        return out

    gross_ser = serialize_bucket(gross_map)
    net_ser = serialize_bucket(net_map)

    return {
        "meta": {
            "asOf": _read_last_stock_sync_at(),
            "d0": d0.isoformat(),
            "dmax": dmax.isoformat(),
        },
        "config": {
            "horizon_days": horizon,
            "safety_stock_percent": ss_percent,
            "config_version_id": int(cfg_id),
        },
        "snapshot": snapshot,
        "gross": gross_ser,
        "net": net_ser,
        "stats": {
            "items": int(len(gross_ser)),
            "buckets": int(sum(len(v) for v in gross_ser.values())),
        },
        "warnings": warnings,
    }


def build_planned_orders_and_purchases(
    db: Session,
    run: PlanningRun,
    net_requirements: Dict[str, Any],
    order_qty_calculator: OrderQuantityCalculator,
    priority_manager: PriorityManager,
    item_cache: Dict[int, Item],
    units_by_ref: Dict[str, Unit],
    active_remaining_by_item: Optional[Dict[int, float]] = None,
    supplier_remaining_by_item_date: Optional[Dict[int, List[Tuple[date, float]]]] = None,
) -> Dict[str, Any]:
    
    run_id = run.run_id
    config = run.config_snapshot
    warnings = []
    created_orders = []
    created_purchases = []
    created_reworks = []
    active_remaining_by_item = active_remaining_by_item or {}
    supplier_remaining_by_item_date = supplier_remaining_by_item_date or {}
    supplier_remaining_work: Dict[int, List[Dict[str, Any]]] = {
        int(iid): [
            {"delivery_date": delivery_date, "remaining_qty": float(qty or 0.0)}
            for delivery_date, qty in sorted(rows, key=lambda x: x[0])
            if float(qty or 0.0) > 1e-12
        ]
        for iid, rows in supplier_remaining_by_item_date.items()
    }

    all_reqs = []
    for item_id_str, buckets in net_requirements.items():
        for need_date_str, qty in buckets.items():
            all_reqs.append(
                {
                    "item_id": int(item_id_str),
                    "need_date": _to_date(need_date_str),
                    "qty": float(qty),
                }
            )

    def consume_component_stock(parent_item_id: int, planned_parent_qty: float) -> None:
        """
        Consume direct BOM components from the calculator stock cache.
        This keeps component gating cumulative across chronological buckets
        within the same run.
        """
        try:
            parent_qty = float(planned_parent_qty or 0.0)
        except Exception:
            parent_qty = 0.0
        if parent_qty <= 1e-9:
            return

        try:
            spec_id = getattr(order_qty_calculator, "default_spec_map", {}).get(int(parent_item_id))
        except Exception:
            spec_id = None
        if not spec_id:
            return

        try:
            comps = order_qty_calculator.components_loader(int(spec_id)) or []
        except Exception:
            return

        for comp in comps:
            try:
                child_id = int(getattr(comp, "item_id"))
                per_unit = float(getattr(comp, "quantity", 0.0) or 0.0)
            except Exception:
                continue
            if per_unit <= 1e-12:
                continue

            consume_qty = parent_qty * per_unit
            if consume_qty <= 1e-12:
                continue

            base_stock = float(getattr(order_qty_calculator, "stock_by_item", {}).get(child_id, 0.0) or 0.0)
            order_qty_calculator.stock_by_item[child_id] = max(base_stock - consume_qty, 0.0)

    def consume_supplier_order_coverage(item_id: int, need_date: date, requested_qty: float) -> float:
        """
        Consume already placed supplier orders that arrive no later than need_date.
        The local mutation prevents one supplier-order row from covering several MRP buckets twice.
        """
        remaining_need = float(requested_qty or 0.0)
        if remaining_need <= 1e-12:
            return 0.0
        rows = supplier_remaining_work.get(int(item_id), [])
        if not rows:
            return remaining_need

        for row in rows:
            if remaining_need <= 1e-12:
                break
            delivery_date = row.get("delivery_date")
            if delivery_date is None or delivery_date > need_date:
                continue
            available_qty = float(row.get("remaining_qty", 0.0) or 0.0)
            if available_qty <= 1e-12:
                continue
            used_qty = min(available_qty, remaining_need)
            row["remaining_qty"] = max(available_qty - used_qty, 0.0)
            remaining_need = max(remaining_need - used_qty, 0.0)

        return remaining_need

    for req in sorted(all_reqs, key=lambda x: x["need_date"]):
        item_id = req["item_id"]
        need_date = req["need_date"]
        requested_qty_raw = float(req["qty"] or 0.0)
        requested_qty = float(requested_qty_raw)
        
        item = item_cache.get(item_id)
        if not item:
            w = log_warning(
                logger,
                "ITEM_NOT_FOUND",
                "Номенклатура не найдена в кэше при построении заказов",
                item_id=item_id,
            )
            warnings.append(w)
            continue

        flow = classify_replenishment_flow(getattr(item, "replenishment_method", None))
        is_purchase = flow == REPLENISHMENT_FLOW_PURCHASE
        is_rework = flow == REPLENISHMENT_FLOW_REWORK
        is_produced = (not is_purchase) and (not is_rework)
        
        if is_produced:
            # NOTE: WIP/active-production netting is already applied upstream
            # in compute_planning_preview (which subtracts remaining_qty of
            # active orders from gross demand chronologically per bucket).
            # Subtracting `active_remaining_by_item` here would double-count
            # WIP — and worse, the per-item amount is read fresh for every
            # bucket without being consumed, so every bucket of a multi-bucket
            # item would receive full WIP credit. The argument is kept on the
            # signature for backward compatibility but no longer used for
            # production-flow netting.
            requested_qty = float(requested_qty_raw)
            if requested_qty <= 1e-9:
                continue

            # Compute quantity with diagnostics (component_limit + horizon_limit)
            final_qty_before, normalized_qty, comp_details, comp_warnings = order_qty_calculator.compute(item_id, requested_qty)
            warnings.extend(comp_warnings)

            horizon_limit = float(comp_details.get("horizon_limit", float(requested_qty)))
            component_limit = float(comp_details.get("component_limit", float(requested_qty)))
            desired_qty = min(float(normalized_qty or 0.0), horizon_limit)

            # Requested quantity is normalized via shared calculator helper.
            requested_qty = float(order_qty_calculator.normalize_qty_for_item(item_id, final_qty_before))

            # Component gating:
            # - If component_limit <= 0 -> do NOT create PlannedOrder. Record a blocking warning.
            if component_limit <= 1e-9:
                warnings.append(
                    make_warning(
                        "COMPONENT_SHORTAGE_BLOCKED",
                        "Заказ заблокирован из-за дефицита комплектующих",
                        run_id=run_id,
                        item_id=int(item_id),
                        requested_qty=float(requested_qty),
                        need_date=need_date.isoformat(),
                    )
                )
                # Skip creation entirely (no qty=0 rows)
                continue

            # - If components cannot cover the horizon-capped lot-sized order, plan partial.
            if component_limit + 1e-9 < float(desired_qty):
                planned_qty = min(component_limit, desired_qty)
                warnings.append(
                    make_warning(
                        "COMPONENT_SHORTAGE_PARTIAL",
                        "Частичное планирование из-за дефицита комплектующих",
                        item_id=int(item_id),
                        requested_qty=float(requested_qty),
                        planned_qty=float(planned_qty),
                        component_limit=float(component_limit),
                        desired_qty=float(desired_qty),
                    )
                )
            else:
                # Otherwise, use lot sizing capped only by horizon demand and components.
                planned_qty = min(desired_qty, component_limit)

            planned_qty = float(planned_qty or 0.0)

            # Enforce shared normalization policy for the created production qty.
            planned_qty = float(order_qty_calculator.normalize_qty_for_item(item_id, planned_qty))

            if planned_qty <= 1e-9:
                # Safety: avoid creating qty=0 rows for any reason
                continue

            order = PlannedOrder(
                run_id=run_id,
                item_id=item_id,
                requested_qty=requested_qty,
                planned_qty=planned_qty,
                qty=planned_qty,
                need_date=need_date,
                bucket_date=need_date,
            )
            created_orders.append(order)
            consume_component_stock(parent_item_id=int(item_id), planned_parent_qty=float(planned_qty))
        elif is_purchase:
            lead_time = item.replenishment_time or 30
            order_date = need_date - timedelta(days=lead_time)
            # Keep the original (pre-supplier-netting) demand for diagnostics:
            # the UI «Покрыто поставщиком» indicator derives supplier coverage
            # as `requested_qty - qty`, so requested_qty MUST stay as the gross
            # net demand. Overwriting it with the post-netting residual makes
            # supplier_covered_qty always equal 0.
            net_demand_for_period = float(requested_qty_raw)
            residual_after_supplier = consume_supplier_order_coverage(
                item_id=int(item_id),
                need_date=need_date,
                requested_qty=net_demand_for_period,
            )
            # Purchase flow uses the same shared quantity normalization layer
            # as production for the final business quantity:
            # - discrete units -> fractional part is removed
            # - metric/non-discrete units -> fractional value is preserved
            planned_qty = float(order_qty_calculator.normalize_qty_for_item(item_id, residual_after_supplier))
            if planned_qty <= 1e-9:
                continue
            # Normalize the diagnostic original-demand too so the unit policy
            # stays consistent on display.
            requested_qty = float(order_qty_calculator.normalize_qty_for_item(item_id, net_demand_for_period))
            purchase = PlannedPurchase(
                run_id=run_id,
                item_id=item_id,
                requested_qty=requested_qty,
                planned_qty=planned_qty,
                qty=planned_qty,
                need_date=need_date,
                order_date=order_date,
                lead_time_days=lead_time,
                bucket_date=need_date,
                supplier_ref1c=getattr(item, 'supplier_ref1c', None),
            )
            created_purchases.append(purchase)
        else:  # rework
            lead_time = item.replenishment_time or 0
            order_date = need_date - timedelta(days=lead_time)

            final_qty_before, normalized_qty, comp_details, comp_warnings = order_qty_calculator.compute(item_id, requested_qty)
            warnings.extend(comp_warnings)

            horizon_limit = float(comp_details.get("horizon_limit", float(requested_qty)))
            component_limit = float(comp_details.get("component_limit", float(requested_qty)))
            desired_qty = min(float(normalized_qty or 0.0), horizon_limit)
            requested_qty = float(order_qty_calculator.normalize_qty_for_item(item_id, final_qty_before))

            spec_id = getattr(order_qty_calculator, "default_spec_map", {}).get(int(item_id))
            shortage_payload = {
                "requested_qty": float(requested_qty),
                "normalized_qty": float(normalized_qty or 0.0),
                "horizon_limit": float(horizon_limit),
                "component_limit": float(component_limit),
            }

            component_blocked = component_limit <= 1e-9
            component_partial = (component_limit > 1e-9) and (component_limit + 1e-9 < float(desired_qty))

            if component_blocked:
                warnings.append(
                    make_warning(
                        "REWORK_COMPONENT_SHORTAGE_BLOCKED",
                        "Заказ на переработку заблокирован из-за дефицита комплектующих",
                        run_id=run_id,
                        item_id=int(item_id),
                        requested_qty=float(requested_qty),
                        need_date=need_date.isoformat(),
                        spec_id=int(spec_id) if spec_id is not None else None,
                    )
                )
                planned_qty = 0.0
            elif component_partial:
                planned_qty = min(component_limit, desired_qty)
                warnings.append(
                    make_warning(
                        "REWORK_COMPONENT_SHORTAGE_PARTIAL",
                        "Заказ на переработку частично ограничен дефицитом комплектующих",
                        run_id=run_id,
                        item_id=int(item_id),
                        requested_qty=float(requested_qty),
                        planned_qty=float(planned_qty),
                        component_limit=float(component_limit),
                        desired_qty=float(desired_qty),
                        need_date=need_date.isoformat(),
                        spec_id=int(spec_id) if spec_id is not None else None,
                    )
                )
            else:
                planned_qty = min(desired_qty, component_limit)

            planned_qty = float(order_qty_calculator.normalize_qty_for_item(item_id, float(planned_qty or 0.0)))
            shortage_payload["planned_qty"] = float(planned_qty)

            rework = PlannedRework(
                run_id=run_id,
                item_id=item_id,
                spec_id=spec_id,
                requested_qty=requested_qty,
                planned_qty=planned_qty,
                qty=planned_qty,
                need_date=need_date,
                order_date=order_date,
                lead_time_days=lead_time,
                bucket_date=need_date,
                component_limit=component_limit,
                component_blocked=bool(component_blocked),
                component_partial=bool(component_partial),
                shortage=shortage_payload,
            )
            created_reworks.append(rework)
            consume_component_stock(parent_item_id=int(item_id), planned_parent_qty=float(planned_qty))

    db.add_all(created_orders)
    db.add_all(created_purchases)
    db.add_all(created_reworks)
    db.flush()

    # Assign priorities after creation
    all_orders_for_prio = db.query(PlannedOrder).filter(PlannedOrder.run_id == run_id).all()
    all_purchases_for_prio = db.query(PlannedPurchase).filter(PlannedPurchase.run_id == run_id).all()

    # This needs more context from the main run function, which is not available here.
    # The new design of PriorityManager requires more data.
    # For now, we will skip priority assignment in this refactoring.
    # A proper implementation would require passing down more data.
    
    # priority_manager.assign_purchase_priorities(all_purchases_for_prio)
    # order_priorities = priority_manager.compute_order_priorities(...)
    # for order in all_orders_for_prio:
    #     order.priority_index = order_priorities.get(order.order_id, 0.0)

    return {"warnings": warnings}


def build_order_stages(
    db: Session,
    run: PlanningRun,
    spec_cache: Dict[int, Specification],
    kind_cache: Dict[int, ProductionKind],
    resource_kind_cache: Dict[int, List[ResourceProductionKind]],
    op_cache: Dict[int, Operation],
    spec_op_cache: Dict[int, List[SpecOperation]],
) -> Dict[str, Any]:
    
    run_id = run.run_id
    warnings = []
    
    orders = db.query(PlannedOrder).filter(PlannedOrder.run_id == run_id).all()
    
    for order in orders:
        spec = spec_cache.get(order.item_id)
        if not spec:
            w = log_warning(
                logger,
                "NO_DEFAULT_SPECIFICATION",
                "Нет спецификации по умолчанию для изделия при построении этапов заказа",
                item_id=order.item_id,
            )
            warnings.append(w)
            continue
        
        order.spec_id = spec.spec_id
        
        spec_ops = spec_op_cache.get(spec.spec_id, [])
        if not spec_ops:
            w = log_warning(
                logger,
                "SPEC_HAS_NO_OPERATIONS",
                "Спецификация не содержит операций при построении этапов заказа",
                item_id=order.item_id,
                spec_id=spec.spec_id,
            )
            warnings.append(w)
            continue

        for spec_op in spec_ops:
            op = op_cache.get(spec_op.operation_id)
            if not op:
                logger.debug(
                    "Order stage skipped due to missing operation",
                    extra={
                        "order_id": order.order_id,
                        "spec_id": spec.spec_id,
                        "spec_operation_id": spec_op.spec_operation_id,
                        "operation_id": spec_op.operation_id,
                    },
                )
                continue

            kind = kind_cache.get(spec.production_kind_id)
            if not kind:
                w = log_warning(
                    logger,
                    "PRODUCTION_KIND_NOT_FOUND",
                    "Не найден вид производства для операции при построении этапов заказа",
                    production_kind_id=spec_op.production_kind_id,
                    spec_id=spec.spec_id,
                    operation_id=spec_op.operation_id,
                )
                warnings.append(w)
                logger.debug(
                    "Order stage skipped due to missing production kind",
                    extra={
                        "order_id": order.order_id,
                        "spec_id": spec.spec_id,
                        "spec_operation_id": spec_op.spec_operation_id,
                        "spec_production_kind_id": spec.production_kind_id,
                        "op_production_kind_id": spec_op.production_kind_id,
                    },
                )
                continue
            
            if not spec_op.stage_id:
                logger.debug(
                    "Order stage skipped due to missing stage reference",
                    extra={
                        "order_id": order.order_id,
                        "spec_id": spec.spec_id,
                        "spec_operation_id": spec_op.spec_operation_id,
                        "operation_id": op.operation_id,
                    },
                )
                continue

            allowed_resources = resource_kind_cache.get(spec.production_kind_id, [])
            resource_kind = allowed_resources[0] if allowed_resources else None
            # Если у вида производства нет ни одной привязки к участку — это проблема входящих данных.
            # Предупреждение NO_AREA_FOR_PRODUCTION_KIND показывается на фронтенде и странице разбора привязок.
            if not allowed_resources:
                try:
                    w = log_warning(
                        logger,
                        "NO_AREA_FOR_PRODUCTION_KIND",
                        "Нет привязки вида производства к участкам",
                        run_id=run_id,
                        item_id=int(order.item_id),
                        spec_id=int(spec.spec_id),
                        spec_code=getattr(spec, "spec_code", None),
                        spec_name=getattr(spec, "spec_name", None),
                        spec_ref1c=getattr(spec, "spec_ref1c", None),
                        production_kind_id=int(spec.production_kind_id) if getattr(spec, "production_kind_id", None) else None,
                        production_kind_name=getattr(kind, "name", None) if kind else None,
                    )
                    warnings.append(w)
                except Exception:
                    # Диагностика не должна ломать расчёт
                    pass
            logger.debug(
                "Order stage build candidate",
                extra={
                    "order_id": order.order_id,
                    "spec_id": spec.spec_id,
                    "spec_operation_id": spec_op.spec_operation_id,
                    "operation_id": op.operation_id,
                    "spec_stage_id": spec_op.stage_id,
                    "resolved_stage_id": spec_op.stage_id,
                    "spec_production_kind_id": spec.production_kind_id,
                    "resolved_resource_kind_id": resource_kind.id if resource_kind else None,
                    "resolved_area_id": resource_kind.resource_id if resource_kind else None,
                },
            )
            
            # Area comes from the production kind only. No stage fallback:
            # an unbound kind stays visible as NO_AREA_FOR_PRODUCTION_KIND
            # instead of being silently routed by ResourceStage.
            area_resolved = resource_kind.resource_id if resource_kind else None

            # Приводим типы к float, так как значения из БД приходят как Decimal
            norm_hours_per_unit_raw = spec_op.time_norm or op.time_norm or 0.0
            try:
                norm_hours_per_unit = float(norm_hours_per_unit_raw or 0.0)
            except Exception:
                norm_hours_per_unit = 0.0

            qty_f = float(order.qty or 0.0)

            stage = PlannedOrderStage(
                run_id=run_id,
                order_id=order.order_id,
                stage_id=spec_op.stage_id,
                area_id=area_resolved,
                hours=qty_f * norm_hours_per_unit,
                bucket_date=order.bucket_date, # Add bucket_date
            )
            db.add(stage)
            
    return {"warnings": warnings}


def apply_capacity_constraints(
    db: Session,
    run: PlanningRun,
    capacity_scheduler: CapacityScheduler,
) -> Dict[str, Any]:
    run_id = run.run_id
    warnings = []

    orders_to_schedule = (
        db.query(PlannedOrder)
        .filter(PlannedOrder.run_id == run_id)
        .order_by(desc(PlannedOrder.priority_index), PlannedOrder.need_date)
        .all()
    )

    # Stages per order (built in PHASE 2).
    stages_by_order: Dict[int, List[PlannedOrderStage]] = defaultdict(list)
    for s in db.query(PlannedOrderStage).filter(PlannedOrderStage.run_id == run_id).all():
        stages_by_order[int(s.order_id)].append(s)

    # child→parent map among the items being scheduled: a parent's default-spec
    # components are its children, so the component must be ready first.
    order_item_ids = {int(o.item_id) for o in orders_to_schedule}
    default_spec_by_item = {
        int(ds.item_id): int(ds.spec_id)
        for ds in db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id.in_(order_item_ids))
        .all()
    } if order_item_ids else {}
    parents_of_item: Dict[int, Set[int]] = {}
    if default_spec_by_item:
        spec_to_parent = {sid: iid for iid, sid in default_spec_by_item.items()}
        for spec_id, comp_item in (
            db.query(SpecComponent.spec_id, SpecComponent.item_id)
            .filter(SpecComponent.spec_id.in_(set(default_spec_by_item.values())))
            .all()
        ):
            parent = spec_to_parent.get(int(spec_id))
            child = int(comp_item)
            if parent is not None and child in order_item_ids:
                parents_of_item.setdefault(child, set()).add(int(parent))

    # Build the batch and keep analytic CAPACITY_LIMITED warnings (no qty change).
    batch: List[Dict[str, Any]] = []
    order_by_key: Dict[int, PlannedOrder] = {}
    for order in orders_to_schedule:
        stages = stages_by_order.get(int(order.order_id), [])
        if not stages:
            continue
        stage_hours = {int(s.stage_id): float(s.hours or 0.0) for s in stages}
        stage_areas = {int(s.stage_id): (int(s.area_id) if s.area_id is not None else None) for s in stages}
        try:
            _, _, limit_warnings = capacity_scheduler.limit_qty_by_capacity(
                order.item_id, order.qty, order.need_date, stage_hours, stage_areas_by_stage=stage_areas
            )
            warnings.extend(limit_warnings)
        except Exception:
            pass
        order_by_key[int(order.order_id)] = order
        batch.append({
            "key": int(order.order_id),
            "item_id": int(order.item_id),
            "qty": float(order.qty or 0.0),
            "need_date": order.need_date,
            "stage_hours": stage_hours,
            "stage_areas": stage_areas,
            "priority": float(order.priority_index or 0.0),
            "fixed": False,
        })

    # Capacity-aware, child→parent-aware scheduling (parents first; a component
    # finishes before the assembly that consumes it; push-right never before today).
    results, schedule_warnings = capacity_scheduler.schedule_orders_bom_aware(batch, parents_of_item)
    for w in schedule_warnings:
        try:
            w.setdefault("run_id", int(run_id))
        except Exception:
            pass
    warnings.extend(schedule_warnings)

    for okey, schedule_result in results.items():
        order = order_by_key.get(int(okey))
        if order is None:
            continue
        order.start_date = schedule_result.get("order_start_date")
        order.finish_date = schedule_result.get("order_finish_date")
        for stage in stages_by_order.get(int(okey), []):
            stage_dates = schedule_result.get("stage_dates", {}).get(stage.stage_id)
            if stage_dates:
                stage.start_date = stage_dates["start"]
                stage.finish_date = stage_dates["finish"]
                stage.bucket_date = stage_dates["start"].date() if stage_dates.get("start") else order.bucket_date

    # 3. Aggregate capacity load at the very end
    capacity_loads = capacity_scheduler.get_aggregated_load()
    for (area_id, bucket_date), load_info in capacity_loads.items():
        db.add(CapacityLoad(
            run_id=run_id,
            area_id=area_id,
            bucket_date=bucket_date,
            hours_planned=load_info["planned"],
            hours_available=load_info["available"],
            overload_hours=max(0, load_info["planned"] - load_info["available"])
        ))

    return {"warnings": warnings}
