from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple, Set, DefaultDict, Callable

from sqlalchemy.orm import Session
from sqlalchemy import func, and_, asc, desc
from collections import defaultdict
import json
import re
import logging
logger = logging.getLogger("prodplan.planning")

from ..models import (
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
    ProductionResource,
    ProductionStage,
    SpecOperation,
    Operation,
    ProductionKind,
    ResourceProductionKind,
    ResourceStage,
    Specification,
    ProductionOrder,
    ProductionProduct,
    SupplierOrder,
    SupplierOrderItem,
    Supplier,
    ItemCategory,
)
from .stage_logic import pick_area_for_stage
from .forecast import forecast_payload
from .mrp_stock_helpers import (
    active_wip_eta_by_item as _active_wip_eta_by_item,
    consume_wip_at_or_before as _consume_wip_at_or_before,
    effective_stock_by_item_all as _effective_stock_by_item_all,
)
from .pegging_builder import PeggingBuilder
from .replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    classify_replenishment_flow,
)
from .warnings import make_warning, log_warning
from .supplier_order_status import (
    STATE_TO_PHASE,
    NETTING_PHASES,
    state_counts_in_mrp as _supplier_order_counts_in_mrp,
)
from .bom_specification_resolver import (
    BomSpecificationResolutionError,
    BomSpecificationResolver,
)
from .production_output_truth import accepted_product_remaining_expr


_REF1C_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _unit_display_from_parts(
    guid: Optional[str],
    short_name: Optional[str],
    unit_name: Optional[str],
    unit_code: Optional[str],
) -> str:
    value = str(short_name or unit_name or unit_code or "").strip()
    if value:
        return value
    raw_guid = str(guid or "").strip()
    if raw_guid and not _REF1C_RE.match(raw_guid):
        return raw_guid
    return "шт."


def _bom_descendant_ids_for_roots(db: Session, root_item_ids: List[int]) -> Set[int]:
    roots = sorted({int(i) for i in root_item_ids if i is not None})
    if not roots:
        return set()
    descendants_by_root = BomSpecificationResolver(db).descendant_ids_by_root(roots)
    return {
        int(item_id)
        for descendants in descendants_by_root.values()
        for item_id in descendants
    }


def _load_stage_area_context(db: Session) -> Tuple[Dict[int, str], Dict[int, int], Dict[int, str]]:
    """Return stage names plus the best production resource for each stage."""
    stage_name_by_id: Dict[int, str] = {}
    try:
        for stage in db.query(ProductionStage).all():
            sid = getattr(stage, "stage_id", None)
            if sid is not None:
                stage_name_by_id[int(sid)] = str(getattr(stage, "stage_name", "") or "")
    except Exception:
        stage_name_by_id = {}

    try:
        resources = db.query(ProductionResource).all()
        stages_by_resource: Dict[int, Set[int]] = {}
        for row in db.query(ResourceStage).all():
            rid = getattr(row, "resource_id", None)
            sid = getattr(row, "stage_id", None)
            if rid is None or sid is None:
                continue
            stages_by_resource.setdefault(int(rid), set()).add(int(sid))
    except Exception:
        resources = []
        stages_by_resource = {}

    area_id_by_stage: Dict[int, int] = {}
    for stage_id in stage_name_by_id:
        area_id = pick_area_for_stage(resources, stages_by_resource, stage_name_by_id, int(stage_id))
        if area_id is not None:
            area_id_by_stage[int(stage_id)] = int(area_id)

    area_name_by_id: Dict[int, str] = {}
    for resource in resources:
        rid = getattr(resource, "resource_id", None)
        if rid is None:
            continue
        area_name_by_id[int(rid)] = str(getattr(resource, "resource_name", "") or "")

    return stage_name_by_id, area_id_by_stage, area_name_by_id


def _load_purchase_area_map(db: Session, item_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Map a purchased component to the stage/resource where it is consumed."""
    unique_item_ids = sorted({int(item_id) for item_id in item_ids if item_id is not None})
    if not unique_item_ids:
        return {}

    stage_name_by_id, area_id_by_stage, area_name_by_id = _load_stage_area_context(db)
    component_stage: Dict[int, int] = {}
    try:
        rows = (
            db.query(SpecComponent.item_id, SpecComponent.stage_id)
            .filter(SpecComponent.item_id.in_(unique_item_ids))
            .filter(SpecComponent.stage_id.isnot(None))
            .all()
        )
    except Exception:
        rows = []

    for row in rows:
        item_id_val = getattr(row, "item_id", row[0] if isinstance(row, (tuple, list)) and len(row) > 0 else None)
        stage_id_val = getattr(row, "stage_id", row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else None)
        if item_id_val is None or stage_id_val is None:
            continue
        iid = int(item_id_val)
        if iid in component_stage:
            continue
        component_stage[iid] = int(stage_id_val)

    result: Dict[int, Dict[str, Any]] = {}
    for iid, sid in component_stage.items():
        area_id = area_id_by_stage.get(int(sid))
        area_name = area_name_by_id.get(int(area_id), "") if area_id is not None else ""
        result[int(iid)] = {
            "main_area_id": int(area_id) if area_id is not None else None,
            "main_area_name": area_name or stage_name_by_id.get(int(sid)) or None,
            "main_stage_id": int(sid),
            "main_stage_name": stage_name_by_id.get(int(sid)) or None,
        }
    return result


def _load_production_area_map(db: Session, item_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Map a produced item to its main stage/resource from the default spec."""
    unique_item_ids = sorted({int(item_id) for item_id in item_ids if item_id is not None})
    if not unique_item_ids:
        return {}

    stage_name_by_id, area_id_by_stage, area_name_by_id = _load_stage_area_context(db)
    item_to_spec: Dict[int, int] = {}
    spec_resolver = BomSpecificationResolver(db)
    spec_ids: Set[int] = set()
    for item_id_val in unique_item_ids:
        spec_id_val = spec_resolver.default_spec_id(item_id_val)
        if spec_id_val is None:
            continue
        item_to_spec[int(item_id_val)] = int(spec_id_val)
        spec_ids.add(int(spec_id_val))

    if not spec_ids:
        return {}

    stage_hours_by_spec: Dict[int, Dict[int, float]] = defaultdict(dict)
    try:
        op_rows = (
            db.query(
                SpecOperation.spec_id,
                SpecOperation.stage_id,
                func.sum(func.coalesce(SpecOperation.time_norm, Operation.time_norm, 0.0)).label("hours"),
            )
            .outerjoin(Operation, SpecOperation.operation_id == Operation.operation_id)
            .filter(SpecOperation.spec_id.in_(list(spec_ids)))
            .filter(SpecOperation.stage_id.isnot(None))
            .group_by(SpecOperation.spec_id, SpecOperation.stage_id)
            .all()
        )
    except Exception:
        op_rows = []

    for row in op_rows or []:
        spec_id_val = getattr(row, "spec_id", row[0] if isinstance(row, (tuple, list)) and len(row) > 0 else None)
        stage_id_val = getattr(row, "stage_id", row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else None)
        hours_val = getattr(row, "hours", row[2] if isinstance(row, (tuple, list)) and len(row) > 2 else 0.0)
        if spec_id_val is None or stage_id_val is None:
            continue
        stage_hours_by_spec[int(spec_id_val)][int(stage_id_val)] = float(hours_val or 0.0)

    if not stage_hours_by_spec:
        try:
            comp_rows = (
                db.query(SpecComponent.spec_id, SpecComponent.stage_id)
                .filter(SpecComponent.spec_id.in_(list(spec_ids)))
                .filter(SpecComponent.stage_id.isnot(None))
                .all()
            )
        except Exception:
            comp_rows = []
        for row in comp_rows or []:
            spec_id_val = getattr(row, "spec_id", row[0] if isinstance(row, (tuple, list)) and len(row) > 0 else None)
            stage_id_val = getattr(row, "stage_id", row[1] if isinstance(row, (tuple, list)) and len(row) > 1 else None)
            if spec_id_val is None or stage_id_val is None:
                continue
            stage_hours_by_spec[int(spec_id_val)].setdefault(int(stage_id_val), 0.0)

    result: Dict[int, Dict[str, Any]] = {}
    for item_id_val, spec_id_val in item_to_spec.items():
        stage_hours = stage_hours_by_spec.get(int(spec_id_val), {})
        if not stage_hours:
            continue
        sid = max(stage_hours.items(), key=lambda item: float(item[1] or 0.0))[0]
        area_id = area_id_by_stage.get(int(sid))
        area_name = area_name_by_id.get(int(area_id), "") if area_id is not None else ""
        result[int(item_id_val)] = {
            "main_area_id": int(area_id) if area_id is not None else None,
            "main_area_name": area_name or stage_name_by_id.get(int(sid)) or None,
            "main_stage_id": int(sid),
            "main_stage_name": stage_name_by_id.get(int(sid)) or None,
        }
    return result


# Default planning config fallback (aligned with Alembic seed)
DEFAULT_PLANNING_CONFIG: Dict[str, Any] = {
    "planning_horizon_days": 90,
    "mps_daily_horizon_days": 90,
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
    "toggles": {"include_wip": False},
}

# Pagination constants
SERVER_MAX_LIMIT = 1000
DEFAULT_PAGE_LIMIT = 50


def _production_supply_qty_expr():
    """Quantity still expected from an open production line.

    A completed 1C order is historical execution, not future supply. Its
    output becomes MRP coverage only through the synced warehouse balance.
    """
    return accepted_product_remaining_expr(
        ProductionProduct.quantity,
        ProductionProduct.produced_qty,
    )
# Состояния заказа поставщику, НЕ учитываемые как ожидаемое поступление в MRP.
# Производная от канонической карты фаз (см. supplier_order_status): всё, что не
# относится к фазам «в пути» / «на складе». Сохранена для обратной совместимости
# импортов в production_control_material_availability / period_plan_service.
SUPPLIER_ORDER_EXCLUDED_STATE_NAMES = {
    name for name, phase in STATE_TO_PHASE.items() if phase not in NETTING_PHASES
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


def _load_turning_blank_priority_map(db: Session, run_id: int) -> Dict[Tuple[int, str], Dict[str, Any]]:
    try:
        run = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).first()
        warnings = list(getattr(run, "warnings", None) or []) if run else []
    except Exception:
        warnings = []
    result: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for warning in warnings:
        if not isinstance(warning, dict) or warning.get("code") != "TURNING_BLANK_PRIORITY":
            continue
        try:
            item_id = int(warning.get("item_id"))
            need_date = str(warning.get("need_date") or "")
        except Exception:
            continue
        if not need_date:
            continue
        result[(item_id, need_date)] = warning
    return result


def _turning_blank_badge(priority_map: Dict[Tuple[int, str], Dict[str, Any]], item_id: int, need_date: Any) -> Optional[str]:
    date_key = need_date.isoformat() if isinstance(need_date, (datetime, date)) else str(need_date or "")
    if (int(item_id), date_key) in priority_map:
        return "Заготовка под токарный участок"
    return None


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
        started_at=datetime.now(timezone.utc),
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
    plan_ids = sorted({int(r.source_plan_id) for r in rows if getattr(r, "source_plan_id", None) is not None})
    plans_by_id: Dict[int, ProductionPlanHeader] = {}
    if plan_ids:
        plans_by_id = {
            int(plan.id): plan
            for plan in db.query(ProductionPlanHeader).filter(ProductionPlanHeader.id.in_(plan_ids)).all()
        }

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
        source_plan = plans_by_id.get(int(r.source_plan_id)) if getattr(r, "source_plan_id", None) is not None else None
        period_from = r.period_from or (source_plan.period_from if source_plan else None)
        period_to = r.period_to or (source_plan.period_to if source_plan else None)
        result.append(
            {
                "run_id": int(r.run_id),
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "horizon_days": r.horizon_days,
                "pinned": bool(getattr(r, "pinned", False)),
                "source_plan_id": int(r.source_plan_id) if r.source_plan_id is not None else None,
                "source_plan_name": str(source_plan.name or "") if source_plan else None,
                "period_from": period_from.isoformat() if period_from else None,
                "period_to": period_to.isoformat() if period_to else None,
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
    rework_cnt = db.query(func.count(PlannedRework.rework_id)).filter(PlannedRework.run_id == run_id).scalar() or 0
    source_plan = None
    if getattr(r, "source_plan_id", None) is not None:
        source_plan = db.query(ProductionPlanHeader).filter(ProductionPlanHeader.id == int(r.source_plan_id)).first()
    period_from = r.period_from or (source_plan.period_from if source_plan else None)
    period_to = r.period_to or (source_plan.period_to if source_plan else None)

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
            "source_plan_id": int(r.source_plan_id) if r.source_plan_id is not None else None,
            "source_plan_name": str(source_plan.name or "") if source_plan else None,
            "period_from": period_from.isoformat() if period_from else None,
            "period_to": period_to.isoformat() if period_to else None,
        },
        "counts": {
            "production_orders": int(order_cnt),
            "purchase_requests": int(purch_cnt),
            "rework_requests": int(rework_cnt),
        },
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
    stage_name_by_id, _fallback_area_by_stage, resource_name_by_id = _load_stage_area_context(db)
    area_name_by_id.update(resource_name_by_id)

    stage_by_order: Dict[int, List[Dict[str, Any]]] = {}
    for s in stages:
        sid = int(s.stage_id)
        aid = int(s.area_id) if s.area_id is not None else None
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
            spec_resolver = BomSpecificationResolver(db)
            item_to_spec: Dict[int, int] = {}
            spec_ids_set: Set[int] = set()
            for iid in item_ids_page:
                sid = spec_resolver.default_spec_id(iid)
                if sid is None:
                    continue
                item_to_spec[int(iid)] = int(sid)
                spec_ids_set.add(int(sid))
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
        except BomSpecificationResolutionError:
            raise
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
                data.update(forecast_payload(fin_d, need_d))
        except Exception:
            data["forecast_date"] = data.get("finish_date")
            data["forecast_shift_days"] = None
            data["forecast_reason"] = None
            data["forecast_status"] = "unavailable"

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
        # Supplier coverage is backend-owned display data.  The frontend must
        # not reconstruct percentages or statuses from raw quantities.
        req = float(values.get("requested_qty") or 0.0)
        net = float(values.get("qty") or 0.0)
        covered = round(max(0.0, req - net), 6)
        values["supplier_covered_qty"] = covered
        if req <= 1e-9:
            values["supplier_coverage_pct"] = None
            values["supplier_coverage_status"] = "not_required"
            values["supplier_coverage_label"] = None
        else:
            coverage_pct = max(0, min(100, round(covered / req * 100)))
            values["supplier_coverage_pct"] = coverage_pct
            values["supplier_coverage_status"] = (
                "full" if coverage_pct >= 100 else "partial" if covered > 1e-9 else "none"
            )
            unit_label = str(values.get("unit") or "").strip()
            unit_suffix = f" {unit_label}" if unit_label else ""
            values["supplier_coverage_label"] = (
                f"{covered:g} / {req:g}{unit_suffix} ({coverage_pct}%)"
                if covered > 1e-9
                else None
            )
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


def _load_item_category_meta(db: Session, item_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    unique_ids = sorted({int(iid) for iid in item_ids if iid is not None})
    if not unique_ids:
        return {}

    try:
        rows = (
            db.query(
                Item.item_id,
                ItemCategory.category_id,
                ItemCategory.category_code,
                ItemCategory.category_name,
                ItemCategory.category_ref1c,
            )
            .outerjoin(ItemCategory, Item.category_id == ItemCategory.category_id)
            .filter(Item.item_id.in_(unique_ids))
            .all()
        )
    except Exception:
        rows = []

    result: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        try:
            item_id_val, category_id_val, category_code_val, category_name_val, category_ref1c_val = row
        except Exception:
            continue
        has_linked_category = category_id_val is not None
        resolved_group_name = (category_name_val or "").strip()
        if not resolved_group_name and has_linked_category:
            resolved_group_name = "Без названия группы"
        if not resolved_group_name:
            resolved_group_name = "Без товарной группы"

        result[int(item_id_val)] = {
            "group_id": int(category_id_val) if category_id_val is not None else None,
            "group_name": resolved_group_name,
            "group_ref1c": category_ref1c_val,
        }
    return result


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
    # Category identity is part of the immutable result payload.  Grouped
    # readers must not join mutable Item metadata when the HTTP request opens.
    category_by_item = _load_item_category_meta(
        db,
        [int(row["item_id"]) for row in data if row.get("item_id") is not None],
    )
    for row in data:
        category = category_by_item.get(int(row.get("item_id") or 0), {})
        row["category_id"] = category.get("group_id")
        row["category_name"] = (
            category.get("group_name") or "Без товарной группы"
        )
        row["category_ref1c"] = category.get("group_ref1c")

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

def capacity_status(overload_hours: Any) -> str:
    return "overloaded" if float(overload_hours or 0.0) > 1e-9 else "within_capacity"


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
            "capacity_status": capacity_status(r.overload_hours),
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
        data = json.loads(p.read_text("utf-8") or "{}")
        if not isinstance(data, dict):
            return None
        val = str(data.get("last_sync") or "").strip()
        return val or None
    except Exception:
        return None


def _load_late_supplier_order_coverage(db: Session, item_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Load active supplier orders that can cover demand, but arrive after the need date.
    This is diagnostic only: late orders do not reduce MRP purchase quantity.
    """
    ids = sorted({int(iid) for iid in item_ids if iid is not None})
    if not ids:
        return {}

    try:
        rows = (
            db.query(
                SupplierOrderItem.item_id_ref,
                SupplierOrderItem.delivery_date,
                SupplierOrderItem.remaining_qty,
                SupplierOrder.order_number,
                SupplierOrder.order_state_key,
                SupplierOrder.order_state_name,
            )
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(SupplierOrderItem.item_id_ref.in_(ids))
            .filter(SupplierOrder.deletion_mark.is_(False))
            .filter(SupplierOrderItem.delivery_date.isnot(None))
            .filter(func.coalesce(SupplierOrderItem.remaining_qty, 0.0) > 0)
            .order_by(SupplierOrderItem.delivery_date.asc(), SupplierOrder.order_number.asc())
            .all()
        )
    except Exception:
        rows = []

    result: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for iid, delivery_dt, qty, order_number, state_key, state_name in rows:
        try:
            if not _supplier_order_counts_in_mrp(state_name):
                continue
            item_id = int(iid)
            delivery_date = delivery_dt.date() if isinstance(delivery_dt, datetime) else delivery_dt
            if not isinstance(delivery_date, date):
                delivery_date = _to_date(str(delivery_dt))
            remaining_qty = float(qty or 0.0)
        except Exception:
            continue
        if remaining_qty <= 1e-12:
            continue
        result[item_id].append(
            {
                "delivery_date": delivery_date,
                "remaining_qty": remaining_qty,
                "order_number": str(order_number or "").strip(),
            }
        )
    return dict(result)


def _late_supplier_order_badge(
    late_supplier_rows: Dict[int, List[Dict[str, Any]]],
    item_id: int,
    need_date: Any,
    qty: Any,
) -> Optional[str]:
    if not need_date:
        return None
    try:
        need_dt = need_date.date() if isinstance(need_date, datetime) else need_date
        if not isinstance(need_dt, date):
            need_dt = _to_date(str(need_date))
        required_qty = float(qty or 0.0)
    except Exception:
        return None
    if required_qty <= 1e-12:
        return None

    total_late = 0.0
    first_delivery: Optional[date] = None
    order_numbers: List[str] = []
    for row in late_supplier_rows.get(int(item_id), []) or []:
        delivery_date = row.get("delivery_date")
        if not isinstance(delivery_date, date) or delivery_date <= need_dt:
            continue
        if first_delivery is None:
            first_delivery = delivery_date
        total_late += float(row.get("remaining_qty", 0.0) or 0.0)
        order_number = str(row.get("order_number") or "").strip()
        if order_number and order_number not in order_numbers:
            order_numbers.append(order_number)
        if total_late + 1e-9 >= required_qty:
            break

    if first_delivery is None or total_late <= 1e-12:
        return None

    delay_days = max(0, int((first_delivery - need_dt).days))
    order_suffix = f" ({', '.join(order_numbers[:2])})" if order_numbers else ""
    if total_late + 1e-9 >= required_qty:
        return f"Покрыто заказом, но опоздание {delay_days} дн.{order_suffix}"
    return f"Частично покрыто заказом, но опоздание {delay_days} дн.{order_suffix}"


def _merge_badges(*badges: Optional[str]) -> Optional[str]:
    parts: List[str] = []
    for badge in badges:
        text = str(badge or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "; ".join(parts) if parts else None
