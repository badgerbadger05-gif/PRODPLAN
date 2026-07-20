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


from .constants import DEFAULT_PAGE_LIMIT, SERVER_MAX_LIMIT, logger
from .helpers import _bom_descendant_ids_for_roots, _ensure_dict, _late_supplier_order_badge, _load_item_category_meta, _load_late_supplier_order_coverage, _load_production_area_map, _load_purchase_area_map, _load_stage_area_context, _load_turning_blank_priority_map, _merge_badges, _to_date, _turning_blank_badge, _unit_display_from_parts

def get_run_production(
    db: Session,
    run_id: int,
    item_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> Dict[str, Any]:
    q = (
        db.query(
            PlannedOrder,
            Item.item_name,
            Item.item_article,
            Item.unit,
            Unit.short_name,
            Unit.unit_name,
            Unit.unit_code,
        )
        .outerjoin(Item, PlannedOrder.item_id == Item.item_id)
        .outerjoin(Unit, Item.unit == Unit.unit_ref1c)
        .filter(PlannedOrder.run_id == run_id)
    )
    if item_id is not None:
        q = q.filter(PlannedOrder.item_id == int(item_id))
    if root_item_id is not None:
        descendant_ids = _bom_descendant_ids_for_roots(db, [int(root_item_id)])
        q = q.filter(PlannedOrder.item_id.in_(descendant_ids or {int(root_item_id)}))
    # bucket_type removed from schema; all rows are daily

    rows_joined = q.all()
    date_from_dt = _to_date(date_from) if date_from else None
    date_to_dt = _to_date(date_to) if date_to else None

    filtered_rows = []
    for row in rows_joined:
        po, in_name, in_article, in_unit_guid, in_unit_short, in_unit_name, in_unit_code = row
        include_row = True
        start_dt = po.start_date.date() if isinstance(po.start_date, datetime) else po.start_date
        finish_dt = po.finish_date.date() if isinstance(po.finish_date, datetime) else po.finish_date

        if date_from_dt:
            if finish_dt is None or finish_dt < date_from_dt:
                include_row = False
        if include_row and date_to_dt:
            if start_dt is None or start_dt > date_to_dt:
                include_row = False
        if include_row:
            filtered_rows.append(row)

    aggregated_data: Dict[Tuple[int, str, str], Dict[str, Any]] = {}
    order_ids: List[int] = []
    turning_blank_priority = _load_turning_blank_priority_map(db, run_id)
    production_area_by_item = _load_production_area_map(
        db,
        [int(row[0].item_id) for row in filtered_rows if getattr(row[0], "item_id", None) is not None],
    )
    
    for row in filtered_rows:
        po, in_name, in_article, in_unit_guid, in_unit_short, in_unit_name, in_unit_code = row
        order_ids.append(int(po.order_id))
        
        start_iso = po.start_date.isoformat() if po.start_date else ""
        if not start_iso:
            # fallback to finish_date if start_date is missing
            fin_dt = po.finish_date.isoformat() if po.finish_date else ""
            start_iso = fin_dt
        unit_display = _unit_display_from_parts(in_unit_guid, in_unit_short, in_unit_name, in_unit_code)
        agg_key = (int(po.item_id), start_iso, unit_display)
        badge = _turning_blank_badge(turning_blank_priority, int(po.item_id), po.need_date)
        
        if agg_key not in aggregated_data:
            aggregated_data[agg_key] = {
                "item_id": int(po.item_id),
                "item_name": in_name,
                "item_article": in_article,
                "unit": unit_display,
                "qty": 0.0,
                "need_date": po.need_date.isoformat() if po.need_date else None,
                "start_date": po.start_date.isoformat() if po.start_date else (po.finish_date.isoformat() if po.finish_date else None),
                "finish_date": po.finish_date.isoformat() if po.finish_date else None,
                "route_ref": po.route_ref,
                "priority_index": float(po.priority_index or 0.0) if po.priority_index is not None else None,
                "bucket_type": "daily",
                "bucket_date": po.bucket_date.isoformat() if po.bucket_date else None,
                "demand_ref": po.demand_ref,
                "demand_date": po.demand_date.isoformat() if po.demand_date else None,
                "badge": badge,
                "turning_blank_priority": bool(badge),
                "stages": [],
                "norm_hours_total": 0.0,
                "norm_hours_per_unit": None,
                "source_order_ids": [],
                # flags for UI semantics
                "flags": {
                    "missingArea": False,
                    "missingNorm": False,
                    "componentBlocked": False,
                    "componentPartial": False,
                    "capacityShiftDays": 0,
                },
            }
        
        aggregated_data[agg_key]["qty"] += float(po.qty or 0.0)
        aggregated_data[agg_key].setdefault("source_order_ids", []).append(int(po.order_id))

    # Stages enrichment
    stages: List[PlannedOrderStage] = []
    if order_ids:
        stages = (
            db.query(PlannedOrderStage)
            .filter(
                PlannedOrderStage.run_id == run_id,
                PlannedOrderStage.order_id.in_(order_ids),
            )
            .all()
        )

    # Enrich stage data with area_name
    area_name_by_id: Dict[int, str] = {}
    try:
        area_ids: Set[int] = {int(s.area_id) for s in stages if getattr(s, "area_id", None) is not None}
        if area_ids:
            res_rows: List[ProductionResource] = (
                db.query(ProductionResource)
                .filter(ProductionResource.resource_id.in_(list(area_ids)))
                .all()
            )
            for r in res_rows:
                try:
                    area_name_by_id[int(r.resource_id)] = getattr(r, "resource_name", None) or ""
                except Exception:
                    continue
    except Exception:
        area_name_by_id = {}
    stage_name_by_id, fallback_area_by_stage, fallback_area_name_by_id = _load_stage_area_context(db)
    area_name_by_id.update(fallback_area_name_by_id)

    stage_by_order: Dict[int, List[Dict[str, Any]]] = {}
    for s in stages:
        sid = int(s.stage_id)
        aid = int(s.area_id) if s.area_id is not None else fallback_area_by_stage.get(sid)
        aname = area_name_by_id.get(aid, "") if aid is not None else stage_name_by_id.get(sid)
        hours_f = float(s.hours or 0.0)
        stage_by_order.setdefault(int(s.order_id), []).append(
            {
                "stage_id": sid,
                "stage_name": stage_name_by_id.get(sid),
                "area_id": aid,
                "area_name": aname,
                "bucket_type": "daily",
                "bucket_date": s.bucket_date.isoformat() if s.bucket_date else None,
                "hours": hours_f,
                # stage-level flag for missing norm
                "missingNorm": hours_f <= 1e-9,
            }
        )

    # Combine per-order info into aggregated rows and compute flags
    for row in filtered_rows:
        po, in_name, in_article, in_unit_guid, in_unit_short, in_unit_name, in_unit_code = row

        unit_display = _unit_display_from_parts(in_unit_guid, in_unit_short, in_unit_name, in_unit_code)
        start_iso = (
            po.start_date.isoformat()
            if po.start_date
            else (po.finish_date.isoformat() if po.finish_date else "")
        )
        agg_key = (int(po.item_id), start_iso, unit_display)

        order_stages = stage_by_order.get(int(po.order_id), [])
        badge = _turning_blank_badge(turning_blank_priority, int(po.item_id), po.need_date)
        if agg_key not in aggregated_data:
            aggregated_data[agg_key] = {
                "item_id": int(po.item_id),
                "item_name": in_name,
                "item_article": in_article,
                "unit": unit_display,
                "qty": float(po.qty or 0.0),
                "need_date": po.need_date.isoformat() if po.need_date else None,
                "start_date": start_iso or None,
                "finish_date": po.finish_date.isoformat() if po.finish_date else None,
                "route_ref": po.route_ref,
                "priority_index": float(po.priority_index or 0.0) if po.priority_index is not None else None,
                "bucket_type": "daily",
                "bucket_date": po.bucket_date.isoformat() if po.bucket_date else None,
                "demand_ref": po.demand_ref,
                "demand_date": po.demand_date.isoformat() if po.demand_date else None,
                "badge": badge,
                "turning_blank_priority": bool(badge),
                "stages": [],
                "norm_hours_total": 0.0,
                "norm_hours_per_unit": None,
                "source_order_ids": [int(po.order_id)],
                "flags": {
                    "missingArea": False,
                    "missingNorm": False,
                    "componentBlocked": False,
                    "componentPartial": False,
                    "capacityShiftDays": 0,
                },
            }

        # Append stages and sum hours
        aggregated_data[agg_key]["stages"].extend(order_stages)
        norm_total = float(sum(float(x.get("hours") or 0.0) for x in order_stages))
        aggregated_data[agg_key]["norm_hours_total"] += norm_total


        # Compute flags per order and OR-aggregate into aggregated_data flags
        flags = aggregated_data[agg_key]["flags"]
        # missing norm if no hours across stages for this order
        if norm_total <= 1e-9:
            flags["missingNorm"] = True
        # missing area if stages absent or no area selected
        if (not order_stages) or all(s.get("area_id") is None for s in order_stages):
            flags["missingArea"] = True
        # component shortage flags deduced from requested vs planned qty
        try:
            requested_qty = float(getattr(po, "requested_qty", 0.0) or 0.0)
            planned_qty = float(getattr(po, "qty", 0.0) or 0.0)
            if requested_qty > 0 and planned_qty <= 1e-9:
                flags["componentBlocked"] = True
            if requested_qty - planned_qty > 1e-9 and planned_qty > 1e-9:
                flags["componentPartial"] = True
        except Exception:
            pass
        # capacity shift: positive days if finish_date is later than need_date
        try:
            if po.finish_date and po.need_date:
                fin_d = po.finish_date.date() if isinstance(po.finish_date, datetime) else po.finish_date
                need_d = po.need_date.date() if isinstance(po.need_date, datetime) else po.need_date
                shift_days = (fin_d - need_d).days
                if shift_days > 0:
                    flags["capacityShiftDays"] = max(int(flags.get("capacityShiftDays", 0) or 0), int(shift_days))
        except Exception:
            pass

    # Build fallback NPU for items that still have 0 qty (rare) to show per-unit norm
    try:
        item_ids_page: List[int] = list({int(r[0].item_id) for r in filtered_rows})
    except Exception:
        item_ids_page = []
    fallback_npu: Dict[int, float] = {}
    if item_ids_page:
        try:
            defs = (
                db.query(DefaultSpecification)
                .filter(DefaultSpecification.item_id.in_(item_ids_page))
                .all()
            )
            item_to_spec: Dict[int, int] = {}
            spec_ids_set: Set[int] = set()
            for d in defs:
                try:
                    iid = int(d.item_id)
                    sid = int(d.spec_id)
                    item_to_spec[iid] = sid
                    spec_ids_set.add(sid)
                except Exception:
                    continue
            if spec_ids_set:
                rows = (
                    db.query(
                        SpecOperation.spec_id.label("spec_id"),
                        func.sum(func.coalesce(SpecOperation.time_norm, Operation.time_norm)).label("sum_norm"),
                    )
                    .join(Operation, SpecOperation.operation_id == Operation.operation_id)
                    .filter(SpecOperation.spec_id.in_(list(spec_ids_set)))
                    .group_by(SpecOperation.spec_id)
                    .all()
                )
                spec_norm_sum: Dict[int, float] = {int(r.spec_id): float(getattr(r, "sum_norm", 0.0) or 0.0) for r in rows}
                for iid, sid in item_to_spec.items():
                    try:
                        npu_val = float(spec_norm_sum.get(int(sid), 0.0) or 0.0)
                        if npu_val > 0.0:
                            fallback_npu[int(iid)] = npu_val
                    except Exception:
                        continue
        except Exception as ex:
            logger.exception("fallback_npu build failed: %s", ex)
            fallback_npu = {}

    # Finalize aggregated rows
    final_data: List[Dict[str, Any]] = []
    for key in sorted(aggregated_data.keys()):
        data = aggregated_data[key]
        qty_val = float(data.get("qty") or 0.0)
        if qty_val > 1e-12:
            data["qty"] = qty_val
            # set per-unit norm if we have total hours
            if float(data.get("norm_hours_total") or 0.0) > 1e-12:
                data["norm_hours_per_unit"] = float(data["norm_hours_total"] / qty_val)
        else:
            item_id = data["item_id"]
            npu_fb = fallback_npu.get(item_id, 0.0)
            if npu_fb > 0.0:
                data["norm_hours_per_unit"] = npu_fb

        if not data.get("start_date") and data.get("finish_date"):
            data["start_date"] = data["finish_date"]

        try:
            if data.get("finish_date") and data.get("need_date"):
                fin_d = date.fromisoformat(str(data["finish_date"])[:10])
                need_d = date.fromisoformat(str(data["need_date"])[:10])
                shift = (fin_d - need_d).days
                data["forecast_date"] = fin_d.isoformat()
                data["forecast_shift_days"] = shift
                data["forecast_reason"] = (
                    "смещение по мощностям"
                    if shift > 0
                    else ("раньше плановой даты" if shift < 0 else "в срок")
                )
        except Exception:
            data["forecast_date"] = data.get("finish_date")
            data["forecast_shift_days"] = None
            data["forecast_reason"] = None

        # stable synthetic order_id for UI tables (aggregated view)
        data["order_id"] = hash(f"{data['item_id']}_{data['start_date']}_{data['unit']}") % (10**10)

        stage_rows = list(data.get("stages") or [])
        if stage_rows:
            best_stage = max(stage_rows, key=lambda x: float(x.get("hours") or 0.0))
            data["main_area_id"] = best_stage.get("area_id")
            data["main_area_name"] = best_stage.get("area_name") or best_stage.get("stage_name") or None
            data["main_stage_id"] = best_stage.get("stage_id")
            data["main_stage_name"] = best_stage.get("stage_name")
        else:
            area_meta = production_area_by_item.get(int(data.get("item_id") or 0), {})
            data["main_area_id"] = area_meta.get("main_area_id")
            data["main_area_name"] = area_meta.get("main_area_name")
            data["main_stage_id"] = area_meta.get("main_stage_id")
            data["main_stage_name"] = area_meta.get("main_stage_name")
        
        final_data.append(data)

    # Filter out technical rows with qty <= 0 (backend safeguard)
    final_data = [r for r in final_data if float(r.get("qty") or 0.0) > 1e-12]

    def _safe_date_key(val: Optional[str]) -> Tuple[int, str]:
        if not val:
            return (0, "")
        return (1, val)

    sort_map = {
        "item_name": lambda x: (x.get("item_name") or "").lower(),
        "item_article": lambda x: (x.get("item_article") or "").lower(),
        "qty": lambda x: float(x.get("qty") or 0.0),
        "need_date": lambda x: _safe_date_key(x.get("need_date")),
        "start_date": lambda x: _safe_date_key(x.get("start_date")),
        "priority_index": lambda x: float(x.get("priority_index") or 0.0),
    }
    
    sb = (sort_by or "start_date").strip().lower()
    sd = (sort_dir or "asc").strip().lower()
    key_fn = sort_map.get(sb, sort_map["start_date"])

    try:
        final_data.sort(key=key_fn, reverse=(sd == "desc"))
    except TypeError:
        def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
            normalized = dict(row)
            normalized["item_id"] = int(row.get("item_id") or 0)
            normalized["item_name"] = row.get("item_name") or ""
            normalized["item_article"] = row.get("item_article") or ""
            normalized["qty"] = float(row.get("qty") or 0.0)
            normalized["need_date"] = row.get("need_date") or ""
            normalized["start_date"] = row.get("start_date") or (row.get("finish_date") or "")
            normalized["priority_index"] = float(row.get("priority_index") or 0.0)
            normalized["bucket_type"] = row.get("bucket_type") or ""
            normalized["bucket_date"] = row.get("bucket_date") or ""
            normalized["route_ref"] = row.get("route_ref") or ""
            normalized["demand_ref"] = row.get("demand_ref") or ""
            normalized["demand_date"] = row.get("demand_date") or ""
            normalized["unit"] = row.get("unit") or ""
            normalized["order_id"] = int(row.get("order_id") or 0)
            normalized["norm_hours_total"] = float(row.get("norm_hours_total") or 0.0)
            normalized["norm_hours_per_unit"] = float(row.get("norm_hours_per_unit") or 0.0)
            normalized["stages"] = row.get("stages") or []
            # flags and main area are already safe
            return normalized

        normalized_rows = [normalize_row(r) for r in final_data]

        norm_sort_map = {
            "item_name": lambda x: x["item_name"].lower(),
            "item_article": lambda x: x["item_article"].lower(),
            "qty": lambda x: x["qty"],
            "need_date": lambda x: (1, x["need_date"]) if x["need_date"] else (0, ""),
            "start_date": lambda x: (1, x["start_date"]) if x["start_date"] else (0, ""),
            "priority_index": lambda x: x["priority_index"],
            "bucket_date": lambda x: (1, x["bucket_date"]) if x["bucket_date"] else (0, ""),
        }
        fallback_key_fn = norm_sort_map.get(sb, norm_sort_map["start_date"])
        normalized_rows.sort(key=fallback_key_fn, reverse=(sd == "desc"))
        final_data = normalized_rows
    
    total = len(final_data)
    total_qty_val = float(sum(item.get("qty", 0.0) for item in final_data))

    req_limit = int(limit or DEFAULT_PAGE_LIMIT)
    if req_limit > SERVER_MAX_LIMIT:
        logger.debug(
            "get_run_production limit clamped: requested=%s, max=%s",
            req_limit,
            SERVER_MAX_LIMIT,
        )
    effective_limit = max(1, min(req_limit, SERVER_MAX_LIMIT))
    effective_offset = max(0, int(offset or 0))

    start_idx = effective_offset
    end_idx = start_idx + effective_limit
    paginated_data = final_data[start_idx:end_idx]

    return {
        "rows": paginated_data,
        "total": int(total),
        "total_qty": float(total_qty_val),
        "limit": int(effective_limit),
        "offset": int(effective_offset),
    }


def get_run_purchases(
    db: Session,
    run_id: int,
    item_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> Dict[str, Any]:
    base_query = (
        db.query(
            PlannedPurchase.purchase_id,
            PlannedPurchase.item_id,
            PlannedPurchase.qty,
            PlannedPurchase.need_date,
            PlannedPurchase.order_date,
            PlannedPurchase.lead_time_days,
            PlannedPurchase.priority_index,
            PlannedPurchase.bucket_date,
            PlannedPurchase.supplier_ref1c,
            PlannedPurchase.requested_qty,
        )
        .filter(PlannedPurchase.run_id == run_id)
    )

    item_rows: Dict[int, Tuple[Optional[str], Optional[str], Optional[str]]] = {}
    unit_rows: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]] = {}

    def ensure_meta_cached(item_ids: List[int]) -> None:
        """
        Robustly populate meta caches for items and units.
        Tolerates test doubles that return unexpected row shapes.
        """
        missing_item_ids = [iid for iid in item_ids if iid not in item_rows]
        if not missing_item_ids:
            return
        try:
            rows = (
                db.query(Item.item_id, Item.item_name, Item.item_article, Item.unit)
                .filter(Item.item_id.in_(missing_item_ids))
                .all()
            )
        except Exception:
            rows = []
        unit_keys_to_fetch: Set[str] = set()
        for row in rows or []:
            try:
                if isinstance(row, (list, tuple)):
                    if len(row) >= 4:
                        iid, name, article, unit_ref = row[0], row[1], row[2], row[3]
                    else:
                        continue
                else:
                    iid = getattr(row, "item_id", None)
                    name = getattr(row, "item_name", None)
                    article = getattr(row, "item_article", None)
                    unit_ref = getattr(row, "unit", None)
                    if iid is None:
                        continue
                item_rows[int(iid)] = (name, article, unit_ref)
                if unit_ref:
                    unit_keys_to_fetch.add(unit_ref)
            except Exception:
                # Skip malformed rows silently (e.g., from FakeQuery in tests)
                continue
        if not unit_keys_to_fetch:
            return
        missing_unit_keys = [key for key in unit_keys_to_fetch if key not in unit_rows]
        if not missing_unit_keys:
            return
        try:
            units = (
                db.query(Unit.unit_ref1c, Unit.short_name, Unit.unit_name, Unit.unit_code)
                .filter(Unit.unit_ref1c.in_(missing_unit_keys))
                .all()
            )
        except Exception:
            units = []
        for urow in units or []:
            try:
                if isinstance(urow, (list, tuple)):
                    if len(urow) >= 4:
                        guid, short_name, unit_name, unit_code = urow[0], urow[1], urow[2], urow[3]
                    else:
                        continue
                else:
                    guid = getattr(urow, "unit_ref1c", None)
                    short_name = getattr(urow, "short_name", None)
                    unit_name = getattr(urow, "unit_name", None)
                    unit_code = getattr(urow, "unit_code", None)
                    if guid is None:
                        continue
                unit_rows[guid] = (short_name, unit_name, unit_code)
            except Exception:
                continue

    q = (
        base_query.outerjoin(Item, PlannedPurchase.item_id == Item.item_id)
        .outerjoin(Unit, Item.unit == Unit.unit_ref1c)
        .add_columns(
            Item.item_name,
            Item.item_article,
            Item.unit,
            Unit.short_name,
            Unit.unit_name,
            Unit.unit_code,
        )
    )
    if item_id is not None:
        q = q.filter(PlannedPurchase.item_id == int(item_id))
    if root_item_id is not None:
        descendant_ids = _bom_descendant_ids_for_roots(db, [int(root_item_id)])
        q = q.filter(PlannedPurchase.item_id.in_(descendant_ids or {int(root_item_id)}))
    # bucket_type removed from schema; all rows are daily

    rows_joined = q.all()
    filtered_rows = []
    item_ids_to_cache: List[int] = []
    for _row in rows_joined:
        try:
            if isinstance(_row, (tuple, list)) and len(_row) >= 2:
                item_ids_to_cache.append(int(_row[1]))
            else:
                iid_attr = getattr(_row, "item_id", None)
                if iid_attr is not None:
                    item_ids_to_cache.append(int(iid_attr))
        except Exception:
            continue
    ensure_meta_cached(item_ids_to_cache)
    for row in rows_joined:
        # Support both legacy tuples (with bucket_type) and new tuples (without)
        seq = list(row) if isinstance(row, (tuple, list)) else [row]
        requested_qty_val = None
        if len(seq) >= 17:
            # Ultra-legacy: bucket_type + requested_qty both present
            (
                purchase_id,
                item_id_val,
                qty_val,
                need_date_val,
                order_date_val,
                lead_time_days_val,
                priority_index_val,
                bucket_type_val,
                bucket_date_val,
                supplier_ref1c_val,
                requested_qty_val,
                in_name,
                in_article,
                in_unit_guid,
                in_unit_short,
                in_unit_name,
                in_unit_code,
            ) = seq[:17]
        elif len(seq) >= 16:
            # Current schema: requested_qty at position 9, no bucket_type
            (
                purchase_id,
                item_id_val,
                qty_val,
                need_date_val,
                order_date_val,
                lead_time_days_val,
                priority_index_val,
                bucket_date_val,
                supplier_ref1c_val,
                requested_qty_val,
                in_name,
                in_article,
                in_unit_guid,
                in_unit_short,
                in_unit_name,
                in_unit_code,
            ) = seq[:16]
            bucket_type_val = "daily"
        elif len(seq) >= 15:
            # Old schema without requested_qty
            (
                purchase_id,
                item_id_val,
                qty_val,
                need_date_val,
                order_date_val,
                lead_time_days_val,
                priority_index_val,
                bucket_date_val,
                supplier_ref1c_val,
                in_name,
                in_article,
                in_unit_guid,
                in_unit_short,
                in_unit_name,
                in_unit_code,
            ) = seq[:15]
            bucket_type_val = "daily"
        else:
            # Fallback to attribute extraction when row shape is unexpected (e.g., RowMapping)
            purchase_id = getattr(row, "purchase_id", None)
            item_id_val = getattr(row, "item_id", None)
            qty_val = getattr(row, "qty", None)
            need_date_val = getattr(row, "need_date", None)
            order_date_val = getattr(row, "order_date", None)
            lead_time_days_val = getattr(row, "lead_time_days", None)
            priority_index_val = getattr(row, "priority_index", None)
            bucket_date_val = getattr(row, "bucket_date", None)
            supplier_ref1c_val = getattr(row, "supplier_ref1c", None)
            requested_qty_val = getattr(row, "requested_qty", None)
            in_name = getattr(row, "item_name", None)
            in_article = getattr(row, "item_article", None)
            in_unit_guid = getattr(row, "unit", None)
            in_unit_short = getattr(row, "short_name", None)
            in_unit_name = getattr(row, "unit_name", None)
            in_unit_code = getattr(row, "unit_code", None)
            bucket_type_val = "daily"
        include_row = True
        if date_from:
            if bucket_date_val is None or bucket_date_val < _to_date(date_from):
                include_row = False
        if date_to:
            if bucket_date_val is None or bucket_date_val > _to_date(date_to):
                include_row = False
        if include_row:
            filtered_rows.append(
                (
                    purchase_id,
                    item_id_val,
                    qty_val,
                    need_date_val,
                    order_date_val,
                    lead_time_days_val,
                    priority_index_val,
                    bucket_date_val,
                    supplier_ref1c_val,
                    requested_qty_val,
                    in_name,
                    in_article,
                    in_unit_guid,
                    in_unit_short,
                    in_unit_name,
                    in_unit_code,
                )
            )
 
    aggregated_data: Dict[Tuple[int, str], Dict[str, Any]] = {}
    turning_blank_priority = _load_turning_blank_priority_map(db, run_id)
    late_supplier_rows = _load_late_supplier_order_coverage(
        db,
        [int(row[1]) for row in filtered_rows if row[1] is not None],
    )
    purchase_area_by_item = _load_purchase_area_map(
        db,
        [int(row[1]) for row in filtered_rows if row[1] is not None],
    )
    category_by_item = _load_item_category_meta(
        db,
        [int(row[1]) for row in filtered_rows if row[1] is not None],
    )
    supplier_refs = sorted({
        str(row[8]).strip()
        for row in filtered_rows
        if row[8] is not None and str(row[8]).strip()
    })
    supplier_name_by_ref: Dict[str, str] = {}
    if supplier_refs:
        try:
            supplier_rows = (
                db.query(Supplier.supplier_ref1c, Supplier.supplier_name)
                .filter(Supplier.supplier_ref1c.in_(supplier_refs))
                .all()
            )
            supplier_name_by_ref = {
                str(ref): str(name or "")
                for ref, name in supplier_rows
                if ref
            }
        except Exception:
            supplier_name_by_ref = {}
    
    for row in filtered_rows:
        (
            purchase_id,
            item_id_val,
            qty_val,
            need_date_val,
            order_date_val,
            lead_time_days_val,
            priority_index_val,
            bucket_date_val,
            supplier_ref1c_val,
            requested_qty_val,
            in_name,
            in_article,
            in_unit_guid,
            in_unit_short,
            in_unit_name,
            in_unit_code,
        ) = row

        if item_id_val not in item_rows:
            ensure_meta_cached([int(item_id_val)])
        if not in_name or not in_article or not in_unit_guid:
            cached = item_rows.get(int(item_id_val))
            if cached:
                fallback_name, fallback_article, fallback_unit_guid = cached
                in_name = in_name or fallback_name
                in_article = in_article or fallback_article
                in_unit_guid = in_unit_guid or fallback_unit_guid
        if (not in_unit_short and not in_unit_name and not in_unit_code) and in_unit_guid:
            cached_unit = unit_rows.get(in_unit_guid)
            if cached_unit:
                cu_short, cu_name, cu_code = cached_unit
                in_unit_short = in_unit_short or cu_short
                in_unit_name = in_unit_name or cu_name
                in_unit_code = in_unit_code or cu_code
        
        unit_display = _unit_display_from_parts(in_unit_guid, in_unit_short, in_unit_name, in_unit_code)
        item_id_int = int(item_id_val)
        agg_key = (item_id_int, unit_display)
        turning_badge = _turning_blank_badge(turning_blank_priority, int(item_id_val), need_date_val)
        late_supplier_badge = _late_supplier_order_badge(
            late_supplier_rows,
            item_id_int,
            need_date_val,
            qty_val,
        )
        badge = _merge_badges(turning_badge, late_supplier_badge)
        area_meta = purchase_area_by_item.get(item_id_int, {})
        category_meta = category_by_item.get(item_id_int, {})
        supplier_ref_clean = str(supplier_ref1c_val or "").strip()
        
        if agg_key not in aggregated_data:
            aggregated_data[agg_key] = {
                "item_id": item_id_int,
                "item_name": in_name,
                "item_article": in_article,
                "unit": unit_display,
                "qty": 0.0,
                "requested_qty": 0.0,
                "need_date": need_date_val.isoformat() if need_date_val else None,
                "order_date": order_date_val.isoformat() if order_date_val else None,
                "lead_time_days": int(lead_time_days_val or 0),
                "priority_index": float(priority_index_val or 0.0) if priority_index_val is not None else None,
                "bucket_type": "daily",
                "bucket_date": bucket_date_val.isoformat() if bucket_date_val else None,
                "supplier_ref1c": supplier_ref_clean or None,
                "supplier_name": supplier_name_by_ref.get(supplier_ref_clean) or "",
                "category_id": category_meta.get("group_id"),
                "category_name": category_meta.get("group_name") or "Без товарной группы",
                "category_ref1c": category_meta.get("group_ref1c"),
                "badge": badge,
                "turning_blank_priority": bool(turning_badge),
                "late_supplier_order": bool(late_supplier_badge),
                "source_purchase_ids": [],
                "main_area_id": area_meta.get("main_area_id"),
                "main_area_name": area_meta.get("main_area_name"),
                "main_stage_id": area_meta.get("main_stage_id"),
                "main_stage_name": area_meta.get("main_stage_name"),
            }
        elif badge:
            aggregated_data[agg_key]["badge"] = _merge_badges(aggregated_data[agg_key].get("badge"), badge)
            aggregated_data[agg_key]["turning_blank_priority"] = bool(
                aggregated_data[agg_key].get("turning_blank_priority") or turning_badge
            )
            aggregated_data[agg_key]["late_supplier_order"] = bool(
                aggregated_data[agg_key].get("late_supplier_order") or late_supplier_badge
            )
        
        aggregated_data[agg_key]["qty"] += float(qty_val or 0.0)
        aggregated_data[agg_key]["requested_qty"] += float(requested_qty_val or 0.0)
        if purchase_id is not None:
            aggregated_data[agg_key].setdefault("source_purchase_ids", []).append(int(purchase_id))

    data: List[Dict[str, Any]] = []
    for key in sorted(aggregated_data.keys()):
        values = aggregated_data[key]
        values["purchase_id"] = hash(
            f"{values['item_id']}_{values['unit']}_{values['need_date'] or ''}_{values['bucket_type']}_{values['bucket_date']}"
        ) % (10**10)
        # supplier_covered_qty = gross need minus net planned purchase (rounded to avoid float noise)
        req = float(values.get("requested_qty") or 0.0)
        net = float(values.get("qty") or 0.0)
        values["supplier_covered_qty"] = round(max(0.0, req - net), 6)
        data.append(values)

    sort_map = {
        "item_name": lambda x: x.get("item_name", ""),
        "item_article": lambda x: x.get("item_article", ""),
        "qty": lambda x: x.get("qty", 0.0),
        "need_date": lambda x: x.get("need_date", ""),
        "order_date": lambda x: x.get("order_date", ""),
        "bucket_date": lambda x: x.get("bucket_date", ""),
        "priority_index": lambda x: x.get("priority_index", 0.0),
    }
    
    sb = (sort_by or "bucket_date").strip().lower()
    sd = (sort_dir or "asc").strip().lower()
    key_fn = sort_map.get(sb, lambda x: x.get("bucket_date", ""))

    try:
        data.sort(key=key_fn, reverse=(sd == "desc"))
    except TypeError:
        def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
            normalized = dict(row)
            normalized["item_id"] = int(row.get("item_id") or 0)
            normalized["item_name"] = row.get("item_name") or ""
            normalized["item_article"] = row.get("item_article") or ""
            normalized["qty"] = float(row.get("qty") or 0.0)
            normalized["need_date"] = row.get("need_date") or ""
            normalized["order_date"] = row.get("order_date") or ""
            normalized["bucket_type"] = row.get("bucket_type") or ""
            normalized["bucket_date"] = row.get("bucket_date") or ""
            normalized["priority_index"] = float(row.get("priority_index") or 0.0)
            normalized["unit"] = row.get("unit") or ""
            normalized["lead_time_days"] = int(row.get("lead_time_days") or 0)
            normalized["supplier_ref1c"] = row.get("supplier_ref1c") or ""
            normalized["supplier_name"] = row.get("supplier_name") or ""
            normalized["category_id"] = row.get("category_id")
            normalized["category_name"] = row.get("category_name") or ""
            normalized["category_ref1c"] = row.get("category_ref1c") or ""
            normalized["purchase_id"] = int(row.get("purchase_id") or 0)
            normalized["main_area_id"] = row.get("main_area_id")
            normalized["main_area_name"] = row.get("main_area_name")
            normalized["main_stage_id"] = row.get("main_stage_id")
            normalized["main_stage_name"] = row.get("main_stage_name")
            return normalized

        normalized_rows = [normalize_row(r) for r in data]

        fallback_sort_map = {
            "item_name": lambda x: x["item_name"].lower(),
            "item_article": lambda x: x["item_article"].lower(),
            "qty": lambda x: x["qty"],
            "need_date": lambda x: (1, x["need_date"]) if x["need_date"] else (0, ""),
            "order_date": lambda x: (1, x["order_date"]) if x["order_date"] else (0, ""),
            "bucket_date": lambda x: (1, x["bucket_date"]) if x["bucket_date"] else (0, ""),
            "priority_index": lambda x: x["priority_index"],
        }
        fallback_key_fn = fallback_sort_map.get(sb, fallback_sort_map["bucket_date"])
        normalized_rows.sort(key=fallback_key_fn, reverse=(sd == "desc"))
        data = normalized_rows

    total = len(data)
    total_qty_val = float(sum(item.get("qty", 0.0) for item in data))

    req_limit = int(limit or DEFAULT_PAGE_LIMIT)
    if req_limit > SERVER_MAX_LIMIT:
        logger.debug(
            "get_run_purchases limit clamped: requested=%s, max=%s",
            req_limit,
            SERVER_MAX_LIMIT,
        )
    effective_limit = max(1, min(req_limit, SERVER_MAX_LIMIT))
    effective_offset = max(0, int(offset or 0))

    start_idx = effective_offset
    end_idx = start_idx + effective_limit
    paginated_data = data[start_idx:end_idx]

    return {
        "rows": paginated_data,
        "total": int(total),
        "total_qty": float(total_qty_val),
        "limit": int(effective_limit),
        "offset": int(effective_offset),
    }


def get_run_purchases_grouped_by_category(
    db: Session,
    run_id: int,
    item_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> Dict[str, Any]:
    purchases = get_run_purchases(
        db=db,
        run_id=run_id,
        item_id=item_id,
        root_item_id=root_item_id,
        bucket_type=None,
        date_from=date_from,
        date_to=date_to,
        limit=100000,
        offset=0,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    rows = list((purchases or {}).get("rows", []) or [])
    category_by_item = _load_item_category_meta(db, [int(row.get("item_id")) for row in rows if row.get("item_id") is not None])

    groups_map: Dict[Optional[int], Dict[str, Any]] = {}
    for row in rows:
        item_id_val = int(row.get("item_id") or 0)
        category_meta = category_by_item.get(item_id_val, {"group_id": None, "group_name": "Без товарной группы"})
        group_id = category_meta.get("group_id")
        group_name = category_meta.get("group_name") or "Без товарной группы"

        if group_id not in groups_map:
            groups_map[group_id] = {
                "group_id": group_id,
                "group_name": group_name,
                "orders": [],
                "sum_qty": 0.0,
            }

        order_entry = dict(row)
        groups_map[group_id]["orders"].append(order_entry)
        groups_map[group_id]["sum_qty"] += float(row.get("qty") or 0.0)

    groups_list = list(groups_map.values())
    groups_list.sort(key=lambda g: ((g.get("group_name") or "").lower(), 1 if g.get("group_id") is None else 0))

    total_groups = len(groups_list)
    total_orders = sum(len(group.get("orders", []) or []) for group in groups_list)
    effective_limit = max(1, min(int(limit or DEFAULT_PAGE_LIMIT), SERVER_MAX_LIMIT))
    effective_offset = max(0, int(offset or 0))
    groups_page = groups_list[effective_offset: effective_offset + effective_limit]

    return {
        "groups": groups_page,
        "total_groups": int(total_groups),
        "total_orders": int(total_orders),
        "limit": int(effective_limit),
        "offset": int(effective_offset),
    }


def _query_run_rework_rows(
    db: Session,
    run_id: int,
    item_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    q = (
        db.query(
            PlannedRework,
            Item.item_name,
            Item.item_article,
            Item.unit,
            Unit.short_name,
            Unit.unit_name,
            Unit.unit_code,
            Specification.spec_code,
            Specification.spec_name,
        )
        .outerjoin(Item, PlannedRework.item_id == Item.item_id)
        .outerjoin(Unit, Item.unit == Unit.unit_ref1c)
        .outerjoin(Specification, PlannedRework.spec_id == Specification.spec_id)
        .filter(PlannedRework.run_id == run_id)
    )
    if item_id is not None:
        q = q.filter(PlannedRework.item_id == int(item_id))
    if root_item_id is not None:
        descendant_ids = _bom_descendant_ids_for_roots(db, [int(root_item_id)])
        q = q.filter(PlannedRework.item_id.in_(descendant_ids or {int(root_item_id)}))

    rows_joined = q.all()
    date_from_dt = _to_date(date_from) if date_from else None
    date_to_dt = _to_date(date_to) if date_to else None

    data: List[Dict[str, Any]] = []
    for row in rows_joined:
        (
            rework,
            item_name,
            item_article,
            unit_guid,
            unit_short,
            unit_name,
            unit_code,
            spec_code,
            spec_name,
        ) = row

        bucket_dt = rework.bucket_date
        if date_from_dt and (bucket_dt is None or bucket_dt < date_from_dt):
            continue
        if date_to_dt and (bucket_dt is None or bucket_dt > date_to_dt):
            continue

        unit_display = _unit_display_from_parts(unit_guid, unit_short, unit_name, unit_code)
        shortage_payload = _ensure_dict(getattr(rework, "shortage", None)) or None

        data.append(
            {
                "rework_id": int(rework.rework_id),
                "item_id": int(rework.item_id),
                "item_name": item_name,
                "item_article": item_article,
                "unit": unit_display,
                "requested_qty": float(rework.requested_qty or 0.0),
                "planned_qty": float(rework.planned_qty or 0.0),
                "qty": float(rework.qty or 0.0),
                "need_date": rework.need_date.isoformat() if rework.need_date else None,
                "order_date": rework.order_date.isoformat() if rework.order_date else None,
                "lead_time_days": int(rework.lead_time_days or 0),
                "priority_index": float(rework.priority_index or 0.0) if rework.priority_index is not None else None,
                "bucket_type": "daily",
                "bucket_date": rework.bucket_date.isoformat() if rework.bucket_date else None,
                "spec_id": int(rework.spec_id) if rework.spec_id is not None else None,
                "spec_code": spec_code,
                "spec_name": spec_name,
                "component_limit": float(rework.component_limit or 0.0) if rework.component_limit is not None else None,
                "component_blocked": bool(getattr(rework, "component_blocked", False)),
                "component_partial": bool(getattr(rework, "component_partial", False)),
                "shortage": shortage_payload,
            }
        )

    return data


def get_run_rework(
    db: Session,
    run_id: int,
    item_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> Dict[str, Any]:
    data = _query_run_rework_rows(
        db=db,
        run_id=run_id,
        item_id=item_id,
        root_item_id=root_item_id,
        date_from=date_from,
        date_to=date_to,
    )

    sort_map = {
        "item_name": lambda x: (x.get("item_name") or "").lower(),
        "item_article": lambda x: (x.get("item_article") or "").lower(),
        "qty": lambda x: float(x.get("qty") or 0.0),
        "requested_qty": lambda x: float(x.get("requested_qty") or 0.0),
        "planned_qty": lambda x: float(x.get("planned_qty") or 0.0),
        "need_date": lambda x: x.get("need_date") or "",
        "order_date": lambda x: x.get("order_date") or "",
        "bucket_date": lambda x: x.get("bucket_date") or "",
        "priority_index": lambda x: float(x.get("priority_index") or 0.0),
        "spec_name": lambda x: (x.get("spec_name") or "").lower(),
    }

    sb = (sort_by or "bucket_date").strip().lower()
    sd = (sort_dir or "asc").strip().lower()
    key_fn = sort_map.get(sb, sort_map["bucket_date"])

    try:
        data.sort(key=key_fn, reverse=(sd == "desc"))
    except TypeError:
        data = [dict(row) for row in data]
        data.sort(key=lambda x: str(key_fn(x)), reverse=(sd == "desc"))

    total = len(data)
    total_qty_val = float(sum(float(item.get("qty") or 0.0) for item in data))

    req_limit = int(limit or DEFAULT_PAGE_LIMIT)
    if req_limit > SERVER_MAX_LIMIT:
        logger.debug(
            "get_run_rework limit clamped: requested=%s, max=%s",
            req_limit,
            SERVER_MAX_LIMIT,
        )
    effective_limit = max(1, min(req_limit, SERVER_MAX_LIMIT))
    effective_offset = max(0, int(offset or 0))

    start_idx = effective_offset
    end_idx = start_idx + effective_limit
    paginated_data = data[start_idx:end_idx]

    return {
        "rows": paginated_data,
        "total": int(total),
        "total_qty": float(total_qty_val),
        "limit": int(effective_limit),
        "offset": int(effective_offset),
    }


def get_run_rework_grouped(
    db: Session,
    run_id: int,
    item_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> Dict[str, Any]:
    rows = _query_run_rework_rows(
        db=db,
        run_id=run_id,
        item_id=item_id,
        root_item_id=root_item_id,
        date_from=date_from,
        date_to=date_to,
    )

    row_sort = {
        "item_name": lambda x: (x.get("item_name") or "").lower(),
        "item_article": lambda x: (x.get("item_article") or "").lower(),
        "qty": lambda x: float(x.get("qty") or 0.0),
        "need_date": lambda x: x.get("need_date") or "",
        "order_date": lambda x: x.get("order_date") or "",
        "bucket_date": lambda x: x.get("bucket_date") or "",
    }
    row_key_fn = row_sort.get((sort_by or "need_date").strip().lower(), row_sort["need_date"])
    rows.sort(key=row_key_fn, reverse=((sort_dir or "asc").strip().lower() == "desc"))

    # Текущая модель items ещё не хранит явную связь строки результата с товарной группой,
    # поэтому до следующей итерации backend выдаёт единый fallback-блок "Без товарной группы".
    groups: List[Dict[str, Any]] = []
    if rows:
        fallback_group = {
            "group_id": None,
            "group_name": "Без товарной группы",
            "orders": rows,
            "sum_qty": float(sum(float(row.get("qty") or 0.0) for row in rows)),
            "sum_requested_qty": float(sum(float(row.get("requested_qty") or 0.0) for row in rows)),
            "sum_planned_qty": float(sum(float(row.get("planned_qty") or 0.0) for row in rows)),
            "blocked_orders": int(sum(1 for row in rows if bool(row.get("component_blocked")))),
            "partial_orders": int(sum(1 for row in rows if bool(row.get("component_partial")))),
        }
        groups.append(fallback_group)

    total_groups = len(groups)
    total_orders = sum(len(group.get("orders", []) or []) for group in groups)

    req_limit = int(limit or DEFAULT_PAGE_LIMIT)
    effective_limit = max(1, min(req_limit, SERVER_MAX_LIMIT))
    effective_offset = max(0, int(offset or 0))
    start_idx = effective_offset
    end_idx = start_idx + effective_limit
    groups_page = groups[start_idx:end_idx]

    return {
        "groups": groups_page,
        "total_groups": int(total_groups),
        "total_orders": int(total_orders),
        "limit": int(effective_limit),
        "offset": int(effective_offset),
    }


def get_run_rework_grouped_by_category(
    db: Session,
    run_id: int,
    item_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> Dict[str, Any]:
    rows = _query_run_rework_rows(
        db=db,
        run_id=run_id,
        item_id=item_id,
        root_item_id=root_item_id,
        date_from=date_from,
        date_to=date_to,
    )

    row_sort = {
        "item_name": lambda x: (x.get("item_name") or "").lower(),
        "item_article": lambda x: (x.get("item_article") or "").lower(),
        "qty": lambda x: float(x.get("qty") or 0.0),
        "need_date": lambda x: x.get("need_date") or "",
        "order_date": lambda x: x.get("order_date") or "",
        "bucket_date": lambda x: x.get("bucket_date") or "",
    }
    row_key_fn = row_sort.get((sort_by or "need_date").strip().lower(), row_sort["need_date"])
    rows.sort(key=row_key_fn, reverse=((sort_dir or "asc").strip().lower() == "desc"))

    category_by_item = _load_item_category_meta(db, [int(row.get("item_id")) for row in rows if row.get("item_id") is not None])
    groups_map: Dict[Optional[int], Dict[str, Any]] = {}

    for row in rows:
        item_id_val = int(row.get("item_id") or 0)
        category_meta = category_by_item.get(item_id_val, {"group_id": None, "group_name": "Без товарной группы"})
        group_id = category_meta.get("group_id")
        group_name = category_meta.get("group_name") or "Без товарной группы"

        if group_id not in groups_map:
            groups_map[group_id] = {
                "group_id": group_id,
                "group_name": group_name,
                "orders": [],
                "sum_qty": 0.0,
                "sum_requested_qty": 0.0,
                "sum_planned_qty": 0.0,
                "blocked_orders": 0,
                "partial_orders": 0,
            }

        groups_map[group_id]["orders"].append(dict(row))
        groups_map[group_id]["sum_qty"] += float(row.get("qty") or 0.0)
        groups_map[group_id]["sum_requested_qty"] += float(row.get("requested_qty") or 0.0)
        groups_map[group_id]["sum_planned_qty"] += float(row.get("planned_qty") or 0.0)
        groups_map[group_id]["blocked_orders"] += int(bool(row.get("component_blocked")))
        groups_map[group_id]["partial_orders"] += int(bool(row.get("component_partial")))

    groups_list = list(groups_map.values())
    groups_list.sort(key=lambda g: ((g.get("group_name") or "").lower(), 1 if g.get("group_id") is None else 0))

    total_groups = len(groups_list)
    total_orders = sum(len(group.get("orders", []) or []) for group in groups_list)
    effective_limit = max(1, min(int(limit or DEFAULT_PAGE_LIMIT), SERVER_MAX_LIMIT))
    effective_offset = max(0, int(offset or 0))
    groups_page = groups_list[effective_offset: effective_offset + effective_limit]

    return {
        "groups": groups_page,
        "total_groups": int(total_groups),
        "total_orders": int(total_orders),
        "limit": int(effective_limit),
        "offset": int(effective_offset),
    }


def get_run_production_grouped(
    db: Session,
    run_id: int,
    item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    area_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Сгруппированная по участкам выдача производственных заказов для прогона.
    Группы формируются по основному участку заказа (stage с максимальными hours).
    - area_id=None => группа «Без участка»
    - Агрегаты мощностей подтягиваются из capacity_load в заданном диапазоне дат.
    """
    # 1) Базовый запрос по заказам с денормализацией item/unit
    q = (
        db.query(
            PlannedOrder,
            Item.item_name,
            Item.item_article,
            Item.unit,
            Unit.short_name,
            Unit.unit_name,
            Unit.unit_code,
        )
        .outerjoin(Item, PlannedOrder.item_id == Item.item_id)
        .outerjoin(Unit, Item.unit == Unit.unit_ref1c)
        .filter(PlannedOrder.run_id == run_id)
    )
    if item_id is not None:
        q = q.filter(PlannedOrder.item_id == int(item_id))

    rows_joined = q.all()
    date_from_dt = _to_date(date_from) if date_from else None
    date_to_dt = _to_date(date_to) if date_to else None

    # 2) Фильтрация по пересечению интервала [start,finish] с [date_from,date_to]
    filtered_rows: List[Tuple[PlannedOrder, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]] = []
    order_ids: List[int] = []
    for row in rows_joined:
        po, in_name, in_article, in_unit_guid, in_unit_short, in_unit_name, in_unit_code = row
        include_row = True
        start_dt = po.start_date.date() if isinstance(po.start_date, datetime) else po.start_date
        finish_dt = po.finish_date.date() if isinstance(po.finish_date, datetime) else po.finish_date

        if date_from_dt:
            if finish_dt is None or finish_dt < date_from_dt:
                include_row = False
        if include_row and date_to_dt:
            if start_dt is None or start_dt > date_to_dt:
                include_row = False
        if include_row:
            filtered_rows.append(row)
            order_ids.append(int(po.order_id))

    # 3) Подтянуть этапы по выбранным заказам
    stages: List[PlannedOrderStage] = []
    if order_ids:
        stages = (
            db.query(PlannedOrderStage)
            .filter(
                PlannedOrderStage.run_id == run_id,
                PlannedOrderStage.order_id.in_(order_ids),
            )
            .all()
        )

    # 4) Проставить area_name для этапов
    area_name_by_id: Dict[int, str] = {}
    try:
        area_ids: Set[int] = {int(s.area_id) for s in stages if getattr(s, "area_id", None) is not None}
        if area_ids:
            res_rows: List[ProductionResource] = (
                db.query(ProductionResource)
                .filter(ProductionResource.resource_id.in_(list(area_ids)))
                .all()
            )
            for r in res_rows:
                try:
                    area_name_by_id[int(r.resource_id)] = getattr(r, "resource_name", None) or ""
                except Exception:
                    continue
    except Exception:
        area_name_by_id = {}

    stage_by_order: Dict[int, List[Dict[str, Any]]] = {}
    for s in stages:
        aid = int(s.area_id) if s.area_id is not None else None
        aname = area_name_by_id.get(aid, "") if aid is not None else None
        hours_f = float(s.hours or 0.0)
        stage_by_order.setdefault(int(s.order_id), []).append(
            {
                "stage_id": int(s.stage_id),
                "area_id": aid,
                "area_name": aname,
                "bucket_type": "daily",
                "bucket_date": s.bucket_date.isoformat() if s.bucket_date else None,
                "hours": hours_f,
                "missingNorm": hours_f <= 1e-9,
            }
        )

    # 5) Построить группы по основному участку для каждого заказа
    groups_map: Dict[Optional[int], Dict[str, Any]] = {}
    today_d = date.today()
    turning_blank_priority = _load_turning_blank_priority_map(db, run_id)

    def _unit_display(_guid: Optional[str], _short: Optional[str], _name: Optional[str], _code: Optional[str]) -> str:
        return _unit_display_from_parts(_guid, _short, _name, _code)

    for row in filtered_rows:
        po, in_name, in_article, in_unit_guid, in_unit_short, in_unit_name, in_unit_code = row
        order_stages = stage_by_order.get(int(po.order_id), [])

        # Нормо-часы суммарно по этапам
        norm_total = float(sum(float(x.get("hours") or 0.0) for x in order_stages))

        # Основной участок = stage с максимальными hours
        main_area_id: Optional[int] = None
        if order_stages:
            try:
                best = max(order_stages, key=lambda s: float(s.get("hours") or 0.0))
                main_area_id = best.get("area_id")
            except Exception:
                main_area_id = None

        # Имя группы
        if main_area_id is None:
            grp_area_name = "Без участка"
        else:
            grp_area_name = area_name_by_id.get(int(main_area_id), "") or "Без участка"

        # Инициализация группы при первом заказе
        if main_area_id not in groups_map:
            groups_map[main_area_id] = {
                "area_id": main_area_id,
                "area_name": grp_area_name,
                "orders": [],
                "norm_sum_hours": 0.0,
                "min_days_to_need": None,
                "cap_overload_hours": 0.0,
                "cap_overloaded_buckets": 0,
            }

        unit_display = _unit_display(in_unit_guid, in_unit_short, in_unit_name, in_unit_code)
        qty_f = float(po.qty or 0.0)
        norm_per_unit = float(norm_total / qty_f) if qty_f > 1e-12 and norm_total > 0 else None
        badge = _turning_blank_badge(turning_blank_priority, int(po.item_id), po.need_date)

        order_entry = {
            "agg_key": f"{int(po.item_id)}|{unit_display}",
            "item_id": int(po.item_id),
            "item_name": in_name,
            "item_article": in_article,
            "unit": unit_display,
            "qty": qty_f,
            "norm_hours_total": float(norm_total),
            "norm_hours_per_unit": norm_per_unit,
            "order_id": int(po.order_id),
            "source_order_ids": [int(po.order_id)],
            "badge": badge,
            "turning_blank_priority": bool(badge),
        }

        grp = groups_map[main_area_id]
        grp["orders"].append(order_entry)
        grp["norm_sum_hours"] = float(grp.get("norm_sum_hours", 0.0) + norm_total)

        # min_days_to_need обновляем как минимум по (need_date - today).days
        try:
            if po.need_date:
                need_d = po.need_date.date() if isinstance(po.need_date, datetime) else po.need_date
                days = (need_d - today_d).days
                cur = grp.get("min_days_to_need")
                grp["min_days_to_need"] = days if (cur is None or days < int(cur)) else cur
        except Exception:
            pass

    # 6) Фильтр по area_id группы (если задан)
    if area_id is not None:
        groups_map = {k: v for k, v in groups_map.items() if (k is not None and int(k) == int(area_id))}

    # 7) Подтянуть агрегаты мощностей из capacity_load
    cap_q = db.query(CapacityLoad).filter(CapacityLoad.run_id == run_id)
    if date_from_dt:
        cap_q = cap_q.filter(CapacityLoad.bucket_date >= date_from_dt)
    if date_to_dt:
        cap_q = cap_q.filter(CapacityLoad.bucket_date <= date_to_dt)
    cap_rows: List[CapacityLoad] = cap_q.all()
    cap_map: Dict[int, Dict[str, float]] = {}
    for r in cap_rows:
        try:
            aid = int(r.area_id)
        except Exception:
            continue
        entry = cap_map.setdefault(aid, {"overload_hours": 0.0, "overloaded_buckets": 0.0})
        ov = float(r.overload_hours or 0.0)
        entry["overload_hours"] += ov
        if ov > 1e-9:
            entry["overloaded_buckets"] += 1.0

    # Применить capacity агрегаты к группам
    for k, grp in groups_map.items():
        if k is None:
            # Без участка оставляем нули
            continue
        cap = cap_map.get(int(k))
        if cap:
            grp["cap_overload_hours"] = float(cap.get("overload_hours", 0.0))
            grp["cap_overloaded_buckets"] = int(cap.get("overloaded_buckets", 0.0))

    # 8) Сортировка и пагинация
    groups_list = list(groups_map.values())

    # Обновим area_name для всех (на случай поздней загрузки)
    for g in groups_list:
        if g.get("area_id") is None:
            g["area_name"] = "Без участка"
        else:
            if not g.get("area_name"):
                g["area_name"] = area_name_by_id.get(int(g["area_id"]), "") or "Без участка"

    # Сортировка групп: по area_name ASC (сначала непустые)
    def _area_sort_key(g: Dict[str, Any]) -> Tuple[int, str]:
        nm = (g.get("area_name") or "").strip()
        return (0 if nm else 1, nm.lower())

    groups_list.sort(key=_area_sort_key)

    total_groups = len(groups_list)
    total_orders = sum(len(g.get("orders", []) or []) for g in groups_list)

    # Пагинация
    req_limit = int(limit or DEFAULT_PAGE_LIMIT)
    effective_limit = max(1, min(req_limit, SERVER_MAX_LIMIT))
    effective_offset = max(0, int(offset or 0))
    start_idx = effective_offset
    end_idx = start_idx + effective_limit
    groups_page = groups_list[start_idx:end_idx]

    return {
        "groups": groups_page,
        "total_groups": int(total_groups),
        "total_orders": int(total_orders),
        "limit": int(effective_limit),
        "offset": int(effective_offset),
    }


def get_run_capacity(
    db: Session,
    run_id: int,
    area_id: Optional[int] = None,
    bucket_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    q = db.query(CapacityLoad).filter(CapacityLoad.run_id == run_id)
    if area_id is not None:
        q = q.filter(CapacityLoad.area_id == int(area_id))
    # bucket_type removed from schema; all rows are daily
    if date_from:
        q = q.filter(CapacityLoad.bucket_date >= _to_date(date_from))
    if date_to:
        q = q.filter(CapacityLoad.bucket_date <= _to_date(date_to))

    total = q.count()
    rows: List[CapacityLoad] = (
        q.order_by(CapacityLoad.bucket_date.asc(), CapacityLoad.area_id.asc())
        .offset(max(0, int(offset)))
        .limit(max(1, min(int(limit or 20), 5000)))
        .all()
    )
    data = [
        {
            "area_id": int(r.area_id),
            "bucket_type": "daily",
            "bucket_date": r.bucket_date.isoformat() if r.bucket_date else None,
            "hours_planned": float(r.hours_planned or 0.0),
            "hours_available": float(r.hours_available or 0.0),
            "overload_hours": float(r.overload_hours or 0.0),
        }
        for r in rows
    ]
    return {"rows": data, "total": int(total), "limit": int(limit), "offset": int(offset)}


def get_run_pegging(
    db: Session,
    run_id: int,
    child_item_id: Optional[int] = None,
    parent_item_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> Dict[str, Any]:
    q = db.query(PeggingLink).filter(PeggingLink.run_id == run_id)
    if child_item_id is not None:
        q = q.filter(PeggingLink.child_item_id == int(child_item_id))
    if parent_item_id is not None:
        q = q.filter(PeggingLink.parent_item_id == int(parent_item_id))
    if date_from:
        q = q.filter(PeggingLink.need_date >= _to_date(date_from))
    if date_to:
        q = q.filter(PeggingLink.need_date <= _to_date(date_to))

    total = q.count()
    rows: List[PeggingLink] = (
        q.order_by(PeggingLink.need_date.asc(), PeggingLink.id.asc())
        .offset(max(0, int(offset)))
        .limit(max(1, min(int(limit or 200), 5000)))
        .all()
    )
    data = [
        {
            "id": int(r.id),
            "child_item_id": int(r.child_item_id),
            "parent_item_id": int(r.parent_item_id) if r.parent_item_id is not None else None,
            "demand_ref": r.demand_ref,
            "qty_contribution": float(r.qty_contribution or 0.0),
            "need_date": r.need_date.isoformat() if r.need_date else None,
            "parent_need_date": r.parent_need_date.isoformat() if r.parent_need_date else None,
        }
        for r in rows
    ]
    return {"rows": data, "total": int(total), "limit": int(limit), "offset": int(offset)}
