"""Periodic MRP reconciliation — top up residual demand on fixed snapshots.

Why this exists
---------------
A period plan is fixed and an MRP snapshot is taken; production orders and
supplier orders are issued from it. Reality then drifts:

* a production order is closed with an un-produced remainder (the components
  came back to stock, but the demand is still open and the order is gone);
* a stock count changes the on-hand quantity of items that were part of the MRP.

The snapshot's `net_required_qty` is frozen at snapshot time and never reflects
this drift. This service recomputes the *current* net demand for each active
snapshot and tops up the missing coverage:

* production-flow items → a fresh internal production order (journal line) the
  user can push to 1C with the usual button;
* purchase-flow items → a fresh ``PlannedPurchase`` row in the same snapshot run.

Nothing is sent to 1C here — that stays a user action (see
``.docs/one_c_export_from_prodplan.md``).

Idempotency
-----------
The recompute reuses ``_explode_bom_net_first``, which nets gross demand against
current stock **and open WIP** (open journal lines with ``remaining_qty > 0``).
So once this service materialises a catch-up production order, that order becomes
open WIP and the next run nets it out — the gap returns to zero and no duplicate
is created. For purchases, the gap is additionally deduped against supplier
orders already arriving and ``PlannedPurchase`` rows already present in the run.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    Item,
    MrpRequirement,
    Operation,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionPlanLine,
    ProductionProduct,
    ResourceProductionKind,
    SpecComponent,
    SpecOperation,
    Specification,
)
from .capacity_scheduler import CapacityScheduler
from .period_plan_service import (
    _explode_bom_net_first,
    _load_purchase_supplier_remaining,
    _to_float,
)
from .production_control_journal import _default_spec_id_for_item
from .replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    classify_replenishment_flow,
)

EPS = 1e-9
FIXED_SNAPSHOT_STATUS = "FIXED_SNAPSHOT"


def _latest_active_snapshot_run_ids(db: Session) -> List[int]:
    """
    Latest FIXED_SNAPSHOT run per source plan whose period has not fully passed
    (``period_to`` is null or >= today). One snapshot per plan is reconciled —
    the most recent one.
    """
    today = date.today()
    rows = (
        db.query(PlanningRun.source_plan_id, func.max(PlanningRun.run_id))
        .filter(PlanningRun.status == FIXED_SNAPSHOT_STATUS)
        .filter(PlanningRun.source_plan_id.isnot(None))
        .filter((PlanningRun.period_to.is_(None)) | (PlanningRun.period_to >= today))
        .group_by(PlanningRun.source_plan_id)
        .all()
    )
    return [int(run_id) for _plan_id, run_id in rows if run_id is not None]


def _rebuild_plan_demands(db: Session, plan_id: int) -> Dict[int, Dict[date, float]]:
    """Level-0 demand buckets per item from the plan lines (mirrors snapshot)."""
    demands: Dict[int, Dict[date, float]] = {}
    lines = (
        db.query(ProductionPlanLine)
        .filter(ProductionPlanLine.plan_id == int(plan_id))
        .filter(ProductionPlanLine.qty > 0)
        .all()
    )
    for line in lines:
        qty = _to_float(line.qty)
        if qty <= 0:
            continue
        item_demands = demands.setdefault(int(line.item_id), {})
        item_demands[line.bucket_date] = item_demands.get(line.bucket_date, 0.0) + qty
    return demands


def _bump_requirement_coverage(req: Optional[MrpRequirement], added_qty: float) -> None:
    """Mirror the materialisation coverage bump, clamped to net_required_qty."""
    if req is None or added_qty <= EPS:
        return
    net_qty = _to_float(req.net_required_qty)
    new_covered = min(_to_float(req.covered_qty) + float(added_qty), net_qty)
    req.covered_qty = new_covered
    req.remaining_qty = max(net_qty - new_covered, 0.0)


def reconcile_snapshot(db: Session, run_id: int, *, dry_run: bool = False) -> Dict[str, Any]:
    """
    Recompute current net demand for one FIXED_SNAPSHOT run and top up the gap.

    Returns a per-item summary of what was (or, in dry-run, would be) added.
    """
    run = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).one_or_none()
    if run is None:
        raise ValueError(f"run_id={run_id}: прогон не найден")
    if str(run.status or "") != FIXED_SNAPSHOT_STATUS:
        raise ValueError(f"run_id={run_id}: не FIXED_SNAPSHOT (status={run.status})")
    if run.source_plan_id is None:
        raise ValueError(f"run_id={run_id}: прогон не привязан к плану периода")

    raw_demands = _rebuild_plan_demands(db, int(run.source_plan_id))
    if not raw_demands:
        return {
            "run_id": int(run.run_id),
            "source_plan_id": int(run.source_plan_id),
            "status": "ok",
            "dry_run": bool(dry_run),
            "production_added": [],
            "purchase_added": [],
            "note": "в плане нет положительной потребности",
        }

    period_to = run.period_to or max(
        (d for buckets in raw_demands.values() for d in buckets), default=date.today()
    )

    # Reconciliation is a TOTAL-over-the-period balance check, not a schedule:
    # we ask "is the whole period's demand covered by current stock + every open
    # order + incoming supply?", deliberately ignoring per-bucket timing.
    # `_explode_bom_net_first` nets WIP time-aware (a late order can't cover an
    # earlier bucket), so we collapse each item's demand into a single bucket at
    # period_to. Then every open WIP line with ETA <= period_to is credited and
    # we don't raise false gaps just because an existing order is scheduled mid
    # period. Catch-up orders we create carry planned_finish = period_to, so the
    # next run nets them out (idempotent).
    plan_demands: Dict[int, Dict[date, float]] = {
        int(iid): {period_to: sum(_to_float(q) for q in buckets.values())}
        for iid, buckets in raw_demands.items()
    }

    # Fresh net demand: nets gross against CURRENT stock + open WIP.
    _gross_map, net_map, _bom = _explode_bom_net_first(db, plan_demands)

    item_ids = [int(iid) for iid in net_map.keys()]
    items_by_id: Dict[int, Item] = {
        int(r.item_id): r
        for r in db.query(Item).filter(Item.item_id.in_(item_ids)).all()
    } if item_ids else {}
    req_by_item: Dict[int, MrpRequirement] = {
        int(r.item_id): r
        for r in db.query(MrpRequirement).filter(MrpRequirement.run_id == int(run.run_id)).all()
    }

    purchase_item_ids = [
        iid for iid in item_ids
        if classify_replenishment_flow(getattr(items_by_id.get(iid), "replenishment_method", None))
        == REPLENISHMENT_FLOW_PURCHASE
    ]
    supplier_work: Dict[int, List[Dict[str, Any]]] = {
        iid: [dict(row) for row in rows]
        for iid, rows in _load_purchase_supplier_remaining(db, purchase_item_ids, period_to).items()
    }
    existing_planned_purchase: Dict[int, float] = {
        int(iid): _to_float(total)
        for iid, total in (
            db.query(PlannedPurchase.item_id, func.sum(PlannedPurchase.qty))
            .filter(PlannedPurchase.run_id == int(run.run_id))
            .group_by(PlannedPurchase.item_id)
            .all()
        )
    }

    production_added: List[Dict[str, Any]] = []
    purchase_added: List[Dict[str, Any]] = []
    now = datetime.utcnow()

    for iid in sorted(net_map.keys()):
        fresh_net = sum(_to_float(q) for q in net_map[iid].values())
        if fresh_net <= EPS:
            continue
        item = items_by_id.get(iid)
        if item is None:
            continue
        flow = classify_replenishment_flow(getattr(item, "replenishment_method", None))
        req = req_by_item.get(iid)

        if flow == REPLENISHMENT_FLOW_PRODUCTION:
            # net_map already excludes current stock and open WIP, so the whole
            # fresh_net is the catch-up quantity. Creating it as an open journal
            # line turns it into WIP, which the next run nets out (idempotent).
            gap = fresh_net
            entry = {
                "item_id": int(iid),
                "item_code": str(item.item_code or ""),
                "item_name": str(item.item_name or ""),
                "qty": round(gap, 6),
                "requirement_id": int(req.id) if req else None,
            }
            if not dry_run:
                order = ProductionOrder(
                    order_number=f"MRP-RC-{int(run.run_id)}-{int(iid)}",
                    order_date=now,
                    order_ref1c=None,
                    is_posted=False,
                    deletion_mark=False,
                    source="mrp",
                    source_run_id=int(run.run_id),
                )
                db.add(order)
                db.flush()
                product = ProductionProduct(
                    order_id=int(order.order_id),
                    item_id=int(iid),
                    line_number=1,
                    quantity=gap,
                    produced_qty=0,
                    remaining_qty=gap,
                    spec_id=_default_spec_id_for_item(db, int(iid)),
                    source_mrp_requirement_id=int(req.id) if req else None,
                )
                db.add(product)
                db.flush()
                db.add(
                    ProductionOrderLineState(
                        product_id=int(product.product_id),
                        status="shortage",
                        issue_status="not_requested",
                        planned_start_date=req.period_from if req else run.period_from,
                        planned_finish_date=req.period_to if req else run.period_to,
                    )
                )
                _bump_requirement_coverage(req, gap)
                entry["order_id"] = int(order.order_id)
                entry["order_number"] = order.order_number
                entry["product_id"] = int(product.product_id)
            production_added.append(entry)

        elif flow == REPLENISHMENT_FLOW_PURCHASE:
            # net_map gives gross - stock for purchased items (no production WIP).
            # Net further against incoming supplier orders, then against purchase
            # lines already present in this run (dedup → idempotent).
            target = fresh_net
            for sup_row in supplier_work.get(iid, []):
                if target <= EPS:
                    break
                avail = _to_float(sup_row.get("remaining_qty"))
                if avail <= EPS:
                    continue
                used = min(avail, target)
                sup_row["remaining_qty"] = max(avail - used, 0.0)
                target -= used
            already = existing_planned_purchase.get(iid, 0.0)
            gap = target - already
            if gap <= EPS:
                continue
            lead_time = int(getattr(item, "replenishment_time", 0) or 0)
            need_date = period_to
            order_date = max(date.today(), need_date - timedelta(days=lead_time))
            entry = {
                "item_id": int(iid),
                "item_code": str(item.item_code or ""),
                "item_name": str(item.item_name or ""),
                "qty": round(gap, 6),
                "requirement_id": int(req.id) if req else None,
            }
            if not dry_run:
                db.add(
                    PlannedPurchase(
                        run_id=int(run.run_id),
                        item_id=int(iid),
                        requested_qty=gap,
                        planned_qty=gap,
                        qty=gap,
                        need_date=need_date,
                        order_date=order_date,
                        lead_time_days=lead_time,
                        bucket_date=need_date,
                        supplier_ref1c=getattr(item, "supplier_ref1c", None),
                        source_mrp_requirement_id=int(req.id) if req else None,
                    )
                )
                # Track so a second item with the same id in this loop won't
                # double-add (defensive; net_map keys are unique).
                existing_planned_purchase[iid] = already + gap
            purchase_added.append(entry)
        # rework flow is intentionally not auto-topped-up in v1.

    # Re-anchor every production line that is NOT yet open in 1C to a fresh
    # capacity-aware, child→parent-aware schedule starting today. Lines already
    # open in 1C stay where they are and pre-book their capacity.
    reschedule = _reschedule_run_journal(db, run, dry_run=dry_run)

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "run_id": int(run.run_id),
        "source_plan_id": int(run.source_plan_id),
        "status": "ok",
        "dry_run": bool(dry_run),
        "production_added": production_added,
        "purchase_added": purchase_added,
        "rescheduled": reschedule,
    }


def _stage_hours_and_areas(
    db: Session,
    spec_id: Optional[int],
    qty: float,
    resource_id_by_spec: Dict[int, int],
) -> tuple[Dict[int, float], Dict[int, Optional[int]]]:
    """Per-stage total norm-hours and the spec's production-kind area."""
    if not spec_id or qty <= 0:
        return {}, {}
    rows = (
        db.query(
            SpecOperation.stage_id,
            func.sum(func.coalesce(SpecOperation.time_norm, Operation.time_norm, 0)).label("h"),
        )
        .join(Operation, SpecOperation.operation_id == Operation.operation_id)
        .filter(SpecOperation.spec_id == int(spec_id))
        .filter(SpecOperation.stage_id.isnot(None))
        .group_by(SpecOperation.stage_id)
        .all()
    )
    stage_hours: Dict[int, float] = {}
    stage_areas: Dict[int, Optional[int]] = {}
    for stage_id, hours in rows:
        sid = int(stage_id)
        per_unit = float(hours or 0.0)
        if per_unit <= 1e-12:
            continue
        stage_hours[sid] = per_unit * float(qty)
        stage_areas[sid] = resource_id_by_spec.get(int(spec_id))
    if not stage_hours:
        component_stage_rows = (
            db.query(SpecComponent.stage_id)
            .filter(SpecComponent.spec_id == int(spec_id))
            .filter(SpecComponent.stage_id.isnot(None))
            .group_by(SpecComponent.stage_id)
            .all()
        )
        for row in component_stage_rows:
            sid_raw = getattr(row, "stage_id", row[0] if isinstance(row, (tuple, list)) and row else None)
            if sid_raw is None:
                continue
            sid = int(sid_raw)
            stage_hours[sid] = 0.0
            stage_areas[sid] = resource_id_by_spec.get(int(spec_id))
    return stage_hours, stage_areas


def _reschedule_run_journal(db: Session, run: PlanningRun, *, dry_run: bool) -> Dict[str, Any]:
    """
    Recompute planned start/finish for the run's production journal lines.

    Lines whose order is already open in 1C (order_ref1c set) are fixed; they
    keep their dates and pre-book capacity. Lines not yet in 1C are replanned
    from today, parents-first, components finishing before the assemblies that
    consume them.
    """
    req_ids = [
        int(r.id)
        for r in db.query(MrpRequirement.id).filter(MrpRequirement.run_id == int(run.run_id)).all()
    ]
    if not req_ids:
        return {"floating": 0, "fixed": 0, "warnings": []}

    rows = (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionProduct.source_mrp_requirement_id.in_(req_ids))
        .all()
    )
    if not rows:
        return {"floating": 0, "fixed": 0, "warnings": []}

    req_by_id = {
        int(r.id): r
        for r in db.query(MrpRequirement).filter(MrpRequirement.run_id == int(run.run_id)).all()
    }

    # Build child→parent map among the items in play: a parent's default-spec
    # components are its children.
    item_ids = {int(pp.item_id) for pp, _po, _st in rows}
    default_spec_by_item = {
        int(ds.item_id): int(ds.spec_id)
        for ds in db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id.in_(item_ids))
        .all()
    }
    resource_id_by_spec: Dict[int, int] = {}
    if default_spec_by_item:
        for spec_id, resource_id in (
            db.query(Specification.spec_id, ResourceProductionKind.resource_id)
            .join(
                ResourceProductionKind,
                ResourceProductionKind.production_kind_id == Specification.production_kind_id,
            )
            .filter(Specification.spec_id.in_(set(default_spec_by_item.values())))
            .order_by(ResourceProductionKind.id.asc())
            .all()
        ):
            resource_id_by_spec.setdefault(int(spec_id), int(resource_id))

    parents_of_item: Dict[int, set] = {}
    if default_spec_by_item:
        comp_rows = (
            db.query(SpecComponent.spec_id, SpecComponent.item_id)
            .filter(SpecComponent.spec_id.in_(set(default_spec_by_item.values())))
            .all()
        )
        spec_to_parent = {sid: iid for iid, sid in default_spec_by_item.items()}
        for spec_id, comp_item in comp_rows:
            parent = spec_to_parent.get(int(spec_id))
            child = int(comp_item)
            if parent is not None and child in item_ids:
                parents_of_item.setdefault(child, set()).add(int(parent))

    scheduler = CapacityScheduler(db, run.config_snapshot or {})
    orders: List[Dict[str, Any]] = []
    state_by_key: Dict[int, ProductionOrderLineState] = {}
    for pp, po, state in rows:
        qty_open = _to_float(pp.remaining_qty)
        if qty_open <= 1e-9:
            qty_open = _to_float(pp.quantity)
        spec_id = pp.spec_id or default_spec_by_item.get(int(pp.item_id))
        stage_hours, stage_areas = _stage_hours_and_areas(db, spec_id, qty_open, resource_id_by_spec)
        workshop_id = resource_id_by_spec.get(int(spec_id)) if spec_id else None
        req = req_by_id.get(int(pp.source_mrp_requirement_id)) if pp.source_mrp_requirement_id else None
        need_date = (req.period_to if req else None) or run.period_to
        fixed = bool(po.order_ref1c)
        orders.append({
            "key": int(pp.product_id),
            "item_id": int(pp.item_id),
            "qty": qty_open,
            "need_date": need_date,
            "stage_hours": stage_hours,
            "stage_areas": stage_areas,
            "workshop_id": workshop_id,
            "fixed": fixed,
            "fixed_start": (state.planned_start_date if state else None),
            "fixed_finish": (state.planned_finish_date if state else None),
        })
        if state is not None:
            state_by_key[int(pp.product_id)] = state

    results, warnings = scheduler.schedule_orders_bom_aware(orders, parents_of_item)

    floating = 0
    for key, res in results.items():
        if res.get("fixed"):
            continue
        state = state_by_key.get(key)
        if state is None:
            continue
        workshop_id = res.get("workshop_id")
        if workshop_id is None:
            order_meta = next((o for o in orders if int(o["key"]) == int(key)), {})
            workshop_id = order_meta.get("workshop_id")
        state.workshop_id = int(workshop_id) if workshop_id is not None else None
        start_dt = res.get("order_start_date")
        finish_dt = res.get("order_finish_date")
        if isinstance(start_dt, datetime):
            state.planned_start_date = start_dt.date()
        elif isinstance(start_dt, date):
            state.planned_start_date = start_dt
        if isinstance(finish_dt, datetime):
            state.planned_finish_date = finish_dt.date()
        elif isinstance(finish_dt, date):
            state.planned_finish_date = finish_dt
        floating += 1

    fixed = sum(1 for o in orders if o.get("fixed"))
    return {"floating": floating, "fixed": fixed, "warnings": warnings}


def reconcile_all_active(db: Session, *, dry_run: bool = False) -> Dict[str, Any]:
    """Reconcile the latest snapshot of every plan whose period is still open."""
    run_ids = _latest_active_snapshot_run_ids(db)
    results: List[Dict[str, Any]] = []
    total_production = 0
    total_purchase = 0
    for rid in run_ids:
        try:
            res = reconcile_snapshot(db, rid, dry_run=dry_run)
            total_production += len(res.get("production_added", []))
            total_purchase += len(res.get("purchase_added", []))
            results.append(res)
        except Exception as exc:  # noqa: BLE001 — isolate one bad run from the rest
            db.rollback()
            results.append({"run_id": int(rid), "status": "error", "error": str(exc)})
    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "runs_checked": len(run_ids),
        "production_lines_added": total_production,
        "purchase_lines_added": total_purchase,
        "results": results,
    }
