from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set, DefaultDict, Callable

from sqlalchemy.orm import Session, load_only
from sqlalchemy import func, and_, asc, desc
from collections import defaultdict
import json
import re
import math
import logging
logger = logging.getLogger("prodplan.planning")

from ..models import (
    PlanningConfigVersion,
    PlanningRun,
    PlannedOrder,
    PlannedOrderStage,
    PlannedPurchase,
    CapacityLoad,
    PeggingLink,
    Item,
    Unit,
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
)
from ..models import RootProduct
from .stage_logic import determine_parent_stage_and_norm
from .order_quantity_calculator import OrderQuantityCalculator
from .priority_manager import PriorityManager
from .capacity_scheduler import CapacityScheduler
from .pegging_builder import PeggingBuilder
from .warnings import make_warning, log_warning

# Default planning config fallback (aligned with Alembic seed)
DEFAULT_PLANNING_CONFIG: Dict[str, Any] = {
    "planning_horizon_days": 90,
    "mps_daily_horizon_days": 90,
    "weekly": {"enabled": True, "anchor_day": "Monday", "need_date_day": "Friday"},
    "procurement": {
        "default_lead_time_days": 30,
        "lead_time_min_policy": "max(default_lead_time_days, lead_time_from_item)",
        "lot_sizing": {"moq_source": "item_card_or_1", "multiple": 1, "rounding": "ceil"},
        "order_date_rounding_policy": "previous_workday",
    },
    "production": {"lot_sizing": {"min_batch": 1, "multiple": 1, "rounding": "ceil"}},
    "safety_stock_percent": 1,
    "capacity": {"use_resource_calendars": True, "consider_power_coefficients": True},
    "prioritization": {
        "weight_criticality": 0.4,
        "weight_importance": 0.3,
        "weight_cycle_time": 0.3,
        "default_importance": 1,
    },
    "toggles": {"include_wip": False, "enable_weekly_route_detail": False},
}

# Pagination constants
SERVER_MAX_LIMIT = 1000
DEFAULT_PAGE_LIMIT = 50


def _ensure_dict(raw: Any) -> Dict[str, Any]:
    """
    Try to robustly convert various JSON/Mapping representations to a plain dict.
    Gracefully handles:
      - dict-like objects
      - JSON string
      - objects implementing .items()
      - list of (key, value) pairs
    Returns {} if conversion is not possible.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return dict(parsed)
            else:
                return {}
        except Exception:
            return {}
    # Mapping protocol
    try:
        # This can still raise if raw is an iterable of single values (e.g. list of strings)
        return dict(raw or {})
    except Exception:
        pass
    # Try items()
    try:
        return dict(getattr(raw, "items")())
    except Exception:
        pass
    # Try sequence of pairs
    try:
        return {k: v for (k, v) in list(raw)}
    except Exception:
        pass
    return {}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursive dict merge: override values take precedence."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override if override is not None else base
    result = dict(base)
    for k, v in (override or {}).items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def get_active_planning_config(db: Session) -> Tuple[int, Dict[str, Any]]:
    """Fetch active planning configuration snapshot and its version id."""
    cfg: Optional[PlanningConfigVersion] = (
        db.query(PlanningConfigVersion)
        .filter(PlanningConfigVersion.is_active.is_(True))
        .order_by(PlanningConfigVersion.created_at.desc())
        .first()
    )
    if not cfg:
        raise RuntimeError("Active planning configuration is not found. Seed config first.")
    cfg_dict = _ensure_dict(getattr(cfg, "config", {}))
    return int(cfg.id), cfg_dict


def list_planning_configs(db: Session, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """
    List planning configuration versions with pagination.
    Returns: {"rows":[{id,version,is_active,created_at,created_by,comment}], "total", "limit", "offset"}
    """
    q = (
        db.query(PlanningConfigVersion)
        .order_by(PlanningConfigVersion.created_at.desc())
        .offset(max(0, int(offset)))
        .limit(max(1, min(int(limit or 50), 200)))
    )
    rows: List[PlanningConfigVersion] = q.all()
    total = db.query(func.count(PlanningConfigVersion.id)).scalar() or 0

    result: List[Dict[str, Any]] = []
    for r in rows:
        result.append(
            {
                "id": int(r.id),
                "version": int(r.version),
                "is_active": bool(r.is_active),
                "created_at": r.created_at.isoformat() if getattr(r, "created_at", None) else None,
                "created_by": getattr(r, "created_by", None),
                "comment": getattr(r, "comment", None),
            }
        )
    return {"rows": result, "total": int(total), "limit": int(limit), "offset": int(offset)}


def create_planning_config_version(
    db: Session,
    config: Any,
    comment: Optional[str] = None,
    created_by: Optional[str] = None,
    activate: bool = False,
) -> Dict[str, Any]:
    """
    Create a new planning configuration version.
    - Computes next version = max(version)+1
    - Optionally activates the new version atomically (deactivates old)
    Returns: {"id", "version", "is_active"}
    """
    cfg_dict = _ensure_dict(config)
    if not cfg_dict:
        raise RuntimeError("Config must be a non-empty object")

    current_max_version = db.query(func.max(PlanningConfigVersion.version)).scalar() or 0
    new_version = int(current_max_version) + 1

    rec = PlanningConfigVersion(
        version=int(new_version),
        is_active=False,
        config=cfg_dict,
        comment=comment,
        created_by=created_by,
    )
    db.add(rec)
    db.flush()
    config_id = int(rec.id)

    if activate:
        # Deactivate others and activate new
        db.query(PlanningConfigVersion).filter(PlanningConfigVersion.is_active.is_(True)).update(
            {"is_active": False}
        )
        rec.is_active = True

    db.commit()
    return {"id": config_id, "version": int(rec.version), "is_active": bool(rec.is_active)}


def activate_planning_config_version(db: Session, config_id: int) -> Dict[str, Any]:
    """
    Activate specified config version (set it active, deactivate previous active).
    Returns: {"id", "version", "is_active": True}
    """
    target: Optional[PlanningConfigVersion] = (
        db.query(PlanningConfigVersion).filter(PlanningConfigVersion.id == int(config_id)).first()
    )
    if not target:
        raise RuntimeError(f"PlanningConfigVersion {config_id} not found")

    # Deactivate previous active
    db.query(PlanningConfigVersion).filter(PlanningConfigVersion.is_active.is_(True)).update(
        {"is_active": False}
    )
    target.is_active = True
    db.commit()

    return {"id": int(target.id), "version": int(target.version), "is_active": True}


def get_active_planning_config_full(db: Session) -> Dict[str, Any]:
    """
    Return full active planning configuration record including config JSON.
    Returns: {"id","version","is_active","config","comment","created_by","created_at"}
    """
    rec: Optional[PlanningConfigVersion] = (
        db.query(PlanningConfigVersion)
        .filter(PlanningConfigVersion.is_active.is_(True))
        .order_by(PlanningConfigVersion.created_at.desc())
        .first()
    )
    if not rec:
        raise RuntimeError("Active planning configuration is not found. Seed config first.")
    return {
        "id": int(rec.id),
        "version": int(rec.version),
        "is_active": True,
        "config": _ensure_dict(getattr(rec, "config", {})),
        "comment": getattr(rec, "comment", None),
        "created_by": getattr(rec, "created_by", None),
        "created_at": rec.created_at.isoformat() if getattr(rec, "created_at", None) else None,
    }

def _to_date(val: Any) -> date:
    """Robustly convert string/datetime to date object"""
    if isinstance(val, date):
        return val
    if isinstance(val, datetime):
        return val.date()
    if not isinstance(val, str):
        raise TypeError(f"Cannot convert {type(val)} to date")
    return datetime.fromisoformat(val.replace("Z", "+00:00")).date()

def _get_or_create_run(
    db: Session,
    run_id: Optional[int],
    horizon_days: Optional[int] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
    started_by: Optional[str] = None,
) -> PlanningRun:
    """
    Get existing or create a new planning_run row with merged config snapshot.
    """
    if run_id:
        run = db.query(PlanningRun).filter(PlanningRun.run_id == run_id).first()
        if not run:
            raise RuntimeError(f"Run {run_id} not found")
        return run

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

    run = PlanningRun(
        status="IN_PROGRESS",
        started_by=started_by or "api",
        horizon_days=int(snapshot.get("planning_horizon_days", 90)),
        config_version_id=cfg_id,
        config_snapshot=snapshot,
        warnings=[],
        kpi={},
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    return run


def list_planning_runs(db: Session, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    q = (
        db.query(PlanningRun)
        .order_by(PlanningRun.started_at.desc())
        .offset(max(0, int(offset)))
        .limit(max(1, min(int(limit or 50), 200)))
    )
    rows: List[PlanningRun] = q.all()

    result: List[Dict[str, Any]] = []
    for r in rows:
        order_cnt = db.query(func.count(PlannedOrder.order_id)).filter(PlannedOrder.run_id == r.run_id).scalar() or 0
        purch_cnt = db.query(func.count(PlannedPurchase.purchase_id)).filter(PlannedPurchase.run_id == r.run_id).scalar() or 0
        overload_cnt = (
            db.query(func.count(CapacityLoad.id))
            .filter(and_(CapacityLoad.run_id == r.run_id, CapacityLoad.overload_hours > 0))
            .scalar()
            or 0
        )
        result.append(
            {
                "run_id": int(r.run_id),
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "horizon_days": r.horizon_days,
                "pinned": bool(getattr(r, "pinned", False)),
                "order_count": int(order_cnt),
                "purchase_count": int(purch_cnt),
                "overload_buckets": int(overload_cnt),
            }
        )

    total = db.query(func.count(PlanningRun.run_id)).scalar() or 0
    return {"rows": result, "total": int(total), "limit": limit, "offset": offset}


def get_run_summary(db: Session, run_id: int) -> Dict[str, Any]:
    r: Optional[PlanningRun] = db.query(PlanningRun).filter(PlanningRun.run_id == run_id).first()
    if not r:
        raise RuntimeError(f"Run {run_id} not found")

    order_cnt = db.query(func.count(PlannedOrder.order_id)).filter(PlannedOrder.run_id == run_id).scalar() or 0
    purch_cnt = db.query(func.count(PlannedPurchase.purchase_id)).filter(PlannedPurchase.run_id == run_id).scalar() or 0

    cap_rows: List[CapacityLoad] = db.query(CapacityLoad).filter(CapacityLoad.run_id == run_id).all()
    overload_total = float(sum(float(x.overload_hours or 0.0) for x in cap_rows))
    overloaded_buckets = int(sum(1 for x in cap_rows if float(x.overload_hours or 0.0) > 0.0))

    # Normalize/augment warnings for structured aggregation
    raw_warnings = list(r.warnings or [])
    normalized_warnings: List[Dict[str, Any]] = []
    for w in raw_warnings:
        if isinstance(w, dict):
            w2 = dict(w)
        else:
            # tolerate string warnings
            w2 = {"code": str(w)}
        code = str(w2.get("code") or "").upper()
        # Map legacy/internal code to UI-expected semantic
        if code == "PRODUCTION_KIND_NOT_FOUND":
            w2["code"] = "NO_PRODUCTION_KIND"
        normalized_warnings.append(w2)

    # Component shortages aggregation
    component_blocked = sum(1 for w in normalized_warnings if str(w.get("code")) == "COMPONENT_SHORTAGE_BLOCKED")
    component_partial = sum(1 for w in normalized_warnings if str(w.get("code")) == "COMPONENT_SHORTAGE_PARTIAL")

    # Kind issues aggregation (missing kind/area binding)
    kind_issue_codes = {"NO_PRODUCTION_KIND", "NO_AREA_FOR_PRODUCTION_KIND"}
    kind_issues_list = [w for w in normalized_warnings if str(w.get("code")) in kind_issue_codes]
    kind_by_code: Dict[str, int] = {}
    for w in kind_issues_list:
        c = str(w.get("code"))
        kind_by_code[c] = kind_by_code.get(c, 0) + 1

    # Missing norms: count orders whose sum of stage hours <= 0
    stage_sums = (
        db.query(PlannedOrderStage.order_id, func.sum(func.coalesce(PlannedOrderStage.hours, 0.0)))
        .filter(PlannedOrderStage.run_id == run_id)
        .group_by(PlannedOrderStage.order_id)
        .all()
    )
    missing_norm_orders = int(sum(1 for _, s in stage_sums if float(s or 0.0) <= 1e-9))

    return {
        "run": {
            "run_id": int(r.run_id),
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "horizon_days": r.horizon_days,
            "pinned": bool(getattr(r, "pinned", False)),
        },
        "counts": {"production_orders": int(order_cnt), "purchase_requests": int(purch_cnt)},
        "capacity": {"overload_total": overload_total, "overloaded_buckets": overloaded_buckets},
        "kpi": r.kpi or {},
        # keep original warnings for backward compatibility
        "warnings": normalized_warnings,
        # structured fields for UI
        "kindIssues": {
            "total": int(len(kind_issues_list)),
            "byCode": kind_by_code,
            "list": kind_issues_list,
        },
        "missingNorms": {
            "total": missing_norm_orders
        },
        "componentShortages": {
            "blocked": int(component_blocked),
            "partial": int(component_partial),
        },
    }


def get_run_production(
    db: Session,
    run_id: int,
    item_id: Optional[int] = None,
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
    
    for row in filtered_rows:
        po, in_name, in_article, in_unit_guid, in_unit_short, in_unit_name, in_unit_code = row
        order_ids.append(int(po.order_id))
        
        start_iso = po.start_date.isoformat() if po.start_date else ""
        if not start_iso:
            # fallback to finish_date if start_date is missing
            fin_dt = po.finish_date.isoformat() if po.finish_date else ""
            start_iso = fin_dt
        unit_display = (in_unit_short or in_unit_name or in_unit_code or in_unit_guid or "").strip()
        agg_key = (int(po.item_id), start_iso, unit_display)
        
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
                "stages": [],
                "norm_hours_total": 0.0,
                "norm_hours_per_unit": None,
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
                # stage-level flag for missing norm
                "missingNorm": hours_f <= 1e-9,
            }
        )

    # Combine per-order info into aggregated rows and compute flags
    for row in filtered_rows:
        po, in_name, in_article, in_unit_guid, in_unit_short, in_unit_name, in_unit_code = row

        unit_display = (in_unit_short or in_unit_name or in_unit_code or in_unit_guid or "").strip()
        start_iso = (
            po.start_date.isoformat()
            if po.start_date
            else (po.finish_date.isoformat() if po.finish_date else "")
        )
        agg_key = (int(po.item_id), start_iso, unit_display)

        order_stages = stage_by_order.get(int(po.order_id), [])
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
                "stages": [],
                "norm_hours_total": 0.0,
                "norm_hours_per_unit": None,
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

        # stable synthetic order_id for UI tables (aggregated view)
        data["order_id"] = hash(f"{data['item_id']}_{data['start_date']}_{data['unit']}") % (10**10)
        
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
        if len(seq) >= 16:
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
                in_name,
                in_article,
                in_unit_guid,
                in_unit_short,
                in_unit_name,
                in_unit_code,
            ) = seq[:16]
        elif len(seq) >= 15:
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
                    in_name,
                    in_article,
                    in_unit_guid,
                    in_unit_short,
                    in_unit_name,
                    in_unit_code,
                )
            )
 
    aggregated_data: Dict[Tuple[int, str], Dict[str, Any]] = {}
    
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
        
        unit_display = (in_unit_short or in_unit_name or in_unit_code or in_unit_guid or "").strip()
        agg_key = (int(item_id_val), unit_display)
        
        if agg_key not in aggregated_data:
            aggregated_data[agg_key] = {
                "item_id": int(item_id_val),
                "item_name": in_name,
                "item_article": in_article,
                "unit": unit_display,
                "qty": 0.0,
                "need_date": need_date_val.isoformat() if need_date_val else None,
                "order_date": order_date_val.isoformat() if order_date_val else None,
                "lead_time_days": int(lead_time_days_val or 0),
                "priority_index": float(priority_index_val or 0.0) if priority_index_val is not None else None,
                "bucket_type": "daily",
                "bucket_date": bucket_date_val.isoformat() if bucket_date_val else None,
                "supplier_ref1c": supplier_ref1c_val,
            }
        
        aggregated_data[agg_key]["qty"] += float(qty_val or 0.0)

    data: List[Dict[str, Any]] = []
    for key in sorted(aggregated_data.keys()):
        values = aggregated_data[key]
        values["purchase_id"] = hash(
            f"{values['item_id']}_{values['unit']}_{values['need_date'] or ''}_{values['bucket_type']}_{values['bucket_date']}"
        ) % (10**10)
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
            normalized["purchase_id"] = int(row.get("purchase_id") or 0)
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

    def _unit_display(_guid: Optional[str], _short: Optional[str], _name: Optional[str], _code: Optional[str]) -> str:
        return ( (_short or "") or (_name or "") or (_code or "") or (_guid or "") ).strip()

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

def _read_last_stock_sync_at() -> Optional[str]:
    from pathlib import Path
    p = Path("config") / "last_sync_time.json"
    if not p.exists():
        return None
    try:
        import json
        data = json.loads(p.read_text("utf-8") or "{}")
        val = str(data.get("last_sync") or "").strip()
        return val or None
    except Exception:
        return None


# Backward-compatibility stub for tests that monkeypatch this symbol
def _generate_shortage_report_v2(db: Session, run_id: int) -> Dict[str, Any]:
    """
    Placeholder function kept for test monkeypatching.
    Real implementation lives elsewhere; tests replace this via monkeypatch.setattr.
    """
    raise NotImplementedError("Use monkeypatch to stub _generate_shortage_report_v2 in tests")


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
    gross_req_result = compute_gross_requirements(db, horizon_days, config_overrides)
    gross_serialized = gross_req_result["gross"]

    gross_map = {
        int(item_id): {datetime.fromisoformat(d).date(): float(qty) for d, qty in buckets.items()}
        for item_id, buckets in gross_serialized.items()
    }

    item_ids = set(gross_map.keys())
    if not item_ids:
        gross_req_result.update({"net": {}})
        return gross_req_result

    snapshot = gross_req_result.get("snapshot", {}) or {}
    include_wip = bool(snapshot.get("toggles", {}).get("include_wip", True))

    items = db.query(Item).filter(Item.item_id.in_(item_ids)).all()
    stock_by_item = {int(i.item_id): float(i.stock_qty or 0.0) for i in items}

    wip_by_item: Dict[int, float] = {}
    if include_wip:
        try:
            from ..models import ProductionProduct
            wip_rows = (
                db.query(ProductionProduct.item_id, func.sum(ProductionProduct.quantity))
                .group_by(ProductionProduct.item_id)
                .all()
            )
            wip_by_item = {int(iid): float(qty or 0.0) for iid, qty in wip_rows}
        except Exception:
            wip_by_item = {}

    net_map: DefaultDict[int, Dict[date, float]] = defaultdict(dict)

    for iid in item_ids:
        available_qty = float(stock_by_item.get(iid, 0.0))
        if include_wip:
            available_qty += float(wip_by_item.get(iid, 0.0))

        buckets = sorted(gross_map.get(iid, {}).items(), key=lambda x: x[0])
        for bucket_date, bucket_qty in buckets:
            if available_qty >= bucket_qty:
                available_qty -= bucket_qty
            else:
                net_qty = bucket_qty - available_qty
                available_qty = 0.0
                net_map[iid][bucket_date] = net_qty

    def serialize_net(bmap: DefaultDict[int, Dict[date, float]]) -> Dict[str, Dict[str, float]]:
        return {
            str(iid): {dt.isoformat(): qty for dt, qty in sorted(dtmap.items())}
            for iid, dtmap in bmap.items()
        }

    gross_req_result.update({"net": serialize_net(net_map)})
    return gross_req_result

# --- Main Planning Run ---

def build_planned_orders_and_purchases(
    db: Session,
    run: PlanningRun,
    net_requirements: Dict[str, Any],
    order_qty_calculator: OrderQuantityCalculator,
    priority_manager: PriorityManager,
    item_cache: Dict[int, Item],
    units_by_ref: Dict[str, Unit],
) -> Dict[str, Any]:
    
    run_id = run.run_id
    config = run.config_snapshot
    warnings = []
    created_orders = []
    created_purchases = []

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

    for req in sorted(all_reqs, key=lambda x: x["need_date"]):
        item_id = req["item_id"]
        need_date = req["need_date"]
        requested_qty = float(req["qty"] or 0.0)
        
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

        # Helper: determine discreteness (whole units) by Unit settings
        def _is_discrete_unit(it: Optional[Item]) -> bool:
            try:
                ref = getattr(it, "unit", None) if it is not None else None
                u = units_by_ref.get(ref) if ref else None
                if u is not None:
                    try:
                        prec = getattr(u, "precision", None)
                        if prec is not None and int(prec) == 0:
                            return True
                    except Exception:
                        pass
                    short = str(getattr(u, "short_name", None) or "").strip().lower()
                    if short in {"шт", "pcs", "pc"}:
                        return True
                    if short in {"кг", "kg", "м", "m", "мм", "cm", "л", "l"}:
                        return False
                # Fallback: treat as discrete
                return True
            except Exception:
                return True

        # Определение типа потока: производство или закупка.
        # Логика согласована с документацией модуля расчёта заказов:
        # если в наименовании способа пополнения содержится "покуп", "закуп", "purchase", "buy" —
        # считаем, что это закупка; во всех остальных случаях — производство.
        method_raw = str(getattr(item, "replenishment_method", "") or "").strip().lower()
        is_purchase = False
        if method_raw:
            purchase_markers = ["покуп", "закуп", "purchase", "buy"]
            if any(marker in method_raw for marker in purchase_markers):
                is_purchase = True

        is_produced = not is_purchase
        
        if is_produced:
            # Compute quantity with diagnostics (component_limit + horizon_limit)
            final_qty_before, normalized_qty, comp_details, comp_warnings = order_qty_calculator.compute(item_id, requested_qty)
            warnings.extend(comp_warnings)

            horizon_limit = float(comp_details.get("horizon_limit", float(requested_qty)))
            component_limit = float(comp_details.get("component_limit", float(requested_qty)))

            # If item is discrete, requested must be whole after compute()
            if _is_discrete_unit(item):
                try:
                    requested_qty = float(math.floor(final_qty_before + 1e-9))
                except Exception:
                    requested_qty = float(int(final_qty_before))
            else:
                requested_qty = float(final_qty_before)

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

            # - If 0 < component_limit < requested_qty -> plan partial by components within horizon
            if component_limit + 1e-9 < float(requested_qty):
                planned_qty = min(component_limit, horizon_limit)
                warnings.append(
                    make_warning(
                        "COMPONENT_SHORTAGE_PARTIAL",
                        "Частичное планирование из-за дефицита комплектующих",
                        item_id=int(item_id),
                        requested_qty=float(requested_qty),
                        planned_qty=float(planned_qty),
                        component_limit=float(component_limit),
                    )
                )
            else:
                # Otherwise, use normalized lot sizing but never exceed horizon/component constraints
                planned_qty = min(float(normalized_qty or 0.0), horizon_limit, component_limit)

            planned_qty = float(planned_qty or 0.0)

            # Enforce whole units for discrete items
            if _is_discrete_unit(item):
                try:
                    planned_qty = float(math.floor(planned_qty + 1e-9))
                except Exception:
                    planned_qty = float(int(planned_qty))

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
        else:  # purchased
            lead_time = item.replenishment_time or 30
            order_date = need_date - timedelta(days=lead_time)
            planned_qty = float(requested_qty)
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

    db.add_all(created_orders)
    db.add_all(created_purchases)
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
            # Раньше формировалось предупреждение NO_AREA_FOR_PRODUCTION_KIND, которое использовалось на фронтенде.
            # Восстанавливаем его генерацию (даже если далее сработает фолбэк по ResourceStage).
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
            
            # Resolve area_id with fallback via ResourceStage if needed
            area_resolved = resource_kind.resource_id if resource_kind else None
            if area_resolved is None and spec_op.stage_id:
                try:
                    rs = db.query(ResourceStage).filter(ResourceStage.stage_id == spec_op.stage_id).first()
                    if rs:
                        area_resolved = int(rs.resource_id)
                except Exception:
                    area_resolved = None

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

    # Process orders one by one in priority order
    orders_to_schedule = (
        db.query(PlannedOrder)
        .filter(PlannedOrder.run_id == run_id)
        .order_by(desc(PlannedOrder.priority_index), PlannedOrder.need_date)
        .all()
    )

    for order in orders_to_schedule:
        stages = db.query(PlannedOrderStage).filter(PlannedOrderStage.order_id == order.order_id).all()
        if not stages:
            continue

        # 1. Capacity analytics only (no qty modifications)
        stage_hours = {s.stage_id: s.hours for s in stages}
        stage_areas = {s.stage_id: (int(s.area_id) if s.area_id is not None else None) for s in stages}

        # Keep analytic warnings but do not mutate order.qty or stage.hours
        try:
            _, _, limit_warnings = capacity_scheduler.limit_qty_by_capacity(
                order.item_id, order.qty, order.need_date, stage_hours, stage_areas_by_stage=stage_areas
            )
            warnings.extend(limit_warnings)
        except Exception:
            # If analytic fails, ignore and proceed to scheduling
            pass

        # 2. Schedule with push-right; qty remains unchanged
        final_stages_with_hours = {s.stage_id: s.hours for s in stages}
        schedule_result, schedule_warnings = capacity_scheduler.schedule_backward(
            order.item_id, order.qty, order.need_date, final_stages_with_hours, stage_areas_by_stage=stage_areas
        )
        # Enrich warnings with run_id/order_id
        for w in schedule_warnings:
            try:
                w["run_id"] = int(run_id)
                w["order_id"] = int(order.order_id)
            except Exception:
                pass
        warnings.extend(schedule_warnings)

        order.start_date = schedule_result.get("order_start_date")
        order.finish_date = schedule_result.get("order_finish_date")

        for stage in stages:
            stage_dates = schedule_result.get("stage_dates", {}).get(stage.stage_id)
            if stage_dates:
                stage.start_date = stage_dates["start"]
                stage.finish_date = stage_dates["finish"]
                # Bucket date should reflect when the work is actually happening
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


def run_planning_run(
    db: Session,
    run_id: Optional[int] = None,
    horizon_days: Optional[int] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
    started_by: Optional[str] = None,
) -> int:
    
    run = _get_or_create_run(db, run_id, horizon_days, config_overrides, started_by)
    
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

        all_resources = db.query(ProductionResource).all()
        res_by_id = {r.resource_id: r for r in all_resources}

        all_res_kinds = db.query(ResourceProductionKind).all()
        production_kinds_by_resource = defaultdict(set)
        for rk in all_res_kinds:
            production_kinds_by_resource[rk.resource_id].add(rk.production_kind_id)

        stock_by_item = {i.item_id: i.stock_qty for i in item_cache.values()}
        
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
            stock_by_item=stock_by_item,
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

        # --- PHASE 1: Build Orders and Purchases ---
        order_result = build_planned_orders_and_purchases(
            db, run, net_requirements, order_qty_calculator, priority_manager, item_cache, units_by_ref
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

    except Exception as e:
        logger.exception(f"Planning run {run.run_id} failed.")
        run.status = "FAILURE"
        run.warnings = (run.warnings or []) + [make_warning("PLANNING_RUN_FAILED", msg=f"Critical error during planning run: {e}", error=str(e))]
        raise
    finally:
        run.finished_at = datetime.utcnow()
        db.commit()
    
    return run.run_id
