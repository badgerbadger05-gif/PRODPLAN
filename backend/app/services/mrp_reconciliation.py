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
The snapshot's gross demand is re-derived from its anchored level-0 roots
through current stock (``_current_snapshot_gross_by_item``), then the gap is
sized as ``net - open WIP`` (open journal lines with ``remaining_qty > 0``,
both 1C and local). So once this service materialises a catch-up production
order, that order becomes open WIP and the next run nets it out — the gap
returns to zero and no duplicate is created. For purchases, the gap is
additionally deduped against supplier orders already arriving and
``PlannedPurchase`` rows already present in the run.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    Item,
    MrpRequirement,
    MrpRequirementBucket,
    Operation,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionPlanLine,
    ProductionProduct,
    SyncLink,
    SpecComponent,
    SpecOperation,
    Specification,
)
from .capacity_scheduler import CapacityScheduler
from .period_plan_service import (
    _load_purchase_supplier_remaining,
    _to_float,
)
from .production_control_journal import (
    _default_spec_id_for_item,
    _split_qty_by_optimal_batch,
    dedupe_mrp_production_orders,
)
from .mrp_stock_helpers import effective_stock_by_item_all
from .replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    classify_replenishment_flow,
)

EPS = 1e-9
FIXED_SNAPSHOT_STATUS = "FIXED_SNAPSHOT"
PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"
DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


def _latest_active_snapshot_run_ids(db: Session) -> List[int]:
    """
    Latest FIXED_SNAPSHOT run per source plan whose period has not fully passed
    (``period_to`` is null or >= today). There must be only one active fixed
    snapshot per plan; max(run_id) is a defensive fallback for legacy duplicates.
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


def _existing_open_catchup_product(db: Session, *, run_id: int, item_id: int) -> Optional[ProductionProduct]:
    order_number = f"MRP-RC-{int(run_id)}-{int(item_id)}"
    linked_order_ids = {
        int(row.source_id)
        for row in (
            db.query(SyncLink.source_id)
            .filter(
                SyncLink.source_system == "PRODPLAN",
                SyncLink.source_doctype == "production_order",
                SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
                SyncLink.status == "success",
                SyncLink.target_ref_key.isnot(None),
            )
            .all()
        )
    }
    query = (
        db.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionOrder.source == "mrp")
        .filter(ProductionOrder.source_run_id == int(run_id))
        .filter(ProductionOrder.order_number == order_number)
        .filter(ProductionProduct.item_id == int(item_id))
        .filter(ProductionOrder.deletion_mark == False)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(func.coalesce(ProductionProduct.remaining_qty, 0) > 0)
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(("completed", "cancelled")))
        .order_by(ProductionProduct.product_id.desc())
    )
    for product in query.all():
        order = product.order
        if order and not order.order_ref1c and int(order.order_id) not in linked_order_ids:
            return product
    return None


def _active_production_qty_by_item(db: Session, item_ids: List[int]) -> Dict[int, float]:
    """Open production/WIP quantity per item, including 1C and local rows."""
    if not item_ids:
        return {}
    rows = (
        db.query(
            ProductionProduct.item_id,
            func.sum(func.coalesce(ProductionProduct.remaining_qty, 0.0)).label("qty"),
        )
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionProduct.item_id.in_([int(iid) for iid in item_ids]))
        .filter(ProductionOrder.deletion_mark == False)
        .filter(
            or_(
                ProductionOrder.order_state_key.is_(None),
                func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY,
            )
        )
        .filter(func.coalesce(ProductionProduct.remaining_qty, 0.0) > 0)
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(("completed", "cancelled")))
        .group_by(ProductionProduct.item_id)
        .all()
    )
    return {int(iid): _to_float(qty) for iid, qty in rows}


def _next_catchup_order_number(db: Session, *, run_id: int, item_id: int) -> str:
    base = f"MRP-RC-{int(run_id)}-{int(item_id)}"
    existing = {
        str(order_number)
        for (order_number,) in (
            db.query(ProductionOrder.order_number)
            .filter(ProductionOrder.source == "mrp")
            .filter(ProductionOrder.source_run_id == int(run_id))
            .filter(ProductionOrder.order_number.like(f"{base}%"))
            .all()
        )
        if order_number
    }
    if base not in existing:
        return base
    seq = 2
    while f"{base}-{seq}" in existing:
        seq += 1
    return f"{base}-{seq}"


def _create_catchup_product(
    db: Session,
    *,
    run: PlanningRun,
    item_id: int,
    qty: float,
    req: Optional[MrpRequirement],
    now: datetime,
) -> tuple[ProductionOrder, ProductionProduct]:
    order = ProductionOrder(
        order_number=_next_catchup_order_number(db, run_id=int(run.run_id), item_id=int(item_id)),
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
        item_id=int(item_id),
        line_number=1,
        quantity=qty,
        produced_qty=0,
        remaining_qty=qty,
        spec_id=_default_spec_id_for_item(db, int(item_id)),
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
    return order, product


def _split_oversized_catchup_batches(
    db: Session,
    run: PlanningRun,
    *,
    dry_run: bool,
    now: datetime,
) -> Dict[str, Any]:
    rows = (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState, Item, MrpRequirement)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .join(Item, Item.item_id == ProductionProduct.item_id)
        .join(MrpRequirement, MrpRequirement.id == ProductionProduct.source_mrp_requirement_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionOrder.source == "mrp")
        .filter(ProductionOrder.source_run_id == int(run.run_id))
        .filter(ProductionOrder.order_number.like(f"MRP-RC-{int(run.run_id)}-%"))
        .filter(ProductionOrder.order_ref1c.is_(None))
        .filter(ProductionOrder.deletion_mark == False)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(func.coalesce(ProductionProduct.remaining_qty, 0) > 0)
        .filter(func.coalesce(ProductionProduct.produced_qty, 0) <= EPS)
        .filter(Item.optimal_batch.isnot(None))
        .filter(Item.optimal_batch > 0)
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(("completed", "cancelled")))
        .order_by(ProductionProduct.product_id.asc())
        .all()
    )

    repaired: List[Dict[str, Any]] = []
    created_count = 0
    for product, order, state, item, req in rows:
        total = _to_float(product.remaining_qty)
        batch = _to_float(item.optimal_batch)
        if total <= batch + EPS:
            continue
        batches = _split_qty_by_optimal_batch(total, batch)
        if len(batches) <= 1:
            continue
        payload = {
            "item_id": int(product.item_id),
            "product_id": int(product.product_id),
            "order_id": int(order.order_id),
            "order_number": str(order.order_number or ""),
            "old_qty": total,
            "batches": [round(_to_float(q), 6) for q in batches],
        }
        repaired.append(payload)
        if dry_run:
            created_count += max(0, len(batches) - 1)
            continue

        product.quantity = batches[0]
        product.remaining_qty = batches[0]
        if state is not None:
            state.status = state.status or "shortage"
        for qty in batches[1:]:
            new_order, _new_product = _create_catchup_product(
                db,
                run=run,
                item_id=int(product.item_id),
                qty=_to_float(qty),
                req=req,
                now=now,
            )
            created_count += 1
            payload.setdefault("created_order_numbers", []).append(str(new_order.order_number or ""))

    return {
        "repaired": repaired,
        "created_orders": created_count,
    }


def _materialize_catchup_gap(
    db: Session,
    *,
    run: PlanningRun,
    item: Item,
    req: Optional[MrpRequirement],
    gap: float,
    now: datetime,
) -> List[tuple[ProductionOrder, ProductionProduct, float]]:
    item_id = int(item.item_id)
    batch = _to_float(getattr(item, "optimal_batch", None))
    if batch <= EPS:
        existing_product = _existing_open_catchup_product(db, run_id=int(run.run_id), item_id=item_id)
        if existing_product is not None:
            existing_product.quantity = _to_float(existing_product.quantity) + gap
            existing_product.remaining_qty = _to_float(existing_product.remaining_qty) + gap
            return [(existing_product.order, existing_product, gap)]
        order, product = _create_catchup_product(db, run=run, item_id=item_id, qty=gap, req=req, now=now)
        return [(order, product, gap)]

    created: List[tuple[ProductionOrder, ProductionProduct, float]] = []
    remaining_gap = _to_float(gap)
    while remaining_gap > EPS:
        qty = min(batch, remaining_gap)
        order, product = _create_catchup_product(db, run=run, item_id=item_id, qty=qty, req=req, now=now)
        created.append((order, product, qty))
        remaining_gap = max(remaining_gap - qty, 0.0)
    return created


def _current_snapshot_gross_by_item(
    db: Session,
    requirements: List[MrpRequirement],
    stock_by_item: Dict[int, float],
) -> tuple[Dict[int, float], Dict[int, int]]:
    """
    Recompute fixed-snapshot gross demand from its level-0 roots through
    current stock.

    Roots (bom_level 0) stay anchored to the frozen snapshot gross — the plan
    is not re-read. Each deeper node's gross is re-derived top-down as the sum
    of parent explode quantities, where a parent explodes
    ``max(gross - stock, 0)``: only physical stock stops the BOM explosion,
    open WIP does not — producing an open parent order still consumes its
    components (same rule as ``_explode_bom_net_first``'s explode_buckets).

    The previous implementation kept each child's frozen gross and added the
    parent's drift versus the snapshot bucket *net* — a stock+WIP-netted
    baseline. Comparing an after-stock current value against an after-stock-
    and-WIP baseline counted everything covered by open parent orders at
    snapshot time as new demand, inflating the whole component subtree of any
    WIP-covered parent on every reconcile cycle.

    Requirements the explosion no longer reaches get gross 0, so their net
    (and hence local order coverage) is zeroed downstream.
    """
    bom_level_by_item = {
        int(req.item_id): int(req.bom_level or 0)
        for req in requirements
    }
    current_gross = {
        int(req.item_id): _to_float(req.total_required_qty)
        for req in requirements
        if int(req.bom_level or 0) == 0
    }

    processed: set[int] = set()
    while True:
        pending_parent_ids = [
            int(item_id)
            for item_id in sorted(current_gross, key=lambda iid: (bom_level_by_item.get(int(iid), 0), int(iid)))
            if int(item_id) not in processed
        ]
        if not pending_parent_ids:
            break
        spec_by_parent = {
            int(row.item_id): int(row.spec_id)
            for row in (
                db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
                .filter(DefaultSpecification.item_id.in_(pending_parent_ids))
                .all()
            )
        }
        if not spec_by_parent:
            processed.update(pending_parent_ids)
            continue
        component_rows = (
            db.query(SpecComponent.spec_id, SpecComponent.item_id, SpecComponent.quantity)
            .filter(SpecComponent.spec_id.in_(list(spec_by_parent.values())))
            .all()
        )
        components_by_spec: Dict[int, List[tuple[int, float]]] = {}
        for spec_id, component_id, qty in component_rows:
            components_by_spec.setdefault(int(spec_id), []).append((int(component_id), _to_float(qty)))

        for parent_id in pending_parent_ids:
            processed.add(parent_id)
            spec_id = spec_by_parent.get(parent_id)
            if spec_id is None:
                continue
            children = components_by_spec.get(spec_id, [])
            if not children:
                continue
            parent_explode_qty = max(
                _to_float(current_gross.get(parent_id, 0.0)) - _to_float(stock_by_item.get(parent_id, 0.0)),
                0.0,
            )
            if parent_explode_qty <= EPS:
                continue
            parent_level = bom_level_by_item.get(parent_id, 0)
            for child_id, qty_per_unit in children:
                if qty_per_unit <= EPS:
                    continue
                child_level = bom_level_by_item.get(child_id)
                if child_level is not None and child_level <= parent_level:
                    continue
                if child_level is None:
                    bom_level_by_item[child_id] = parent_level + 1
                current_gross[child_id] = (
                    _to_float(current_gross.get(child_id, 0.0)) + parent_explode_qty * qty_per_unit
                )

    for req in requirements:
        current_gross.setdefault(int(req.item_id), 0.0)

    return current_gross, bom_level_by_item


def _ensure_reconciled_requirements(
    db: Session,
    run: PlanningRun,
    existing_requirements: List[MrpRequirement],
    current_gross_by_item: Dict[int, float],
    current_net_by_item: Dict[int, float],
    bom_level_by_item: Dict[int, int],
) -> Dict[int, MrpRequirement]:
    req_by_item: Dict[int, MrpRequirement] = {int(req.item_id): req for req in existing_requirements}
    for item_id in sorted(current_gross_by_item):
        if item_id in req_by_item:
            continue
        gross = _to_float(current_gross_by_item.get(item_id, 0.0))
        net = _to_float(current_net_by_item.get(item_id, 0.0))
        if gross <= EPS and net <= EPS:
            continue
        req = MrpRequirement(
            run_id=int(run.run_id),
            item_id=int(item_id),
            total_required_qty=gross,
            net_required_qty=net,
            covered_qty=0.0,
            remaining_qty=net,
            period_from=run.period_from,
            period_to=run.period_to,
            bom_level=bom_level_by_item.get(item_id, 0),
        )
        db.add(req)
        db.flush()
        db.add(
            MrpRequirementBucket(
                requirement_id=int(req.id),
                run_id=int(run.run_id),
                item_id=int(item_id),
                bucket_date=run.period_to or run.period_from or date.today(),
                gross_qty=gross,
                net_qty=net,
            )
        )
        req_by_item[item_id] = req
    return req_by_item


def _link_orphan_mrp_products_to_requirements(
    db: Session,
    run: PlanningRun,
    req_by_item: Dict[int, MrpRequirement],
) -> Dict[str, Any]:
    if not req_by_item:
        return {"linked": 0, "items": []}
    rows = (
        db.query(ProductionProduct, ProductionOrder)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .filter(ProductionOrder.source == "mrp")
        .filter(ProductionOrder.source_run_id == int(run.run_id))
        .filter(ProductionOrder.deletion_mark == False)
        .filter(ProductionProduct.item_id.in_(list(req_by_item)))
        .filter(ProductionProduct.source_mrp_requirement_id.is_(None))
        .all()
    )
    linked_items: set[int] = set()
    for product, _order in rows:
        req = req_by_item.get(int(product.item_id))
        if req is None:
            continue
        product.source_mrp_requirement_id = int(req.id)
        linked_items.add(int(product.item_id))
    return {"linked": len(rows), "items": sorted(linked_items)}

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

    snapshot_requirements = (
        db.query(MrpRequirement)
        .filter(MrpRequirement.run_id == int(run.run_id))
        .all()
    )
    if not snapshot_requirements:
        return {
            "run_id": int(run.run_id),
            "source_plan_id": int(run.source_plan_id),
            "status": "ok",
            "dry_run": bool(dry_run),
            "production_added": [],
            "purchase_added": [],
            "note": "в плане нет положительной потребности",
        }

    period_to = run.period_to or max((req.period_to for req in snapshot_requirements), default=date.today())

    # A fixed snapshot already contains the BOM explosion result in
    # MrpRequirement.total_required_qty. Reconciliation must not explode the
    # current plan again: doing so can reintroduce obsolete gross demand after
    # local MRP orders were reduced/cancelled. Re-anchor only the net side of the
    # frozen snapshot to current effective stock, then compare it with open WIP.
    stock_by_item = effective_stock_by_item_all(db)
    current_gross_by_item, bom_level_by_item = _current_snapshot_gross_by_item(db, snapshot_requirements, stock_by_item)
    current_net_by_item: Dict[int, float] = {}
    for iid in sorted(current_gross_by_item):
        current_net_by_item[iid] = max(
            _to_float(current_gross_by_item.get(iid, 0.0)) - _to_float(stock_by_item.get(iid, 0.0)),
            0.0,
        )

    req_by_item = _ensure_reconciled_requirements(
        db,
        run,
        snapshot_requirements,
        current_gross_by_item,
        current_net_by_item,
        bom_level_by_item,
    )
    orphan_link_repair = _link_orphan_mrp_products_to_requirements(db, run, req_by_item)
    item_ids = sorted(current_net_by_item)
    active_production_by_item = _active_production_qty_by_item(db, item_ids)
    items_by_id: Dict[int, Item] = {
        int(r.item_id): r
        for r in db.query(Item).filter(Item.item_id.in_(item_ids)).all()
    } if item_ids else {}

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
    now = datetime.now(timezone.utc)

    for iid in sorted(current_net_by_item.keys()):
        item = items_by_id.get(iid)
        if item is None:
            continue
        flow = classify_replenishment_flow(getattr(item, "replenishment_method", None))
        req = req_by_item.get(iid)
        current_net = _to_float(current_net_by_item.get(iid, 0.0))

        if req is not None:
            req.net_required_qty = current_net

        if flow == REPLENISHMENT_FLOW_PRODUCTION:
            open_qty = _to_float(active_production_by_item.get(iid, 0.0))
            if req is not None:
                req.covered_qty = min(open_qty, current_net)
                req.remaining_qty = max(current_net - _to_float(req.covered_qty), 0.0)
            gap = max(current_net - open_qty, 0.0)
            if gap <= EPS:
                continue
            entry = {
                "item_id": int(iid),
                "item_code": str(item.item_code or ""),
                "item_name": str(item.item_name or ""),
                "qty": round(gap, 6),
                "requirement_id": int(req.id) if req else None,
            }
            if not dry_run:
                products = _materialize_catchup_gap(db, run=run, item=item, req=req, gap=gap, now=now)
                _bump_requirement_coverage(req, gap)
                active_production_by_item[iid] = open_qty + gap
                entry["orders"] = [
                    {
                        "order_id": int(order.order_id),
                        "order_number": order.order_number,
                        "product_id": int(product.product_id),
                        "qty": round(_to_float(qty), 6),
                    }
                    for order, product, qty in products
                ]
                if products:
                    order, product, _qty = products[0]
                    entry["order_id"] = int(order.order_id)
                    entry["order_number"] = order.order_number
                    entry["product_id"] = int(product.product_id)
            production_added.append(entry)

        elif flow == REPLENISHMENT_FLOW_PURCHASE:
            if current_net <= EPS:
                if req is not None:
                    req.covered_qty = 0.0
                    req.remaining_qty = 0.0
                continue
            target = current_net
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
            if req is not None:
                req.covered_qty = min(current_net - max(gap, 0.0), current_net)
                req.remaining_qty = max(current_net - _to_float(req.covered_qty), 0.0)
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

    batch_repair = _split_oversized_catchup_batches(db, run, dry_run=dry_run, now=now)

    # Re-anchor every production line that is NOT yet open in 1C to a fresh
    # capacity-aware, child→parent-aware schedule starting today. Lines already
    # open in 1C stay where they are and pre-book their capacity.
    reschedule = _reschedule_run_journal(db, run, dry_run=dry_run)

    if dry_run:
        db.rollback()
    else:
        db.commit()

    mrp_order_repair = dedupe_mrp_production_orders(db, dry_run=dry_run)

    return {
        "run_id": int(run.run_id),
        "source_plan_id": int(run.source_plan_id),
        "status": "ok",
        "dry_run": bool(dry_run),
        "production_added": production_added,
        "purchase_added": purchase_added,
        "rescheduled": reschedule,
        "mrp_order_repair": mrp_order_repair,
        "mrp_batch_repair": batch_repair,
        "orphan_link_repair": orphan_link_repair,
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
    from .workshop_resolution import resolve_workshop_for_specs

    resource_id_by_spec: Dict[int, int] = resolve_workshop_for_specs(
        db, list(set(default_spec_by_item.values()))
    )

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
