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


from .constants import DEFAULT_PLANNING_CONFIG
from .config import get_active_planning_config
from .helpers import _deep_merge

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


def _clear_run_outputs(db: Session, run_id: int) -> None:
    """Delete all planned outputs of a run so a re-run is idempotent.

    Without this, recomputing an existing run_id re-adds orders/stages/etc.
    on top of the previous rows and doubles every result. PlannedOrderStage
    references planned_order, so it is deleted first.
    """
    db.query(PlannedOrderStage).filter(PlannedOrderStage.run_id == run_id).delete(synchronize_session=False)
    for model in (PeggingLink, CapacityLoad, PlannedRework, PlannedPurchase, PlannedOrder):
        db.query(model).filter(model.run_id == run_id).delete(synchronize_session=False)


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
