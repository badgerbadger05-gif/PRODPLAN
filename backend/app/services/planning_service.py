from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple, Set, DefaultDict

from sqlalchemy.orm import Session
from sqlalchemy import func, and_, asc, desc
from collections import defaultdict
import json
import re
import math

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
    ProductionKind,
    ResourceProductionKind,
    Specification,
)
from .stage_logic import determine_parent_stage_and_norm

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


def create_planning_run(
    db: Session,
    horizon_days: Optional[int] = None,
    use_weekly: Optional[bool] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
    started_by: Optional[str] = None,
) -> int:
    """
    Create planning_run row with merged config snapshot.
    Note: actual calculation is not implemented yet; this creates a run placeholder.
    """
    try:
        try:
            cfg_id, cfg = get_active_planning_config(db)
        except Exception:
            cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)
    except Exception:
        cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)

    # apply simple overrides into a snapshot (non-destructive deep merge)
    overrides: Dict[str, Any] = {}
    if horizon_days is not None:
        overrides["planning_horizon_days"] = int(horizon_days)
    if use_weekly is not None:
        overrides.setdefault("weekly", {})
        overrides["weekly"]["enabled"] = bool(use_weekly)
    if config_overrides:
        overrides = _deep_merge(overrides, config_overrides)

    try:
        snapshot = _deep_merge(cfg, overrides)
    except Exception:
        snapshot = dict(cfg) if isinstance(cfg, dict) else dict(DEFAULT_PLANNING_CONFIG)

    run = PlanningRun(
        status="SUCCESS",  # placeholder until actual calc implemented
        started_by=started_by or "api",
        horizon_days=int(snapshot.get("planning_horizon_days") or snapshot.get("mps_daily_horizon_days", 90)),
        use_weekly=bool(snapshot.get("weekly", {}).get("enabled", True)),
        config_version_id=cfg_id,
        config_snapshot=snapshot,
        warnings=[],
        kpi={},
        started_at=datetime.utcnow(),
        finished_at=datetime.utcnow(),
    )
    db.add(run)
    db.flush()
    run_id = int(run.run_id)
    db.commit()
    return run_id


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
        # lightweight metrics
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
                "use_weekly": r.use_weekly,
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

    return {
        "run": {
            "run_id": int(r.run_id),
            "status": r.status,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "horizon_days": r.horizon_days,
            "use_weekly": r.use_weekly,
            "pinned": bool(getattr(r, "pinned", False)),
        },
        "counts": {"production_orders": int(order_cnt), "purchase_requests": int(purch_cnt)},
        "capacity": {"overload_total": overload_total, "overloaded_buckets": overloaded_buckets},
        "kpi": r.kpi or {},
        "warnings": r.warnings or [],
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
    """
    Возвращает производственные заказы по прогону с денормализованными полями номенклатуры,
    поддержкой сортировки и агрегатом total_qty. Также рассчитывает нормативы:
      - norm_hours_total: сумма часов по PlannedOrderStage для заказа
      - norm_hours_per_unit: norm_hours_total / qty (если qty > 0)
    Колонки для сортировки: item_name | item_article | qty | need_date | bucket_date | priority_index
    """
    # Базовый запрос для total/total_qty
    base_q = db.query(PlannedOrder).filter(PlannedOrder.run_id == run_id)
    if item_id is not None:
        base_q = base_q.filter(PlannedOrder.item_id == int(item_id))
    if bucket_type in {"daily", "weekly"}:
        base_q = base_q.filter(PlannedOrder.bucket_type == bucket_type)
    if date_from:
        base_q = base_q.filter(PlannedOrder.bucket_date >= _to_date(date_from))
    if date_to:
        base_q = base_q.filter(PlannedOrder.bucket_date <= _to_date(date_to))

    total = base_q.count()
    total_qty_q = db.query(func.coalesce(func.sum(PlannedOrder.qty), 0.0)).filter(PlannedOrder.run_id == run_id)
    if item_id is not None:
        total_qty_q = total_qty_q.filter(PlannedOrder.item_id == int(item_id))
    if bucket_type in {"daily", "weekly"}:
        total_qty_q = total_qty_q.filter(PlannedOrder.bucket_type == bucket_type)
    if date_from:
        total_qty_q = total_qty_q.filter(PlannedOrder.bucket_date >= _to_date(date_from))
    if date_to:
        total_qty_q = total_qty_q.filter(PlannedOrder.bucket_date <= _to_date(date_to))
    total_qty_val = float(total_qty_q.scalar() or 0.0)

    # Запрос с join к Item для денормализации и сортировки
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
    if bucket_type in {"daily", "weekly"}:
        q = q.filter(PlannedOrder.bucket_type == bucket_type)
    if date_from:
        q = q.filter(PlannedOrder.bucket_date >= _to_date(date_from))
    if date_to:
        q = q.filter(PlannedOrder.bucket_date <= _to_date(date_to))

    sort_map = {
        "item_name": Item.item_name,
        "item_article": Item.item_article,
        "qty": PlannedOrder.qty,
        "need_date": PlannedOrder.need_date,
        "bucket_date": PlannedOrder.bucket_date,
        "priority_index": PlannedOrder.priority_index,
    }
    sb = (sort_by or "bucket_date").strip().lower()
    sd = (sort_dir or "asc").strip().lower()
    col = sort_map.get(sb, PlannedOrder.bucket_date)
    dir_fn = desc if sd == "desc" else asc
    q = q.order_by(dir_fn(col), PlannedOrder.order_id.asc())

    rows_joined = (
        q.offset(max(0, int(offset)))
         .limit(max(1, min(int(limit or 100), 1000)))
         .all()
    )

    # Stages только по выбранным заказам
    order_ids = [int(row[0].order_id) for row in rows_joined]
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

    stage_by_order: Dict[int, List[Dict[str, Any]]] = {}
    for s in stages:
        stage_by_order.setdefault(int(s.order_id), []).append(
            {
                "stage_id": int(s.stage_id),
                "area_id": int(s.area_id) if s.area_id is not None else None,
                "bucket_type": s.bucket_type,
                "bucket_date": s.bucket_date.isoformat() if s.bucket_date else None,
                "hours": float(s.hours or 0.0),
            }
        )

    data: List[Dict[str, Any]] = []
    for row in rows_joined:
        po, in_name, in_article, in_unit_guid, in_unit_short, in_unit_name, in_unit_code = row
        st_list = stage_by_order.get(int(po.order_id), [])
        norm_total = float(sum(float(x.get("hours") or 0.0) for x in st_list))
        qty_val = float(po.qty or 0.0)
        norm_per_unit = float(norm_total / qty_val) if qty_val > 1e-12 else None
        display_unit = in_unit_short or in_unit_name or in_unit_code or in_unit_guid
        data.append(
            {
                "order_id": int(po.order_id),
                "item_id": int(po.item_id),
                "item_name": in_name,
                "item_article": in_article,
                "unit": display_unit,
                "qty": qty_val,
                "need_date": po.need_date.isoformat() if po.need_date else None,
                "start_date": po.start_date.isoformat() if po.start_date else None,
                "finish_date": po.finish_date.isoformat() if po.finish_date else None,
                "route_ref": po.route_ref,
                "priority_index": float(po.priority_index or 0.0) if po.priority_index is not None else None,
                "bucket_type": po.bucket_type,
                "bucket_date": po.bucket_date.isoformat() if po.bucket_date else None,
                "demand_ref": po.demand_ref,
                "demand_date": po.demand_date.isoformat() if po.demand_date else None,
                "stages": st_list,
                "norm_hours_total": float(norm_total),
                "norm_hours_per_unit": float(norm_per_unit) if norm_per_unit is not None else None,
            }
        )

    return {
        "rows": data,
        "total": int(total),
        "total_qty": float(total_qty_val),
        "limit": int(limit),
        "offset": int(offset),
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
    """
    Возвращает заявки на закупку по прогону с денормализованными полями номенклатуры,
    поддержкой сортировки и агрегатом total_qty.
    Колонки для сортировки: item_name | item_article | qty | need_date | order_date | bucket_date | priority_index
    """
    # Базовый запрос для total/total_qty
    base_q = db.query(PlannedPurchase).filter(PlannedPurchase.run_id == run_id)
    if item_id is not None:
        base_q = base_q.filter(PlannedPurchase.item_id == int(item_id))
    if bucket_type in {"daily", "weekly"}:
        base_q = base_q.filter(PlannedPurchase.bucket_type == bucket_type)
    if date_from:
        base_q = base_q.filter(PlannedPurchase.bucket_date >= _to_date(date_from))
    if date_to:
        base_q = base_q.filter(PlannedPurchase.bucket_date <= _to_date(date_to))

    total = base_q.count()
    total_qty_q = db.query(func.coalesce(func.sum(PlannedPurchase.qty), 0.0)).filter(PlannedPurchase.run_id == run_id)
    if item_id is not None:
        total_qty_q = total_qty_q.filter(PlannedPurchase.item_id == int(item_id))
    if bucket_type in {"daily", "weekly"}:
        total_qty_q = total_qty_q.filter(PlannedPurchase.bucket_type == bucket_type)
    if date_from:
        total_qty_q = total_qty_q.filter(PlannedPurchase.bucket_date >= _to_date(date_from))
    if date_to:
        total_qty_q = total_qty_q.filter(PlannedPurchase.bucket_date <= _to_date(date_to))
    total_qty_val = float(total_qty_q.scalar() or 0.0)

    # Join к Item для денормализации и сортировки
    q = (
        db.query(
            PlannedPurchase,
            Item.item_name,
            Item.item_article,
            Item.unit,
            Unit.short_name,
            Unit.unit_name,
            Unit.unit_code,
        )
        .outerjoin(Item, PlannedPurchase.item_id == Item.item_id)
        .outerjoin(Unit, Item.unit == Unit.unit_ref1c)
        .filter(PlannedPurchase.run_id == run_id)
    )
    if item_id is not None:
        q = q.filter(PlannedPurchase.item_id == int(item_id))
    if bucket_type in {"daily", "weekly"}:
        q = q.filter(PlannedPurchase.bucket_type == bucket_type)
    if date_from:
        q = q.filter(PlannedPurchase.bucket_date >= _to_date(date_from))
    if date_to:
        q = q.filter(PlannedPurchase.bucket_date <= _to_date(date_to))

    sort_map = {
        "item_name": Item.item_name,
        "item_article": Item.item_article,
        "qty": PlannedPurchase.qty,
        "need_date": PlannedPurchase.need_date,
        "order_date": PlannedPurchase.order_date,
        "bucket_date": PlannedPurchase.bucket_date,
        "priority_index": PlannedPurchase.priority_index,
    }
    sb = (sort_by or "bucket_date").strip().lower()
    sd = (sort_dir or "asc").strip().lower()
    col = sort_map.get(sb, PlannedPurchase.bucket_date)
    dir_fn = desc if sd == "desc" else asc
    q = q.order_by(dir_fn(col), PlannedPurchase.purchase_id.asc())

    rows_joined = (
        q.offset(max(0, int(offset)))
         .limit(max(1, min(int(limit or 100), 1000)))
         .all()
    )

    data: List[Dict[str, Any]] = []
    for row in rows_joined:
        r, in_name, in_article, in_unit_guid, in_unit_short, in_unit_name, in_unit_code = row
        display_unit = in_unit_short or in_unit_name or in_unit_code or in_unit_guid
        data.append(
            {
                "purchase_id": int(r.purchase_id),
                "item_id": int(r.item_id),
                "item_name": in_name,
                "item_article": in_article,
                "unit": display_unit,
                "qty": float(r.qty or 0.0),
                "need_date": r.need_date.isoformat() if r.need_date else None,
                "order_date": r.order_date.isoformat() if r.order_date else None,
                "lead_time_days": int(r.lead_time_days),
                "priority_index": float(r.priority_index or 0.0) if r.priority_index is not None else None,
                "bucket_type": r.bucket_type,
                "bucket_date": r.bucket_date.isoformat() if r.bucket_date else None,
                "supplier_ref1c": r.supplier_ref1c,
            }
        )

    return {
        "rows": data,
        "total": int(total),
        "total_qty": float(total_qty_val),
        "limit": int(limit),
        "offset": int(offset),
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
    if bucket_type in {"daily", "weekly"}:
        q = q.filter(CapacityLoad.bucket_type == bucket_type)
    if date_from:
        q = q.filter(CapacityLoad.bucket_date >= _to_date(date_from))
    if date_to:
        q = q.filter(CapacityLoad.bucket_date <= _to_date(date_to))

    total = q.count()
    rows: List[CapacityLoad] = (
        q.order_by(CapacityLoad.bucket_date.asc(), CapacityLoad.area_id.asc())
        .offset(max(0, int(offset)))
        .limit(max(1, min(int(limit or 200), 5000)))
        .all()
    )
    data = [
        {
            "area_id": int(r.area_id),
            "bucket_type": r.bucket_type,
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


# --- Preview calculation (gross requirement + netting) ---

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


def _iso_week_friday(d: date) -> date:
    # ISO: Monday=1 .. Sunday=7
    wd = d.isoweekday()
    monday = d - timedelta(days=wd - 1)
    friday = monday + timedelta(days=4)
    return friday


def compute_gross_requirements(
    db: Session,
    horizon_days: Optional[int] = None,
    use_weekly: Optional[bool] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Step 1: Compute gross requirements (BOM expansion) in time buckets without netting or DB writes.
    - Buckets: daily (exact dates) and weekly (Friday of ISO week) according to policy.
    - Safety stock percent is applied to gross buckets as per policy.
    Returns: { meta, config, gross, stats }
    """
    # 1) Load active config and apply overrides (mirrors create_planning_run/compute_planning_preview)
    try:
        try:
            cfg_id, cfg = get_active_planning_config(db)
        except Exception:
            cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)
    except Exception:
        cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)

    overrides: Dict[str, Any] = {}
    if horizon_days is not None:
        overrides["planning_horizon_days"] = int(horizon_days)
    if use_weekly is not None:
        overrides.setdefault("weekly", {})
        overrides["weekly"]["enabled"] = bool(use_weekly)
    if config_overrides:
        overrides = _deep_merge(overrides, config_overrides)

    try:
        snapshot = _deep_merge(cfg, overrides)
    except Exception:
        snapshot = dict(cfg) if isinstance(cfg, dict) else dict(DEFAULT_PLANNING_CONFIG)

    horizon = int(snapshot.get("planning_horizon_days") or snapshot.get("mps_daily_horizon_days", 90))
    weekly_enabled = bool(snapshot.get("weekly", {}).get("enabled", True))
    ss_percent = float(snapshot.get("safety_stock_percent", 1) or 0.0)

    d0: date = date.today()
    dmax: date = d0 + timedelta(days=max(1, horizon) - 1)

    # 2) Fetch MPS daily entries in horizon (source of gross top-level demand)
    mps_rows: List[ProductionPlanEntry] = (
        db.query(ProductionPlanEntry)
        .filter(ProductionPlanEntry.date >= d0, ProductionPlanEntry.date <= dmax)
        .all()
    )

    # Determine Dcontig = max continuous day from d0 present in MPS
    present_days: Set[date] = {
        r.date.date() for r in mps_rows if float(r.planned_qty or 0.0) > 0.0
    }
    dcontig: Optional[date] = None
    cur = d0
    while cur <= dmax and cur in present_days:
        dcontig = cur
        cur = cur + timedelta(days=1)

    # 3) Build DefaultSpecification map and lazy components cache
    defaults: List[DefaultSpecification] = db.query(DefaultSpecification).all()
    default_spec_map: Dict[int, int] = {}
    for rec in defaults:
        try:
            default_spec_map[int(rec.item_id)] = int(rec.spec_id)
        except Exception:
            continue

    components_cache: Dict[int, List[SpecComponent]] = {}

    def get_components_for_spec(spec_id: int) -> List[SpecComponent]:
        if spec_id in components_cache:
            return components_cache[spec_id]
        comps = db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()
        components_cache[spec_id] = comps
        return comps

    # 4) Accumulators for gross buckets
    gross_daily: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))
    gross_weekly: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))

    def add_to_bucket(item_id: int, dt: date, qty: float, is_weekly: bool) -> None:
        if qty == 0.0:
            return
        if is_weekly:
            gross_weekly[item_id][dt] += qty
        else:
            gross_daily[item_id][dt] += qty

    # Recursive expansion with cycle guard
    def expand_bom(item_id: int, qty: float, is_weekly: bool, bucket_date: date, path: Set[int], depth: int = 0) -> None:
        if qty <= 0.0:
            return
        if depth > 200:
            return
        if item_id in path:
            return
        spec_id = default_spec_map.get(item_id)
        if not spec_id:
            return
        new_path = set(path)
        new_path.add(item_id)
        comps = get_components_for_spec(spec_id)
        if not comps:
            return
        for c in comps:
            try:
                child_id = int(c.item_id)
                comp_qty = float(c.quantity or 0.0)
            except Exception:
                continue
            child_qty = qty * comp_qty
            if child_qty <= 0.0:
                continue
            # Accumulate child demand in the same bucket
            add_to_bucket(child_id, bucket_date, child_qty, is_weekly)
            # Recurse
            expand_bom(child_id, child_qty, is_weekly, bucket_date, new_path, depth + 1)

    # 5) Fill gross buckets from MPS (top-level + BOM expansion)
    for r in mps_rows:
        root_qty = float(r.planned_qty or 0.0)
        if root_qty <= 0.0:
            continue
        day = r.date.date()
        is_week = False
        if weekly_enabled:
            # If Dcontig is None => no daily window; everything beyond d0 is weekly
            if dcontig is None:
                is_week = True
            else:
                is_week = day > dcontig
        # Resolve bucket date
        bucket_dt = _iso_week_friday(day) if is_week else day
        # Accumulate root (top-level) demand
        try:
            root_item_id = int(r.item_id)
        except Exception:
            continue
        add_to_bucket(root_item_id, bucket_dt, root_qty, is_week)
        # Expand into components within the same bucket
        expand_bom(root_item_id, root_qty, is_week, bucket_dt, path=set(), depth=0)

    # 6) Apply safety stock percentage to gross (per bucket)
    factor = 1.0 + (ss_percent / 100.0 if ss_percent else 0.0)
    if abs(factor - 1.0) > 1e-9:
        for iid, dmap in gross_daily.items():
            for dt in list(dmap.keys()):
                dmap[dt] = float(dmap[dt]) * factor
        for iid, wmap in gross_weekly.items():
            for dt in list(wmap.keys()):
                wmap[dt] = float(wmap[dt]) * factor

    # 7) Convert to serializable structures
    def serialize_bucket(bmap: DefaultDict[int, DefaultDict[date, float]]) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for iid, dtmap in bmap.items():
            out[str(int(iid))] = {dt.isoformat(): float(q) for dt, q in sorted(dtmap.items(), key=lambda x: x[0])}
        return out

    gross_ser = {"daily": serialize_bucket(gross_daily), "weekly": serialize_bucket(gross_weekly)}
    daily_bucket_count = sum(len(v) for v in gross_ser["daily"].values())
    weekly_bucket_count = sum(len(v) for v in gross_ser["weekly"].values())

    return {
        "meta": {
            "asOf": _read_last_stock_sync_at(),
            "d0": d0.isoformat(),
            "dmax": dmax.isoformat(),
            "dcontig": dcontig.isoformat() if dcontig else None,
        },
        "config": {
            "horizon_days": horizon,
            "weekly_enabled": weekly_enabled,
            "safety_stock_percent": ss_percent,
            "config_version_id": int(cfg_id),
        },
        "gross": gross_ser,
        "stats": {
            "items": len(set(gross_daily.keys()) | set(gross_weekly.keys())),
            "daily_buckets": int(daily_bucket_count),
            "weekly_buckets": int(weekly_bucket_count),
        },
    }
def compute_planning_preview(
    db: Session,
    horizon_days: Optional[int] = None,
    use_weekly: Optional[bool] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Compute gross requirements (BOM expansion) and net requirements (stock netting + safety stock)
    without writing anything to DB. Output buckets: daily (exact dates) and weekly (Friday of ISO week).
    """
    # 1) Load active config and apply overrides (same logic as create_planning_run)
    try:
        try:
            cfg_id, cfg = get_active_planning_config(db)
        except Exception:
            cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)
    except Exception:
        cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)
    overrides: Dict[str, Any] = {}
    if horizon_days is not None:
        overrides["planning_horizon_days"] = int(horizon_days)
    if use_weekly is not None:
        overrides.setdefault("weekly", {})
        overrides["weekly"]["enabled"] = bool(use_weekly)
    if config_overrides:
        overrides = _deep_merge(overrides, config_overrides)
    try:
        snapshot = _deep_merge(cfg, overrides)
    except Exception:
        snapshot = dict(cfg) if isinstance(cfg, dict) else dict(DEFAULT_PLANNING_CONFIG)

    horizon = int(snapshot.get("planning_horizon_days") or snapshot.get("mps_daily_horizon_days", 90))
    weekly_enabled = bool(snapshot.get("weekly", {}).get("enabled", True))
    ss_percent = float(snapshot.get("safety_stock_percent", 1) or 0.0)

    d0: date = date.today()
    dmax: date = d0 + timedelta(days=max(1, horizon) - 1)

    # 2) Fetch MPS daily entries in horizon (source of gross top-level demand)
    mps_rows: List[ProductionPlanEntry] = (
        db.query(ProductionPlanEntry)
        .filter(ProductionPlanEntry.date >= d0, ProductionPlanEntry.date <= dmax)
        .all()
    )

    # Determine Dcontig = max continuous day from d0 present in MPS
    present_days: Set[date] = {
        r.date.date() for r in mps_rows if float(r.planned_qty or 0.0) > 0.0
    }
    dcontig: Optional[date] = None
    cur = d0
    while cur <= dmax and cur in present_days:
        dcontig = cur
        cur = cur + timedelta(days=1)

    # 3) Build DefaultSpecification map and lazy components cache
    defaults: List[DefaultSpecification] = db.query(DefaultSpecification).all()
    default_spec_map: Dict[int, int] = {}
    for rec in defaults:
        try:
            default_spec_map[int(rec.item_id)] = int(rec.spec_id)
        except Exception:
            continue

    components_cache: Dict[int, List[SpecComponent]] = {}

    def get_components_for_spec(spec_id: int) -> List[SpecComponent]:
        if spec_id in components_cache:
            return components_cache[spec_id]
        comps = db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()
        components_cache[spec_id] = comps
        return comps

    # 4) Accumulators for gross buckets
    gross_daily: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))
    gross_weekly: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))

    def add_to_bucket(item_id: int, dt: date, qty: float, is_weekly: bool) -> None:
        if qty == 0.0:
            return
        if is_weekly:
            gross_weekly[item_id][dt] += qty
        else:
            gross_daily[item_id][dt] += qty

    # Recursive expansion with cycle guard
    def expand_bom(item_id: int, qty: float, is_weekly: bool, bucket_date: date, path: Set[int], depth: int = 0) -> None:
        if qty <= 0.0:
            return
        if depth > 200:
            return
        if item_id in path:
            return
        spec_id = default_spec_map.get(item_id)
        if not spec_id:
            return
        new_path = set(path)
        new_path.add(item_id)
        comps = get_components_for_spec(spec_id)
        if not comps:
            return
        for c in comps:
            try:
                child_id = int(c.item_id)
                comp_qty = float(c.quantity or 0.0)
            except Exception:
                continue
            child_qty = qty * comp_qty
            if child_qty <= 0.0:
                continue
            # Accumulate child demand in the same bucket
            add_to_bucket(child_id, bucket_date, child_qty, is_weekly)
            # Recurse
            expand_bom(child_id, child_qty, is_weekly, bucket_date, new_path, depth + 1)

    # 5) Fill gross buckets from MPS (top-level + BOM expansion)
    for r in mps_rows:
        root_qty = float(r.planned_qty or 0.0)
        if root_qty <= 0.0:
            continue
        day = r.date.date()
        is_week = False
        if weekly_enabled:
            # If Dcontig is None => no daily window; everything beyond d0 is weekly
            if dcontig is None:
                is_week = True
            else:
                is_week = day > dcontig
        # Resolve bucket date
        bucket_dt = _iso_week_friday(day) if is_week else day
        # Accumulate root (top-level) demand
        try:
            root_item_id = int(r.item_id)
        except Exception:
            continue
        add_to_bucket(root_item_id, bucket_dt, root_qty, is_week)
        # Expand into components within the same bucket
        expand_bom(root_item_id, root_qty, is_week, bucket_dt, path=set(), depth=0)

    # 6) Apply safety stock percentage to gross (per bucket)
    factor = 1.0 + (ss_percent / 100.0 if ss_percent else 0.0)
    if abs(factor - 1.0) > 1e-9:
        for iid, dmap in gross_daily.items():
            for dt in list(dmap.keys()):
                dmap[dt] = float(dmap[dt]) * factor
        for iid, wmap in gross_weekly.items():
            for dt in list(wmap.keys()):
                wmap[dt] = float(wmap[dt]) * factor

    # 7) Netting with stocks (WIP ignored)
    # Collect items involved
    item_ids: Set[int] = set(gross_daily.keys()) | set(gross_weekly.keys())
    if not item_ids:
        preview = {
            "meta": {
                "asOf": _read_last_stock_sync_at(),
                "d0": d0.isoformat(),
                "dmax": dmax.isoformat(),
                "dcontig": dcontig.isoformat() if dcontig else None,
            },
            "config": {
                "horizon_days": horizon,
                "weekly_enabled": weekly_enabled,
                "safety_stock_percent": ss_percent,
                "config_version_id": int(cfg_id),
            },
            "gross": {"daily": {}, "weekly": {}},
            "net": {"daily": {}, "weekly": {}},
            "stats": {"items": 0, "daily_buckets": 0, "weekly_buckets": 0},
        }
        return preview

    items: List[Item] = db.query(Item).filter(Item.item_id.in_(item_ids)).all()
    stock_by_item: Dict[int, float] = {int(x.item_id): float(x.stock_qty or 0.0) for x in items}

    net_daily: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))
    net_weekly: DefaultDict[int, DefaultDict[date, float]] = defaultdict(lambda: defaultdict(float))

    for iid in item_ids:
        remaining = float(stock_by_item.get(iid, 0.0) or 0.0)
        # Build time-ordered buckets
        buckets: List[Tuple[str, date, float]] = []
        for dt, q in gross_daily.get(iid, {}).items():
            buckets.append(("daily", dt, float(q or 0.0)))
        for dt, q in gross_weekly.get(iid, {}).items():
            buckets.append(("weekly", dt, float(q or 0.0)))
        buckets.sort(key=lambda x: x[1])  # sort by date ascending

        for btype, dt, q in buckets:
            if q <= 0.0:
                continue
            if remaining >= q:
                net_q = 0.0
                remaining -= q
            else:
                net_q = q - max(remaining, 0.0)
                remaining = 0.0
            if net_q > 0.0:
                if btype == "daily":
                    net_daily[iid][dt] += net_q
                else:
                    net_weekly[iid][dt] += net_q

    # 8) Convert to serializable structures
    def serialize_bucket(bmap: DefaultDict[int, DefaultDict[date, float]]) -> Dict[str, Dict[str, float]]:
        out: Dict[str, Dict[str, float]] = {}
        for iid, dtmap in bmap.items():
            out[str(int(iid))] = {dt.isoformat(): float(q) for dt, q in sorted(dtmap.items(), key=lambda x: x[0])}
        return out

    gross_ser = {"daily": serialize_bucket(gross_daily), "weekly": serialize_bucket(gross_weekly)}
    net_ser = {"daily": serialize_bucket(net_daily), "weekly": serialize_bucket(net_weekly)}

    daily_bucket_count = sum(len(v) for v in gross_ser["daily"].values())
    weekly_bucket_count = sum(len(v) for v in gross_ser["weekly"].values())

    return {
        "meta": {
            "asOf": _read_last_stock_sync_at(),
            "d0": d0.isoformat(),
            "dmax": dmax.isoformat(),
            "dcontig": dcontig.isoformat() if dcontig else None,
        },
        "config": {
            "horizon_days": horizon,
            "weekly_enabled": weekly_enabled,
            "safety_stock_percent": ss_percent,
            "config_version_id": int(cfg_id),
        },
        "gross": gross_ser,
        "net": net_ser,
        "stats": {
            "items": len(item_ids),
            "daily_buckets": int(daily_bucket_count),
            "weekly_buckets": int(weekly_bucket_count),
        },
    }
# === Planning run execution (saving results to DB) ===

def _to_date(s: str) -> date:
    try:
        return date.fromisoformat(str(s))
    except Exception:
        # Best effort: try first 10 chars
        return date.fromisoformat(str(s)[:10])


def _previous_workday(d: date) -> date:
    # Simplified 5/2 calendar: Sat/Sun roll back to previous Friday
    wd = d.weekday()  # Mon=0..Sun=6
    if wd == 5:  # Saturday
        return d - timedelta(days=1)
    if wd == 6:  # Sunday
        return d - timedelta(days=2)
    return d


def _classify_flow(method: Optional[str]) -> str:
    """
    Return 'purchase' or 'production' based on replenishment_method text.
    Heuristic: contains 'покуп' or 'purchase' -> purchase, else production.
    """
    m = (method or "").strip().lower()
    if "покуп" in m or "purchase" in m or "закуп" in m:
        return "purchase"
    return "production"


def _load_active_config_snapshot(
    db: Session,
    horizon_days: Optional[int],
    use_weekly: Optional[bool],
    config_overrides: Optional[Dict[str, Any]],
) -> Tuple[int, Dict[str, Any]]:
    try:
        try:
            cfg_id, cfg = get_active_planning_config(db)
        except Exception:
            cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)
    except Exception:
        cfg_id, cfg = 0, dict(DEFAULT_PLANNING_CONFIG)

    overrides: Dict[str, Any] = {}
    if horizon_days is not None:
        overrides["planning_horizon_days"] = int(horizon_days)
    if use_weekly is not None:
        overrides.setdefault("weekly", {})
        overrides["weekly"]["enabled"] = bool(use_weekly)
    if config_overrides:
        overrides = _deep_merge(overrides, config_overrides)
    try:
        snapshot = _deep_merge(cfg, overrides)
    except Exception:
        snapshot = dict(cfg) if isinstance(cfg, dict) else dict(DEFAULT_PLANNING_CONFIG)
    return int(cfg_id), snapshot


def _normalize_lot_qty(qty: float, min_qty: Optional[float], multiple: Optional[float], rounding: str) -> float:
    """
    Normalize quantity according to lot sizing rules:
      - enforce minimum quantity (min_qty/MOQ) if provided (>0)
      - enforce multiple (batch multiple) using rounding policy: ceil|floor|round
    Returns non-negative float. If result becomes 0 or negative, returns 0.0
    """
    try:
        q = float(qty or 0.0)
    except Exception:
        q = 0.0
    if q <= 0.0:
        return 0.0

    # Ensure minimum
    try:
        mn = float(min_qty) if (min_qty is not None) else None
    except Exception:
        mn = None
    if mn is not None and mn > 0.0 and q < mn:
        q = mn

    # Apply multiple with rounding policy
    try:
        m = float(multiple) if (multiple is not None) else None
    except Exception:
        m = None
    mode = (rounding or "ceil").strip().lower()
    if m is not None and m > 0.0:
        ratio = q / m
        if mode == "floor":
            q = math.floor(ratio) * m
        elif mode == "round":
            q = round(ratio) * m
        else:
            # default: ceil
            q = math.ceil(ratio) * m

        # After rounding to multiple, still ensure minimum if defined
        if mn is not None and mn > 0.0 and q < mn:
            q = math.ceil(mn / m) * m

    if not (q > 0.0):
        return 0.0
    return float(q)


def _normalize_qty_for_production(snapshot: Dict[str, Any], qty: float) -> float:
    """
    Production lot sizing based on snapshot['production']['lot_sizing'].
    Supported keys: min_batch, multiple, rounding (ceil|floor|round).
    Defaults: min_batch=1, multiple=1, rounding='ceil'
    """
    lot_cfg: Dict[str, Any] = {}
    if isinstance(snapshot, dict):
        lot_cfg = (snapshot.get("production") or {}).get("lot_sizing") or {}
    try:
        min_batch = float(lot_cfg.get("min_batch", 1) or 1)
    except Exception:
        min_batch = 1.0
    try:
        multiple = float(lot_cfg.get("multiple", 1) or 1)
    except Exception:
        multiple = 1.0
    rounding = str(lot_cfg.get("rounding", "ceil") or "ceil").strip().lower()
    return _normalize_lot_qty(qty, min_batch, multiple, rounding)


def _normalize_qty_for_procurement(snapshot: Dict[str, Any], item: Optional[Item], qty: float) -> float:
    """
    Procurement lot sizing based on snapshot['procurement']['lot_sizing'].
    Supported keys: moq, moq_source, multiple, rounding (ceil|floor|round).
    - moq_source=item_card_or_1 tries item.moq, then falls back to 1 if not present.
    - If 'moq' key is provided explicitly, it takes precedence.
    Defaults: moq_source=item_card_or_1, multiple=1, rounding='ceil'
    """
    lot_cfg: Dict[str, Any] = {}
    if isinstance(snapshot, dict):
        lot_cfg = (snapshot.get("procurement") or {}).get("lot_sizing") or {}

    # Determine minimum quantity (MOQ)
    min_qty: Optional[float] = None
    if "moq" in lot_cfg:
        try:
            min_qty = float(lot_cfg.get("moq"))
        except Exception:
            min_qty = None

    moq_source = str(lot_cfg.get("moq_source", "item_card_or_1") or "item_card_or_1").strip().lower()
    if min_qty is None:
        if moq_source == "item_card_or_1":
            # Try to read from item card if present
            try:
                item_moq = getattr(item, "moq", None)
                if item_moq is not None:
                    min_qty = float(item_moq)
            except Exception:
                pass
            if min_qty is None:
                min_qty = 1.0
        else:
            # Unknown source -> fallback to 1
            min_qty = 1.0

    try:
        multiple = float(lot_cfg.get("multiple", 1) or 1)
    except Exception:
        multiple = 1.0
    rounding = str(lot_cfg.get("rounding", "ceil") or "ceil").strip().lower()
    return _normalize_lot_qty(qty, min_qty, multiple, rounding)
def run_planning_run(
    db: Session,
    horizon_days: Optional[int] = None,
    use_weekly: Optional[bool] = None,
    config_overrides: Optional[Dict[str, Any]] = None,
    started_by: Optional[str] = None,
) -> int:
    """
    Execute planning run:
      - Create PlanningRun with status RUNNING and config snapshot
      - Compute preview (gross + net)
      - Classify flows (production vs purchase)
      - Persist planned_order / planned_purchase
      - Build PlannedOrderStage (stage detection by children, norm_hours by spec ops, map to area)
      - Aggregate CapacityLoad (hours_planned/available/overload)
      - Compute priority_index for orders and purchases
      - Record PeggingLink (one-level child→parent by bucket)
      - Finalize run with SUCCESS and KPI/warnings
    """
    # 1) Snapshot config and create RUNNING run
    cfg_id, snapshot = _load_active_config_snapshot(db, horizon_days, use_weekly, config_overrides)
    run = PlanningRun(
        status="RUNNING",
        started_by=started_by or "api",
        horizon_days=int(snapshot.get("planning_horizon_days") or snapshot.get("mps_daily_horizon_days", 90)),
        use_weekly=bool(snapshot.get("weekly", {}).get("enabled", True)),
        config_version_id=cfg_id,
        config_snapshot=snapshot,
        warnings=[],
        kpi={},
        started_at=datetime.utcnow(),
        finished_at=None,
    )
    db.add(run)
    db.flush()
    run_id = int(run.run_id)

    warnings: List[Dict[str, Any]] = []

    try:
        # 2) Compute preview to obtain gross + net by buckets
        preview = compute_planning_preview(
            db=db,
            horizon_days=horizon_days,
            use_weekly=use_weekly,
            config_overrides=config_overrides or {},
        )
        net = (preview or {}).get("net") or {}
        net_daily: Dict[str, Dict[str, float]] = net.get("daily") or {}
        net_weekly: Dict[str, Dict[str, float]] = net.get("weekly") or {}

        if not net_daily and not net_weekly:
            # Fallback: if net not produced, use gross
            gross = (preview or {}).get("gross") or {}
            net_daily = gross.get("daily") or {}
            net_weekly = gross.get("weekly") or {}
            warnings.append({"code": "PREVIEW_NO_NET", "msg": "Net requirements missing; used gross as fallback"})

        # 3) Load items dictionary for all involved item_ids
        all_item_ids: Set[int] = set()
        for iid in net_daily.keys():
            try:
                all_item_ids.add(int(iid))
            except Exception:
                continue
        for iid in net_weekly.keys():
            try:
                all_item_ids.add(int(iid))
            except Exception:
                continue

        items: List[Item] = []
        if all_item_ids:
            items = db.query(Item).filter(Item.item_id.in_(all_item_ids)).all()
        item_by_id: Dict[int, Item] = {int(x.item_id): x for x in items}

        # 4) Persist planned purchases and orders (collect created rows)
        created_orders: List[PlannedOrder] = []
        created_purchases: List[PlannedPurchase] = []

        default_lt = int(snapshot.get("procurement", {}).get("default_lead_time_days", 30) or 30)

        def _add_purchase(iid: int, need_dt: date, q: float, bucket_type: str):
            item = item_by_id.get(iid)
            lt = default_lt
            try:
                lt = max(default_lt, int(getattr(item, "replenishment_time", 0) or 0))
            except Exception:
                lt = default_lt

            # Lot sizing for procurement
            qn = _normalize_qty_for_procurement(snapshot, item, q)
            if qn <= 0.0:
                return

            order_dt = _previous_workday(need_dt - timedelta(days=lt))
            rec = PlannedPurchase(
                run_id=run_id,
                item_id=iid,
                qty=qn,
                need_date=need_dt,
                order_date=order_dt,
                lead_time_days=int(lt),
                priority_index=None,
                bucket_type=bucket_type,
                bucket_date=need_dt,
                supplier_ref1c=None,
            )
            db.add(rec)
            created_purchases.append(rec)

        def _add_order(iid: int, need_dt: date, q: float, bucket_type: str):
            # Lot sizing for production
            qn = _normalize_qty_for_production(snapshot, q)
            if qn <= 0.0:
                return

            rec = PlannedOrder(
                run_id=run_id,
                item_id=iid,
                qty=qn,
                need_date=need_dt,
                start_date=None,
                finish_date=None,
                route_ref=None,
                priority_index=None,
                bucket_type=bucket_type,
                bucket_date=need_dt,
                demand_ref=None,
                demand_date=need_dt,
            )
            db.add(rec)
            created_orders.append(rec)

        # Daily buckets
        for iid_str, buckets in net_daily.items():
            try:
                iid = int(iid_str)
            except Exception:
                continue
            item = item_by_id.get(iid)
            flow = _classify_flow(getattr(item, "replenishment_method", None)) if item else "production"

            for dt_str, qty in (buckets or {}).items():
                try:
                    q = float(qty or 0.0)
                except Exception:
                    q = 0.0
                if q <= 0.0:
                    continue
                need_dt = _to_date(dt_str)
                if flow == "purchase":
                    _add_purchase(iid, need_dt, q, "daily")
                else:
                    _add_order(iid, need_dt, q, "daily")

        # Weekly buckets
        for iid_str, buckets in net_weekly.items():
            try:
                iid = int(iid_str)
            except Exception:
                continue
            item = item_by_id.get(iid)
            flow = _classify_flow(getattr(item, "replenishment_method", None)) if item else "production"

            for dt_str, qty in (buckets or {}).items():
                try:
                    q = float(qty or 0.0)
                except Exception:
                    q = 0.0
                if q <= 0.0:
                    continue
                need_dt = _to_date(dt_str)
                if flow == "purchase":
                    _add_purchase(iid, need_dt, q, "weekly")
                else:
                    _add_order(iid, need_dt, q, "weekly")

        # Flush to get primary keys for orders/purchases
        db.flush()

        # 5) Build stage detection and norm caches based on default specs
        defaults: List[DefaultSpecification] = db.query(DefaultSpecification).all()
        default_spec_map: Dict[int, int] = {}
        for rec in defaults:
            try:
                default_spec_map[int(rec.item_id)] = int(rec.spec_id)
            except Exception:
                continue

        # Load specifications to get production kind info
        specifications: List[Specification] = db.query(Specification).all()
        spec_by_id: Dict[int, Specification] = {int(spec.spec_id): spec for spec in specifications if spec.spec_id}

        components_cache: Dict[int, List[SpecComponent]] = {}
        operations_cache: Dict[int, List[SpecOperation]] = {}

        def get_components_for_spec(spec_id: int) -> List[SpecComponent]:
            if spec_id in components_cache:
                return components_cache[spec_id]
            comps = db.query(SpecComponent).filter(SpecComponent.spec_id == spec_id).all()
            components_cache[spec_id] = comps
            return comps

        def get_operations_for_spec(spec_id: int) -> List[SpecOperation]:
            if spec_id in operations_cache:
                return operations_cache[spec_id]
            ops = db.query(SpecOperation).filter(SpecOperation.spec_id == spec_id).all()
            operations_cache[spec_id] = ops
            return ops

        # Resources list
        resources: List[ProductionResource] = db.query(ProductionResource).all()

        # Resources and production kind mapping (new logic)
        resource_production_kinds: List[ResourceProductionKind] = db.query(ResourceProductionKind).all()
        production_kinds_by_resource: Dict[int, Set[int]] = {}
        for rpk in resource_production_kinds:
            production_kinds_by_resource.setdefault(int(rpk.resource_id), set()).add(int(rpk.production_kind_id))

        all_stages: List[ProductionStage] = db.query(ProductionStage).all()
        stage_name_map: Dict[int, str] = {int(s.stage_id): str(s.stage_name or "") for s in all_stages}

        # All production kinds for name lookup
        all_production_kinds: List[ProductionKind] = db.query(ProductionKind).all()
        production_kind_name_map: Dict[int, str] = {int(pk.id): str(pk.name or "") for pk in all_production_kinds}

        # Item-level cached analysis: stage_id, production_kind_id and norm_hours (per unit)
        item_stage_cache: Dict[int, Tuple[Optional[int], Optional[str]]] = {}
        item_norm_cache: Dict[int, float] = {}
        item_production_kind_cache: Dict[int, Optional[int]] = {}

        def analyze_parent_item(item_id: int) -> Tuple[Optional[int], Optional[str], float, Optional[int]]:
            """
            Delegates stage determination and norm-hours calculation to shared helper,
            aligned with 'Распределение этапов' page logic.
            Returns (stage_id or None, reason_if_ambiguous, norm_hours_single, production_kind_id or None).

            Domain overrides for painting:
            - If operations' majority stage corresponds to painting ('покраска'/'paint'), force painting.
            - If item looks like a painted part by name/article/code (e.g., contains color markers or '-SP' suffix),
              force painting.
            """
            # cache hit
            if item_id in item_stage_cache and item_id in item_norm_cache and item_id in item_production_kind_cache:
                st, reason = item_stage_cache[item_id]
                return st, reason, item_norm_cache[item_id], item_production_kind_cache[item_id]

            stg_id, rsn, nh = determine_parent_stage_and_norm(
                default_spec_map=default_spec_map,
                get_components_for_spec=get_components_for_spec,
                get_operations_for_spec=get_operations_for_spec,
                item_id=int(item_id),
            )

            # Get production kind from specification
            spec_id = default_spec_map.get(int(item_id))
            production_kind_id = None
            if spec_id:
                spec = spec_by_id.get(int(spec_id))
                if spec:
                    production_kind_id = spec.production_kind_id

            # Helpers
            def _painting_stage_id() -> Optional[int]:
                try:
                    for sid, nm in stage_name_map.items():
                        nml = (nm or "").strip().lower()
                        if "покраск" in nml or "paint" in nml:
                            return int(sid)
                except Exception:
                    return None
                return None

            def _is_painting_stage(sid: Optional[int]) -> bool:
                if sid is None:
                    return False
                name = (stage_name_map.get(int(sid)) or "").strip().lower()
                return ("покраск" in name) or ("paint" in name)

            def _looks_painted() -> bool:
                it = item_by_id.get(int(item_id))
                if not it:
                    return False
                txt = " ".join([
                    str(getattr(it, "item_name", "") or ""),
                    str(getattr(it, "item_article", "") or ""),
                    str(getattr(it, "item_code", "") or ""),
                ]).lower()
                # Heuristics: color adjectives and explicit paint keywords/suffixes
                color_keywords = [
                    "красн", "черн", "бел", "син", "голуб", "зел", "сер", "оранж",
                    "желт", "жёлт", "фиолет", "бордов", "слонов", "покраск", "покраш"
                ]
                if any(k in txt for k in color_keywords):
                    return True
                # Code suffixes like -SP, GSP
                code = str(getattr(it, "item_code", "") or "")
                if re.search(r"(-|_)?(g?sp)\b", code.lower()):
                    return True
                article = str(getattr(it, "item_article", "") or "")
                if re.search(r"(-|_)?(g?sp)\b", article.lower()):
                    return True
                return False

            # Painting override: from operations majority
            # Apply only if production_kind_id is not defined (to maintain priority of production kinds)
            if production_kind_id is None:
                try:
                    spec_id_local = default_spec_map.get(int(item_id))
                    op_major: Optional[int] = None
                    if spec_id_local:
                        ops_local = get_operations_for_spec(int(spec_id_local)) or []
                        from collections import defaultdict as _dd
                        op_counts: Dict[int, int] = _dd(int)
                        for op in ops_local:
                            sid = getattr(op, "stage_id", None)
                            if sid is None:
                                continue
                            try:
                                op_counts[int(sid)] += 1
                            except Exception:
                                continue
                        if op_counts:
                            max_cnt = max(op_counts.values())
                            top = [sid for sid, cnt in op_counts.items() if cnt == max_cnt]
                            if len(top) == 1:
                                op_major = int(top[0])

                    # Apply only when base stage is not determined yet
                    if stg_id is None and op_major is not None and _is_painting_stage(op_major):
                        stg_id = int(op_major)
                        rsn = "FORCE_PAINTING_FROM_OPERATIONS"
                except Exception:
                    # fail-safe: ignore override on any error
                    pass

                # Painting override: by item attributes (name/article/code) — behind config flag and only when stage is not defined
                try:
                    enable_by_name = bool(((snapshot.get("production") or {}).get("toggles") or {}).get("force_painting_by_name", False))
                    if enable_by_name and stg_id is None and _looks_painted():
                        paint_sid = _painting_stage_id()
                        if paint_sid is not None:
                            stg_id = int(paint_sid)
                            rsn = "FORCE_PAINTING_BY_NAME"
                except Exception:
                    pass

            item_stage_cache[item_id] = (stg_id, rsn)
            item_norm_cache[item_id] = float(nh or 0.0)
            item_production_kind_cache[item_id] = production_kind_id
            return stg_id, rsn, float(nh or 0.0), production_kind_id

        def pick_area_for_production_kind(production_kind_id: int) -> Optional[int]:
            """Pick area based on production kind mapping"""
            candidates = [rid for rid, pkset in production_kinds_by_resource.items() if production_kind_id in pkset]
            if not candidates:
                return None
            if len(candidates) == 1:
                return candidates[0]
            # Heuristic: return first candidate sorted by id
            return sorted(candidates)[0]

        # Stage-based area mapping removed (migration to production kinds)

        # 6) Backward scheduling of stages by days with resource capacity
        stages_created = 0

        # Resource dictionaries and helpers
        res_by_id: Dict[int, ProductionResource] = {int(r.resource_id): r for r in resources}

        def _is_workday(d: date) -> bool:
            # Mon..Fri are workdays
            return d.weekday() <= 4

        def _day_available_hours(res: Optional[ProductionResource], d: date) -> float:
            if not _is_workday(d) or res is None:
                return 0.0
            daily_hours = float(getattr(res, "daily_work_hours", 8.0) or 8.0)
            power_coeff = float(getattr(res, "capacity", 1.0) or 1.0)
            return daily_hours * power_coeff

        # Accumulator of planned hours per (area_id, day)
        capacity_usage_daily: DefaultDict[Tuple[int, date], float] = defaultdict(float)

        def _pick_area_for_day(stage_id_val: int, production_kind_id_val: Optional[int], d: date) -> Optional[int]:
            # Choose candidate resource with maximum free capacity on date d
            # Use ONLY production kind mapping (no stage fallback per migration plan)
            candidates = []
            if production_kind_id_val is not None:
                candidates = [rid for rid, pkset in production_kinds_by_resource.items() if production_kind_id_val in pkset]
            if not candidates:
                return None
            best_rid: Optional[int] = None
            best_free: float = -1.0
            for rid in candidates:
                res = res_by_id.get(int(rid))
                avail = _day_available_hours(res, d)
                used = float(capacity_usage_daily.get((int(rid), d), 0.0))
                free = max(0.0, avail - used)
                if free > best_free:
                    best_free = free
                    best_rid = int(rid)
            # Even if all candidates have zero free capacity, return a candidate to allow placement/overflow accounting
            return best_rid if best_rid is not None else int(sorted(candidates)[0])

        d0: date = date.today()

        # Track missing mappings summary (production kind primary, stage kept for analytics/backward compatibility)
        missing_area_pk_counts: DefaultDict[int, int] = defaultdict(int)
        missing_area_stage_counts: DefaultDict[int, int] = defaultdict(int)

        for order in created_orders:
            item_id = int(order.item_id)
            qty = float(order.qty or 0.0)
            stage_id, reason, norm_single, production_kind_id = analyze_parent_item(item_id)
            if stage_id is None and production_kind_id is None:
                if reason in {"NO_CHILD_STAGE", "MIXED_CHILD_STAGES"}:
                    warnings.append(
                        {
                            "code": reason,
                            "msg": f"Stage cannot be determined for item_id={item_id}",
                            "item_id": item_id,
                        }
                    )
                # Skip if both stage and production kind are undefined
                continue

            # Pick candidate areas based on production kind first, then stage as fallback
            candidate_res_ids = []
            if production_kind_id is not None:
                candidate_res_ids = [rid for rid, pkset in production_kinds_by_resource.items() if production_kind_id in pkset]
            # No stage-based fallback per migration plan
            if not candidate_res_ids:
                if production_kind_id is not None:
                    # Enrich diagnostic: item + spec + production kind name
                    try:
                        item_rec = item_by_id.get(int(item_id))
                    except Exception:
                        item_rec = None
                    try:
                        spec_id_local = default_spec_map.get(int(item_id))
                    except Exception:
                        spec_id_local = None
                    spec_ref1c_local = None
                    spec_code_local = None
                    spec_name_local = None
                    if spec_id_local is not None:
                        try:
                            spec_local = spec_by_id.get(int(spec_id_local))
                            if spec_local is not None:
                                spec_ref1c_local = getattr(spec_local, "spec_ref1c", None)
                                spec_code_local = getattr(spec_local, "spec_code", None)
                                spec_name_local = getattr(spec_local, "spec_name", None)
                        except Exception:
                            spec_ref1c_local = None
                            spec_code_local = None
                            spec_name_local = None
                    pk_name = None
                    try:
                        pk_name = production_kind_name_map.get(int(production_kind_id))
                    except Exception:
                        pk_name = None

                    warnings.append(
                        {
                            "code": "NO_AREA_FOR_PRODUCTION_KIND",
                            "msg": f"No resource area for production_kind_id={int(production_kind_id)}",
                            "production_kind_id": int(production_kind_id),
                            "production_kind_name": pk_name,
                            "item_id": int(item_id),
                            "item_code": getattr(item_rec, "item_code", None) if item_rec is not None else None,
                            "item_name": getattr(item_rec, "item_name", None) if item_rec is not None else None,
                            "item_article": getattr(item_rec, "item_article", None) if item_rec is not None else None,
                            "root_item_id": int(item_id),
                            "root_item_code": getattr(item_rec, "item_code", None) if item_rec is not None else None,
                            "root_item_name": getattr(item_rec, "item_name", None) if item_rec is not None else None,
                            "root_item_article": getattr(item_rec, "item_article", None) if item_rec is not None else None,
                            "spec_code": spec_code_local,
                            "spec_name": spec_name_local,
                            "spec_id": int(spec_id_local) if spec_id_local is not None else None,
                            "spec_ref1c": spec_ref1c_local,
                        }
                    )
                    missing_area_pk_counts[int(production_kind_id)] += 1
                else:
                    warnings.append(
                        {
                            "code": "NO_PRODUCTION_KIND",
                            "msg": f"No production kind for item_id={int(item_id)}",
                            "item_id": int(item_id),
                        }
                    )
                continue

            total_hours = float(norm_single or 0.0) * float(qty or 0.0)

            # Zero-norm safeguard:
            # If total norm-hours is <= 0, we still create a stage slice with hours=0
            # so the order is grouped by stage/area in UI (instead of falling into '—').
            if total_hours <= 1e-9:
                sel_area_id = None
                if production_kind_id is not None:
                    sel_area_id = pick_area_for_production_kind(int(production_kind_id))
                if sel_area_id is None:
                    # Fallback: choose any mapped resource for stability (based on already computed candidates)
                    sel_area_id = int(sorted(candidate_res_ids)[0]) if candidate_res_ids else None
                if sel_area_id is not None:
                    pos = PlannedOrderStage(
                        run_id=run_id,
                        order_id=int(order.order_id),
                        stage_id=int(stage_id) if stage_id is not None else None,  # Stage still used for operations
                        area_id=int(sel_area_id),
                        bucket_type="daily",
                        bucket_date=order.need_date,
                        hours=0.0,
                    )
                    db.add(pos)
                    stages_created += 1
                    # Do not affect capacity usage; set order dates for consistency
                    order.start_date = order.need_date
                    order.finish_date = order.need_date
                else:
                    # Enrich zero-norm diagnostic with item/spec/pk info
                    try:
                        item_rec_zn = item_by_id.get(int(order.item_id))
                    except Exception:
                        item_rec_zn = None
                    try:
                        spec_id_zn = default_spec_map.get(int(order.item_id))
                    except Exception:
                        spec_id_zn = None
                    spec_ref1c_zn = None
                    spec_code_zn = None
                    spec_name_zn = None
                    if spec_id_zn is not None:
                        try:
                            spec_zn = spec_by_id.get(int(spec_id_zn))
                            if spec_zn is not None:
                                spec_ref1c_zn = getattr(spec_zn, "spec_ref1c", None)
                                spec_code_zn = getattr(spec_zn, "spec_code", None)
                                spec_name_zn = getattr(spec_zn, "spec_name", None)
                        except Exception:
                            spec_ref1c_zn = None
                            spec_code_zn = None
                            spec_name_zn = None
                    pk_name_zn = None
                    if production_kind_id is not None:
                        try:
                            pk_name_zn = production_kind_name_map.get(int(production_kind_id))
                        except Exception:
                            pk_name_zn = None

                    warnings.append(
                        {
                            "code": "NO_AREA_FOR_PRODUCTION_KIND_ZERO_NORM",
                            "msg": f"No resource area resolved for zero-norm production_kind_id={int(production_kind_id) if production_kind_id is not None else 'N/A'}",
                            "production_kind_id": int(production_kind_id) if production_kind_id is not None else None,
                            "production_kind_name": pk_name_zn,
                            "order_id": int(order.order_id),
                            "item_id": int(order.item_id),
                            "item_code": getattr(item_rec_zn, "item_code", None) if item_rec_zn is not None else None,
                            "item_name": getattr(item_rec_zn, "item_name", None) if item_rec_zn is not None else None,
                            "item_article": getattr(item_rec_zn, "item_article", None) if item_rec_zn is not None else None,
                            "root_item_id": int(order.item_id),
                            "root_item_code": getattr(item_rec_zn, "item_code", None) if item_rec_zn is not None else None,
                            "root_item_name": getattr(item_rec_zn, "item_name", None) if item_rec_zn is not None else None,
                            "root_item_article": getattr(item_rec_zn, "item_article", None) if item_rec_zn is not None else None,
                            "spec_code": spec_code_zn,
                            "spec_name": spec_name_zn,
                            "spec_id": int(spec_id_zn) if spec_id_zn is not None else None,
                            "spec_ref1c": spec_ref1c_zn,
                        }
                    )
                # Skip regular scheduling in zero-norm case
                continue

            remaining = total_hours
            used_dates: List[date] = []

            cur = order.need_date  # start scheduling backward from need date

            # Backward allocation within horizon [d0..need_date], dynamically picking area with max free capacity
            while remaining > 1e-6 and cur >= d0:
                if not _is_workday(cur):
                    cur = cur - timedelta(days=1)
                    continue

                sel_area_id = _pick_area_for_day(int(stage_id) if stage_id is not None else None, production_kind_id, cur)
                if sel_area_id is None:
                    # no area mapping at all (should have been caught above) – skip day
                    cur = cur - timedelta(days=1)
                    continue

                key = (int(sel_area_id), cur)
                res = res_by_id.get(int(sel_area_id))
                avail = _day_available_hours(res, cur)
                used = float(capacity_usage_daily.get(key, 0.0))
                free = max(0.0, avail - used)
                if free <= 1e-9:
                    cur = cur - timedelta(days=1)
                    continue

                place = min(remaining, free)
                if place <= 0.0:
                    cur = cur - timedelta(days=1)
                    continue

                # Create daily stage slice for this order
                pos = PlannedOrderStage(
                    run_id=run_id,
                    order_id=int(order.order_id),
                    stage_id=int(stage_id) if stage_id is not None else None,
                    area_id=int(sel_area_id),
                    bucket_type="daily",
                    bucket_date=cur,
                    hours=float(place),
                )
                db.add(pos)
                stages_created += 1

                capacity_usage_daily[key] = used + float(place)
                used_dates.append(cur)
                remaining -= float(place)

                if remaining > 1e-6:
                    cur = cur - timedelta(days=1)

            # If some hours remain, place them to earliest workday at/ before d0 (allow overload) and warn
            fallback_used_date: Optional[date] = None
            if remaining > 1e-6:
                fb = d0
                # find workday at/ before d0 (max 7-day lookback as safety)
                tries = 7
                while tries > 0 and not _is_workday(fb):
                    fb = fb - timedelta(days=1)
                    tries -= 1

                sel_area_id_fb = _pick_area_for_day(int(stage_id) if stage_id is not None else None, production_kind_id, fb) or int(sorted(candidate_res_ids)[0])

                pos = PlannedOrderStage(
                    run_id=run_id,
                    order_id=int(order.order_id),
                    stage_id=int(stage_id) if stage_id is not None else None,
                    area_id=int(sel_area_id_fb),
                    bucket_type="daily",
                    bucket_date=fb,
                    hours=float(remaining),
                )
                db.add(pos)
                stages_created += 1
                capacity_usage_daily[(int(sel_area_id_fb), fb)] += float(remaining)
                fallback_used_date = fb
                warnings.append(
                    {
                        "code": "SCHED_OVERFLOW",
                        "msg": f"Order {int(order.order_id)} overflow: scheduled residual {float(remaining):.3f}h on {fb.isoformat()}",
                        "order_id": int(order.order_id),
                        "residual_hours": float(remaining),
                        "date": fb.isoformat(),
                    }
                )
                remaining = 0.0

            # Set start/finish dates on order
            if used_dates or fallback_used_date:
                all_dates = list(used_dates)
                if fallback_used_date:
                    all_dates.append(fallback_used_date)
                order.start_date = min(all_dates)
                order.finish_date = max(all_dates)

        db.flush()

        # 7) Aggregate CapacityLoad for daily and weekly buckets
        def _iso_friday(d: date) -> date:
            return _iso_week_friday(d)

        overload_total = 0.0
        overloaded_buckets = 0

        # Daily capacity load
        weekly_acc: DefaultDict[Tuple[int, date], float] = defaultdict(float)  # (area_id, friday) -> planned hours

        for (area_id, day), hours_planned in capacity_usage_daily.items():
            res = res_by_id.get(int(area_id))
            hours_available = _day_available_hours(res, day)
            overload = max(0.0, float(hours_planned or 0.0) - float(hours_available or 0.0))

            cap_d = CapacityLoad(
                run_id=run_id,
                area_id=int(area_id),
                bucket_type="daily",
                bucket_date=day,
                hours_planned=float(hours_planned or 0.0),
                hours_available=float(hours_available or 0.0),
                overload_hours=float(overload or 0.0),
            )
            db.add(cap_d)

            overload_total += float(overload or 0.0)
            if float(overload or 0.0) > 0.0:
                overloaded_buckets += 1

            # accumulate for weekly
            friday = _iso_friday(day)
            weekly_acc[(int(area_id), friday)] += float(hours_planned or 0.0)

        # Weekly capacity load (5/2 calendar → 5 workdays per week)
        for (area_id, friday), hours_planned in weekly_acc.items():
            res = res_by_id.get(int(area_id))
            daily_hours = float(getattr(res, "daily_work_hours", 8.0) or 8.0)
            power_coeff = float(getattr(res, "capacity", 1.0) or 1.0)
            hours_available = daily_hours * 5.0 * power_coeff
            overload = max(0.0, float(hours_planned or 0.0) - float(hours_available or 0.0))

            cap_w = CapacityLoad(
                run_id=run_id,
                area_id=int(area_id),
                bucket_type="weekly",
                bucket_date=friday,
                hours_planned=float(hours_planned or 0.0),
                hours_available=float(hours_available or 0.0),
                overload_hours=float(overload or 0.0),
            )
            db.add(cap_w)

            overload_total += float(overload or 0.0)
            if float(overload or 0.0) > 0.0:
                overloaded_buckets += 1

        db.flush()

        if overloaded_buckets > 0:
            warnings.append(
                {
                    "code": "CAPACITY_OVERLOAD",
                    "msg": f"Detected {overloaded_buckets} overloaded buckets, total overload hours={overload_total:.3f}",
                    "overloaded_buckets": int(overloaded_buckets),
                    "overload_total": float(overload_total),
                }
            )

        # Summary of missing production_kind->area mappings
        if missing_area_pk_counts:
            warnings.append(
                {
                    "code": "NO_AREA_FOR_PRODUCTION_KIND_SUMMARY",
                    "msg": "Missing resource area mapping for production kinds",
                    "by_production_kind": [
                        {"production_kind_id": int(pkid), "production_kind_name": production_kind_name_map.get(int(pkid)), "count": int(cnt)}
                        for pkid, cnt in sorted(missing_area_pk_counts.items())
                    ],
                }
            )

        # 8) Pegging (one-level child→parent by bucket, per planned order)
        for order in created_orders:
            parent_item_id = int(order.item_id)
            spec_id = default_spec_map.get(parent_item_id)
            if not spec_id:
                continue
            comps = get_components_for_spec(spec_id)
            if not comps:
                continue
            for c in comps:
                try:
                    child_id = int(c.item_id)
                    comp_qty = float(c.quantity or 0.0)
                except Exception:
                    continue
                if comp_qty <= 0:
                    continue
                qty_contrib = float(order.qty or 0.0) * comp_qty
                if qty_contrib <= 0:
                    continue
                pl = PeggingLink(
                    run_id=run_id,
                    child_item_id=child_id,
                    parent_item_id=parent_item_id,
                    demand_ref=None,
                    qty_contribution=float(qty_contrib),
                    need_date=order.bucket_date,
                    parent_need_date=order.bucket_date,
                )
                db.add(pl)

        db.flush()

        # 9) priority_index for orders/purchases
        prio_cfg = (snapshot.get("prioritization") or {}) if isinstance(snapshot, dict) else {}
        w_crit = float(prio_cfg.get("weight_criticality", 0.4) or 0.4)
        w_imp = float(prio_cfg.get("weight_importance", 0.3) or 0.3)
        w_cycle = float(prio_cfg.get("weight_cycle_time", 0.3) or 0.3)
        default_importance = float(prio_cfg.get("default_importance", 1) or 1.0)

        # Norms for production items (already in item_norm_cache)
        max_norm = max([v for v in item_norm_cache.values()] or [0.0])
        # Lead times for purchases
        purchase_lt: List[int] = [int(p.lead_time_days or 0) for p in created_purchases]
        max_lt = max(purchase_lt or [0])

        d0: date = date.today()

        # Orders
        for o in created_orders:
            need_dt: date = o.need_date
            days_to_need = (need_dt - d0).days
            if days_to_need <= 0:
                days_to_need = 1
            # Approx criticality: net demand present -> stock factor ~1
            criticality = 2.0 / float(days_to_need)
            cycle_norm = 0.0
            nh = float(item_norm_cache.get(int(o.item_id), 0.0) or 0.0)
            if max_norm > 0.0:
                cycle_norm = nh / max_norm
            prio = w_crit * criticality + w_imp * default_importance + w_cycle * cycle_norm
            o.priority_index = float(prio)

        # Purchases
        for p in created_purchases:
            need_dt: date = p.need_date
            days_to_need = (need_dt - d0).days
            if days_to_need <= 0:
                days_to_need = 1
            criticality = 2.0 / float(days_to_need)
            cycle_norm = 0.0
            if max_lt > 0:
                cycle_norm = float(int(p.lead_time_days or 0)) / float(max_lt)
            prio = w_crit * criticality + w_imp * default_importance + w_cycle * cycle_norm
            p.priority_index = float(prio)

        db.flush()

        # 10) Finalize run with extended KPI
        kpi = {
            "orders_created": int(len(created_orders)),
            "purchases_created": int(len(created_purchases)),
            "items_involved": int(len(all_item_ids)),
            "stages_count": int(stages_created),
            "capacity_overload_total": float(overload_total),
            "overloaded_buckets": int(overloaded_buckets),
            "missing_area_stage_distinct": int(len(missing_area_stage_counts)),
            "missing_area_stage_total": int(sum(missing_area_stage_counts.values())),
        }

        run.status = "SUCCESS"
        run.kpi = kpi
        run.warnings = warnings
        run.finished_at = datetime.utcnow()

        db.commit()
        return run_id

    except Exception as e:
        try:
            run.status = "FAILED"
            run.finished_at = datetime.utcnow()
            w = warnings or []
            w.append({"code": "RUN_FAILED", "msg": str(e)})
            run.warnings = w
            db.commit()
        except Exception:
            db.rollback()
        raise

# --- Retention & pin management for planning runs ---

def cleanup_planning_runs(db: Session, older_than_days: int = 30, dry_run: bool = False) -> Dict[str, Any]:
    """
    Delete planning runs older than N days except those marked as pinned.
    Returns a report with affected run_ids. When dry_run=True, nothing is deleted.
    """
    try:
        days = max(1, int(older_than_days or 30))
    except Exception:
        days = 30
    cutoff_dt = datetime.utcnow() - timedelta(days=days)

    q = (
        db.query(PlanningRun)
        .filter(
            PlanningRun.started_at < cutoff_dt,
            PlanningRun.pinned.is_(False),
        )
    )
    runs: List[PlanningRun] = q.all()
    run_ids: List[int] = [int(r.run_id) for r in runs]

    report: Dict[str, Any] = {
        "dry_run": bool(dry_run),
        "older_than_days": int(days),
        "cutoff": cutoff_dt.isoformat(),
        "count": len(run_ids),
        "run_ids": run_ids,
    }

    if dry_run:
        return report

    if run_ids:
        # Cascade deletes will clear dependent tables (planned_order, planned_purchase, capacity_load, pegging_link, etc.)
        q.delete(synchronize_session=False)
        db.commit()

    return report


def set_run_pinned(db: Session, run_id: int, pinned: bool) -> Dict[str, Any]:
    """
    Set pinned flag for a planning run to protect from cleanup.
    """
    r: Optional[PlanningRun] = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).first()
    if not r:
        raise RuntimeError(f"Run {run_id} not found")
    r.pinned = bool(pinned)
    db.commit()
    return {"run_id": int(r.run_id), "pinned": bool(r.pinned)}