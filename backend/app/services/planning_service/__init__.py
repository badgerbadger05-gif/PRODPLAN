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


from .constants import (
    logger,
    _REF1C_RE,
    DEFAULT_PLANNING_CONFIG,
    SERVER_MAX_LIMIT,
    DEFAULT_PAGE_LIMIT,
    DONE_STATE_KEY,
    SUPPLIER_ORDER_EXCLUDED_STATE_NAMES,
)

from .helpers import (
    _unit_display_from_parts,
    _bom_descendant_ids_for_roots,
    _load_stage_area_context,
    _load_purchase_area_map,
    _load_production_area_map,
    _production_supply_qty_expr,
    _ensure_dict,
    _load_turning_blank_priority_map,
    _turning_blank_badge,
    _deep_merge,
    _to_date,
    _read_last_stock_sync_at,
    _load_item_category_meta,
    _get_active_production_remaining_by_item,
    _get_active_1c_remaining_by_item,
    _get_active_supplier_remaining_by_item_date,
    _load_late_supplier_order_coverage,
    _late_supplier_order_badge,
    _merge_badges,
    _build_component_reservations_from_active_1c,
    _generate_shortage_report_v2,
)

from .config import (
    get_active_planning_config,
    list_planning_configs,
    create_planning_config_version,
    activate_planning_config_version,
    get_active_planning_config_full,
)

from .runs import (
    _get_or_create_run,
    _clear_run_outputs,
    list_planning_runs,
    get_run_summary,
)

from .run_views import (
    get_run_production,
    get_run_purchases,
    get_run_purchases_grouped_by_category,
    _query_run_rework_rows,
    get_run_rework,
    get_run_rework_grouped,
    get_run_rework_grouped_by_category,
    get_run_production_grouped,
    get_run_capacity,
    get_run_pegging,
)

from .core import (
    compute_gross_requirements,
    compute_planning_preview,
    build_planned_orders_and_purchases,
    build_order_stages,
    apply_capacity_constraints,
)


# ---------------------------------------------------------------------------
# run_planning_run is defined at package level (not in .core) so that its global
# namespace IS the public package namespace. This preserves the pre-split
# behaviour where tests/consumers monkeypatch app.services.planning_service.
# compute_planning_preview (and similar) and the entry point observes the patch.
# ---------------------------------------------------------------------------

def run_planning_run(
    db: Session,
    run_id: Optional[int] = None,
    horizon_days: Optional[int] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
    started_by: Optional[str] = None,
) -> int:
    
    run = _get_or_create_run(db, run_id, horizon_days, config_overrides, started_by)

    # Recomputing an existing run must be idempotent: drop the previous
    # outputs and reset the header to IN_PROGRESS before producing new rows.
    if run_id:
        _clear_run_outputs(db, run.run_id)
        run.status = "IN_PROGRESS"
        run.warnings = []
        run.finished_at = None

    # Commit the run header (and any clearing) up front so it survives a
    # rollback of partial work on failure below.
    db.commit()

    try:
        # --- PREPARATION ---
        net_req_result = compute_planning_preview(db, run.horizon_days, run.config_snapshot)
        net_requirements = net_req_result.get("net", {})
        
        all_item_ids = {int(item_id) for item_id in net_requirements.keys()}
        items = db.query(Item).filter(Item.item_id.in_(all_item_ids)).all()
        item_cache = {i.item_id: i for i in items}

        # Collect all necessary data for calculators
        all_specs_list = db.query(Specification).all()
        spec_by_id = {s.spec_id: s for s in all_specs_list}
        
        default_specs = db.query(DefaultSpecification).all()
        default_spec_map = {ds.item_id: ds.spec_id for ds in default_specs}

        def components_loader(spec_id: int) -> List[SpecComponent]:
            return db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()

        # A) Active production orders as already planned finished output.
        # Covers both 1C-synced orders and internal MRP-originated ones
        # (source='mrp' from /v1/production-control/orders/from-mrp), per plan
        # rule "эти заказы учитываются в следующих MRP-расчетах".
        active_remaining_by_item = _get_active_production_remaining_by_item(db)
        supplier_remaining_by_item_date = _get_active_supplier_remaining_by_item_date(db)

        # B) Active 1C orders reserve components recursively across full BOM depth.
        limits_cfg = (run.config_snapshot or {}).get("planning", {}).get("limits", {})
        max_bom_depth = int(limits_cfg.get("max_bom_depth", 200) or 200)
        reserved_by_component, reserve_warnings = _build_component_reservations_from_active_1c(
            db=db,
            default_spec_map=default_spec_map,
            components_loader=components_loader,
            max_depth=max_bom_depth,
        )

        all_resources = db.query(ProductionResource).all()
        res_by_id = {r.resource_id: r for r in all_resources}

        all_res_kinds = db.query(ResourceProductionKind).all()
        production_kinds_by_resource = defaultdict(set)
        for rk in all_res_kinds:
            production_kinds_by_resource[rk.resource_id].add(rk.production_kind_id)

        # IMPORTANT:
        # stock_by_item must include not only items with net requirements, but also their BOM components.
        # Otherwise, components that are fully covered by stock (net=0) are absent from net_requirements,
        # absent from item_cache, and thus treated as 0 in OrderQuantityCalculator._limit_by_components().
        component_item_ids: Set[int] = set()
        try:
            spec_ids_for_run: Set[int] = set()
            for iid in all_item_ids:
                sid = default_spec_map.get(int(iid))
                if sid:
                    spec_ids_for_run.add(int(sid))
            if spec_ids_for_run:
                comp_rows = (
                    db.query(SpecComponent.item_id)
                    .filter(SpecComponent.spec_id.in_(list(spec_ids_for_run)))
                    .all()
                )
                for (cid,) in comp_rows:
                    try:
                        component_item_ids.add(int(cid))
                    except Exception:
                        continue
        except Exception as ex:
            logger.exception("Failed to prefetch component ids for stock cache: %s", ex)
            component_item_ids = set()

        stock_item_ids: Set[int] = set(all_item_ids) | set(component_item_ids) | set(reserved_by_component.keys())
        stock_by_item: Dict[int, float] = {}
        if stock_item_ids:
            try:
                stock_rows = (
                    db.query(Item.item_id, Item.stock_qty)
                    .filter(Item.item_id.in_(list(stock_item_ids)))
                    .all()
                )
                stock_by_item = {int(iid): float(qty or 0.0) for iid, qty in stock_rows}
            except Exception as ex:
                logger.exception("Failed to build stock_by_item cache: %s", ex)
                stock_by_item = {int(i.item_id): float(i.stock_qty or 0.0) for i in item_cache.values()}

        try:
            missing_cnt = len([cid for cid in component_item_ids if cid not in stock_by_item])
            logger.debug(
                "stock_by_item cache built: net_items=%s, component_items=%s, total=%s, missing_components=%s",
                len(all_item_ids),
                len(component_item_ids),
                len(stock_by_item),
                missing_cnt,
            )
        except Exception:
            pass

        # Apply B) reservation map to stock cache (non-negative clamp).
        effective_stock_by_item: Dict[int, float] = dict(stock_by_item)
        for comp_id, reserved_qty in reserved_by_component.items():
            try:
                iid = int(comp_id)
                reserve_val = float(reserved_qty or 0.0)
            except Exception:
                continue
            if reserve_val <= 1e-12:
                continue
            base_stock = float(effective_stock_by_item.get(iid, 0.0) or 0.0)
            effective_stock_by_item[iid] = max(base_stock - reserve_val, 0.0)
        
        # This is a simplification; in a real scenario, WIP would be calculated from open production orders
        wip_by_item = defaultdict(float)

        total_demand_by_item = defaultdict(float)
        for item_id_str, buckets in net_requirements.items():
            total_demand_by_item[int(item_id_str)] += sum(float(val or 0.0) for val in buckets.values())

        # Units cache for discreteness policy
        units_all = db.query(Unit).all()
        units_by_ref = {getattr(u, "unit_ref1c"): u for u in units_all}

        order_qty_calculator = OrderQuantityCalculator(
            snapshot=run.config_snapshot,
            default_spec_map=default_spec_map,
            spec_by_id=spec_by_id,
            components_loader=components_loader,
            item_by_id=item_cache,
            units_by_ref=units_by_ref,
            res_by_id=res_by_id,
            production_kinds_by_resource=production_kinds_by_resource,
            stock_by_item=effective_stock_by_item,
            wip_by_item=wip_by_item,
            horizon_days=run.horizon_days,
            total_demand_by_item=total_demand_by_item,
        )
        priority_manager = PriorityManager(run.config_snapshot)
        capacity_scheduler = CapacityScheduler(db, run.config_snapshot)
        pegging_builder = PeggingBuilder()
        
        # Caches for stage building
        all_specs = db.query(Specification).join(DefaultSpecification).filter(DefaultSpecification.item_id.in_(all_item_ids)).all()
        spec_cache = {}
        for item_id, spec_id in default_spec_map.items():
            if item_id in all_item_ids:
                spec = next((s for s in all_specs if s.spec_id == spec_id), None)
                if spec:
                    spec_cache[item_id] = spec
        all_spec_ids = [s.spec_id for s in all_specs]
        
        all_spec_ops = db.query(SpecOperation).filter(SpecOperation.spec_id.in_(all_spec_ids)).all()
        spec_op_cache = defaultdict(list)
        for so in all_spec_ops: spec_op_cache[so.spec_id].append(so)
        
        all_kind_ids = {s.production_kind_id for s in all_specs if s.production_kind_id}
        all_kinds = db.query(ProductionKind).filter(ProductionKind.id.in_(all_kind_ids)).all()
        kind_cache = {k.id: k for k in all_kinds}
        
        resource_kind_cache: Dict[int, List[ResourceProductionKind]] = defaultdict(list)
        for rk in db.query(ResourceProductionKind).filter(ResourceProductionKind.production_kind_id.in_(all_kind_ids)).all():
            resource_kind_cache[rk.production_kind_id].append(rk)

        all_op_ids = {so.operation_id for so in all_spec_ops}
        all_ops = db.query(Operation).filter(Operation.operation_id.in_(all_op_ids)).all()
        op_cache = {o.operation_id: o for o in all_ops}
        
        all_warnings = []
        all_warnings.extend(net_req_result.get("warnings", []) or [])
        all_warnings.extend(reserve_warnings)

        # --- PHASE 1: Build Orders and Purchases ---
        order_result = build_planned_orders_and_purchases(
            db,
            run,
            net_requirements,
            order_qty_calculator,
            priority_manager,
            item_cache,
            units_by_ref,
            active_remaining_by_item=active_remaining_by_item,
            supplier_remaining_by_item_date=supplier_remaining_by_item_date,
        )
        all_warnings.extend(order_result["warnings"])
        db.flush()

        # --- PHASE 2: Build Order Stages ---
        stage_result = build_order_stages(
            db, run, spec_cache, kind_cache, resource_kind_cache, op_cache, spec_op_cache
        )
        all_warnings.extend(stage_result["warnings"])
        db.flush()

        # --- PHASE 3: Apply Capacity Constraints ---
        capacity_result = apply_capacity_constraints(db, run, capacity_scheduler)
        all_warnings.extend(capacity_result["warnings"])
        db.flush()

        # --- FINALIZATION ---
        all_orders = db.query(PlannedOrder).filter(PlannedOrder.run_id == run.run_id).all()
        pegging_links = pegging_builder.build(
            run_id=run.run_id,
            orders=all_orders,
            default_spec_map=default_spec_map,
            get_components_for_spec=components_loader,
        )
        db.add_all(pegging_links)
        
        # Enrich warnings with nomenclature (item/root) meta so UI can display names and articles
        try:
            # Build child -> parent map from one-level pegging links
            parent_by_child: Dict[int, int] = {}
            for l in pegging_links:
                try:
                    if getattr(l, "child_item_id", None) is not None and getattr(l, "parent_item_id", None) is not None:
                        parent_by_child[int(l.child_item_id)] = int(l.parent_item_id)
                except Exception:
                    continue

            def _ensure_item(iid: Optional[int]):
                if iid is None:
                    return None
                ii = int(iid)
                it = item_cache.get(ii)
                if it is None:
                    try:
                        it = db.query(Item).filter(Item.item_id == ii).first()
                        if it:
                            item_cache[ii] = it
                    except Exception:
                        it = None
                return it

            for w in all_warnings:
                try:
                    iid = w.get("item_id")
                    it = _ensure_item(iid)
                    if it is not None:
                        # Fill current item meta if absent
                        w.setdefault("item_code", getattr(it, "item_code", None))
                        w.setdefault("item_name", getattr(it, "item_name", None))
                        w.setdefault("item_article", getattr(it, "item_article", None))
                    # Resolve root (parent from pegging) or fallback to the same item
                    parent_iid = None
                    try:
                        parent_iid = parent_by_child.get(int(iid)) if iid is not None else None
                    except Exception:
                        parent_iid = None
                    if parent_iid is None and iid is not None:
                        parent_iid = int(iid)
                    p = _ensure_item(parent_iid)
                    if p is not None:
                        w.setdefault("root_item_id", int(getattr(p, "item_id", None) or parent_iid or 0))
                        w.setdefault("root_item_code", getattr(p, "item_code", None))
                        w.setdefault("root_item_name", getattr(p, "item_name", None))
                        w.setdefault("root_item_article", getattr(p, "item_article", None))
                except Exception:
                    # best-effort enrichment; skip invalid warning shapes
                    continue
        except Exception:
            # Do not fail a successful run because of enrichment issues
            pass

        run.status = "SUCCESS"
        run.warnings = all_warnings
        run.finished_at = datetime.now(timezone.utc)
        db.commit()

    except Exception as e:
        logger.exception(f"Planning run {run.run_id} failed.")
        run_id_failed = run.run_id
        # Discard any partial rows written before the failure so reports never
        # read a half-built FAILURE run as if it were valid.
        db.rollback()
        run = db.query(PlanningRun).filter(PlanningRun.run_id == run_id_failed).first()
        if run is not None:
            run.status = "FAILURE"
            run.finished_at = datetime.now(timezone.utc)
            run.warnings = (run.warnings or []) + [make_warning("PLANNING_RUN_FAILED", msg=f"Critical error during planning run: {e}", error=str(e))]
            db.commit()
        raise

    return run.run_id
