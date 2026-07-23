"""Periodic MRP reconciliation — drift-correction + repair on fixed snapshots.

Why this exists
---------------
A period plan is fixed, an MRP snapshot is taken and frozen; production and
supplier orders are issued from it. From then on ``net_required_qty`` is the
single, immutable source of demand — the freeze owns it, reconcile never writes
it. Reconcile has exactly two jobs:

* **drift-correction sizing** — read the ledger the cycle persisted
  (``effective_net = max(net_required_qty + drift_adjustment_qty, 0)`` and
  ``executed_qty``) and top up / trim each requirement's OWN outstanding supply
  to ``desired_outstanding = max(effective_net − executed, 0)``:
    - production-flow items → a fresh internal catch-up order (journal line);
    - purchase-flow items → a fresh ``PlannedPurchase`` row in the same run.
* **repair** — dedupe, optimal-batch split, capacity reschedule, binding repair,
  orphan-link — structural hygiene independent of sizing.

There is NO re-explosion here: reconcile never re-derives gross demand from the
current ``SpecComponent`` / plan lines. Reality corrections flow ONLY through
``drift_adjustment_qty`` (a pure function of the immutable freeze baseline +
frozen norms + current facts, recomputed from scratch each ledger cycle).

Nothing is sent to 1C here — that stays a user action (see
``.docs/one_c_export_from_prodplan.md``).

Idempotency / anti-reinflation
------------------------------
The class of reinflation bugs is structurally impossible now: (1) re-explosion
is physically removed; (2) ``net_required_qty`` has no writer in reconcile;
(3) drift is a pure function that never reads its own result; (4) a first
reconcile materialises a gap G, which becomes own_open_coverage +G, so a second
reconcile over unchanged facts sees gap 0 → zero order churn (proposals never
enter drift, which is computed from stock/produced/received facts). A surplus is
capped by the open deficit (effective_net ≥ executed); a shortfall top-up is
capped by the frozen ``initial_snapshot_stock``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from ..models import (
    DefaultSpecification,
    DbrFeederSignal,
    Item,
    MrpFreezeAllocation,
    MrpRequirement,
    MrpRequirementBucket,
    Operation,
    PlannedPurchase,
    PlanningRun,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SyncLink,
    SpecComponent,
    SpecOperation,
    Specification,
    SupplierOrder,
    SupplierOrderItem,
)
from .capacity_scheduler import CapacityScheduler
from .period_plan_service import (
    _to_float,
)
from .production_control_journal import (
    _default_spec_id_for_item,
    _split_qty_by_optimal_batch,
    dedupe_mrp_production_orders,
)
from .production_binding_repair import repair_clean_mrp_bindings
from .mrp_execution_ledger import run_ledger_cycle
from .supplier_order_status import state_is_terminal as _supplier_order_is_terminal
from .replenishment import (
    REPLENISHMENT_FLOW_PRODUCTION,
    REPLENISHMENT_FLOW_PURCHASE,
    classify_replenishment_flow,
)
from .paint_weld_pairs import is_welded_blocked

EPS = 1e-9
FIXED_SNAPSHOT_STATUS = "FIXED_SNAPSHOT"
CLOSED_STATUS = "CLOSED"
PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"
PURCHASE_ORDER_ENTITY = "Document_ЗаказПоставщику"
DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"


def _production_supply_qty_expr():
    """Quantity still expected from an open production line."""
    return func.coalesce(ProductionProduct.remaining_qty, 0.0)


def _latest_active_snapshot_run_ids(db: Session) -> List[int]:
    """
    Latest FIXED_SNAPSHOT run per source plan. Activity is status-based, NOT
    period-gated: an overdue-but-open snapshot (june runs 13/14 with executed=0)
    stays active, aligned with the ledger scope (``_scope_run_ids`` is
    period-agnostic). A CLOSED run is excluded naturally (the ==FIXED_SNAPSHOT
    filter). There must be only one active fixed snapshot per plan; max(run_id)
    is a defensive fallback for legacy duplicates.
    """
    rows = (
        db.query(PlanningRun.source_plan_id, func.max(PlanningRun.run_id))
        .filter(PlanningRun.status == FIXED_SNAPSHOT_STATUS)
        .filter(PlanningRun.source_plan_id.isnot(None))
        .group_by(PlanningRun.source_plan_id)
        .all()
    )
    return [int(run_id) for _plan_id, run_id in rows if run_id is not None]


def _trim_unexported_planned_purchases(
    db: Session,
    *,
    run_id: int,
    item_id: int,
    target_qty: float,
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
    """Remove or shrink stale local MRP purchase recommendations down to target."""
    purchases = (
        db.query(PlannedPurchase)
        .filter(PlannedPurchase.run_id == int(run_id), PlannedPurchase.item_id == int(item_id))
        .order_by(PlannedPurchase.need_date.desc(), PlannedPurchase.purchase_id.desc())
        .all()
    )
    if not purchases:
        return None

    purchase_ids = [int(pp.purchase_id) for pp in purchases]
    exported_ids = {
        int(source_id)
        for (source_id,) in (
            db.query(SyncLink.source_id)
            .filter(
                SyncLink.source_system == "PRODPLAN",
                SyncLink.source_doctype == "planned_purchase",
                SyncLink.source_id.in_(purchase_ids),
                SyncLink.target_entity == "Document_ЗаказПоставщику",
                SyncLink.status == "success",
                SyncLink.target_ref_key.isnot(None),
            )
            .all()
        )
    }

    current_total = sum(_to_float(pp.qty) for pp in purchases)
    surplus = current_total - max(_to_float(target_qty), 0.0)
    if surplus <= EPS:
        return None

    removed_qty = 0.0
    removed_purchase_ids: List[int] = []
    reduced: List[Dict[str, Any]] = []

    for purchase in purchases:
        if surplus <= EPS:
            break
        purchase_id = int(purchase.purchase_id)
        if purchase_id in exported_ids:
            continue
        qty = _to_float(purchase.qty)
        if qty <= EPS:
            continue

        delta = min(qty, surplus)
        next_qty = max(qty - delta, 0.0)
        if next_qty <= EPS:
            removed_purchase_ids.append(purchase_id)
            if not dry_run:
                db.delete(purchase)
        else:
            reduced.append(
                {
                    "purchase_id": purchase_id,
                    "from_qty": round(qty, 6),
                    "to_qty": round(next_qty, 6),
                }
            )
            if not dry_run:
                purchase.qty = next_qty
                purchase.planned_qty = max(_to_float(purchase.planned_qty) - delta, 0.0)
                purchase.requested_qty = max(_to_float(purchase.requested_qty) - delta, 0.0)

        removed_qty += delta
        surplus -= delta

    if removed_qty <= EPS:
        return None

    return {
        "item_id": int(item_id),
        "target_qty": round(max(_to_float(target_qty), 0.0), 6),
        "removed_qty": round(removed_qty, 6),
        "removed_purchase_ids": removed_purchase_ids,
        "reduced": reduced,
        "exported_qty": round(
            sum(_to_float(pp.qty) for pp in purchases if int(pp.purchase_id) in exported_ids),
            6,
        ),
    }


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


def _own_open_production_by_item(
    db: Session,
    run: PlanningRun,
    open_req_ids: Set[int],
) -> Dict[int, float]:
    """Open production supply this run OWNS, per item (v2 §5).

    Own = a line linked to one of the run's open requirements
    (``source_mrp_requirement_id`` ∈ open_req_ids) OR a fresh MRP catch-up order
    of this run (``ProductionOrder.source == 'mrp'`` and
    ``source_run_id == run_id``). The frozen-WIP allocations of the active
    version (G2 set: ``source_type='wip_order'`` product lines) are EXCLUDED —
    their qty is already netted into ``net_required_qty`` at freeze time, so
    counting them here would double-cover the net and trim genuine catch-ups.

    The old ``_active_production_qty_by_item`` summed the WHOLE WIP for an item
    globally (every plan's orders) = a double count; this counts only own supply.
    """
    supply_qty = _production_supply_qty_expr()

    g2_product_ids: Set[int] = set()
    if run.active_freeze_version is not None:
        g2_product_ids = {
            int(pid)
            for (pid,) in (
                db.query(MrpFreezeAllocation.source_line_ref)
                .filter(MrpFreezeAllocation.run_id == int(run.run_id))
                .filter(MrpFreezeAllocation.freeze_version == int(run.active_freeze_version))
                .filter(MrpFreezeAllocation.source_type == "wip_order")
                .all()
            )
            if pid is not None and str(pid).isdigit()
        }

    own_filter = or_(
        ProductionProduct.source_mrp_requirement_id.in_(open_req_ids) if open_req_ids else False,
        and_(ProductionOrder.source == "mrp", ProductionOrder.source_run_id == int(run.run_id)),
    )
    rows = (
        db.query(
            ProductionProduct.item_id,
            ProductionProduct.product_id,
            supply_qty.label("qty"),
        )
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(own_filter)
        .filter(ProductionOrder.deletion_mark == False)
        .filter(func.lower(func.coalesce(ProductionOrder.order_state_key, "")) != DONE_STATE_KEY)
        .filter(supply_qty > 0)
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(("completed", "cancelled")))
        .all()
    )
    result: Dict[int, float] = {}
    for item_id, product_id, qty in rows:
        if int(product_id) in g2_product_ids:
            continue
        result[int(item_id)] = result.get(int(item_id), 0.0) + _to_float(qty)
    return result


def _dbr_owned_qty(
    db: Session,
    item_ids: Sequence[int],
    *,
    period_to: date,
) -> Dict[int, float]:
    """Open DBR journal quantity that may cover this MRP horizon.

    This is a conservative item-level bridge until explicit DBR↔MRP allocations
    exist: only positive, non-terminal remaining journal quantity linked to an
    active signal due no later than the run horizon is counted.
    """
    ids = sorted({int(item_id) for item_id in item_ids})
    if not ids:
        return {}
    remaining_qty = func.coalesce(
        ProductionProduct.remaining_qty,
        ProductionProduct.quantity - func.coalesce(ProductionProduct.produced_qty, 0),
        0,
    )
    signal_due = func.coalesce(
        DbrFeederSignal.required_date,
        DbrFeederSignal.need_date,
    )
    rows = (
        db.query(
            ProductionProduct.item_id,
            func.sum(remaining_qty).label("qty"),
        )
        .join(
            DbrFeederSignal,
            DbrFeederSignal.id == ProductionProduct.source_dbr_signal_id,
        )
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionProduct.item_id.in_(ids))
        .filter(DbrFeederSignal.status.in_(("Open", "Order Created", "In Work")))
        .filter(remaining_qty > 0)
        .filter(or_(signal_due.is_(None), signal_due <= period_to))
        .filter(
            func.coalesce(ProductionOrderLineState.status, "shortage").notin_(
                ("completed", "cancelled")
            )
        )
        .group_by(ProductionProduct.item_id)
        .all()
    )
    return {int(item_id): _to_float(qty) for item_id, qty in rows}


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
    exclude_item_ids: Optional[Set[int]] = None,
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
        if exclude_item_ids and int(product.item_id) in exclude_item_ids:
            continue
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
    for product, order in rows:
        req = req_by_item.get(int(product.item_id))
        if req is None:
            continue
        product.source_mrp_requirement_id = int(req.id)
        # Cap an oversized orphan line to the requirement's OUTSTANDING demand
        # (effective_net − executed), not to raw net: executed output already
        # satisfies part of the net, and drift_adjustment moves the target.
        req_qty = max(_effective_net(req, db) - _to_float(req.executed_qty), 0.0)
        if (
            req_qty > EPS
            and not order.order_ref1c
            and _to_float(product.produced_qty) <= EPS
            and _to_float(product.remaining_qty) > req_qty + EPS
        ):
            product.quantity = req_qty
            product.remaining_qty = req_qty
        linked_items.add(int(product.item_id))
    return {"linked": len(rows), "items": sorted(linked_items)}


def _effective_net(req: MrpRequirement, db: Optional[Session] = None) -> float:
    """Frozen net demand corrected by drift (v2 §8). The single demand target
    for sizing; reconcile never writes ``net_required_qty`` itself.

    Inc6 (design §11б): under STOCK_SOURCE=bin the demand target derives from the
    reservation ledger's ``uncovered`` + supplier term (same source as closure —
    :func:`item_ledger.reservation_ledger.effective_net_bin`) so an evaporated
    supply re-surfaces as a proposal exactly once (§7 ex4). Legacy stays
    byte-identical: with ``db`` None (or the flag off) the formula is unchanged."""
    if db is not None:
        from .item_ledger.config import use_bin_stock

        if use_bin_stock():
            from .item_ledger.reservation_ledger import effective_net_bin

            val = effective_net_bin(db, req)
            if val is not None:
                return val
    return max(_to_float(req.net_required_qty) + _to_float(req.drift_adjustment_qty), 0.0)

def _trim_unexported_catchup_production(
    db: Session,
    *,
    run: PlanningRun,
    item_id: int,
    target_qty: float,
    welded_blocked: Set[int],
    dry_run: bool,
) -> Optional[Dict[str, Any]]:
    """Shrink / delete this run's OWN unexported catch-up production down to
    ``target_qty`` (v2 §8, drift-driven surplus). Only own MRP lines that are not
    in 1C (``order_ref1c`` NULL, no success production-order SyncLink) and not
    yet produced (``produced_qty`` ≤ EPS) are touched, newest first
    (``product_id`` desc). A line fully removed drops its order+product+state; a
    partially trimmed line has its qty reduced. Welded-pair items are skipped."""
    if int(item_id) in welded_blocked:
        return None
    rows = (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionOrder.source == "mrp")
        .filter(ProductionOrder.source_run_id == int(run.run_id))
        .filter(ProductionProduct.item_id == int(item_id))
        .filter(ProductionOrder.deletion_mark == False)
        .filter(ProductionOrder.order_ref1c.is_(None))
        .filter(func.coalesce(ProductionProduct.produced_qty, 0) <= EPS)
        .filter(func.coalesce(ProductionProduct.remaining_qty, 0) > EPS)
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(("completed", "cancelled")))
        .order_by(ProductionProduct.product_id.desc())
        .all()
    )
    if not rows:
        return None

    order_ids = [int(o.order_id) for _p, o, _s in rows]
    exported_order_ids = {
        int(source_id)
        for (source_id,) in (
            db.query(SyncLink.source_id)
            .filter(
                SyncLink.source_system == "PRODPLAN",
                SyncLink.source_doctype == "production_order",
                SyncLink.source_id.in_(order_ids),
                SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
                SyncLink.status == "success",
                SyncLink.target_ref_key.isnot(None),
            )
            .all()
        )
    }
    trimmable = [
        (pp, order, state)
        for pp, order, state in rows
        if int(order.order_id) not in exported_order_ids
    ]
    current_total = sum(_to_float(pp.remaining_qty) for pp, _o, _s in trimmable)
    surplus = current_total - max(_to_float(target_qty), 0.0)
    if surplus <= EPS:
        return None

    removed_qty = 0.0
    removed_product_ids: List[int] = []
    reduced: List[Dict[str, Any]] = []
    for pp, order, state in trimmable:
        if surplus <= EPS:
            break
        qty = _to_float(pp.remaining_qty)
        if qty <= EPS:
            continue
        delta = min(qty, surplus)
        next_qty = max(qty - delta, 0.0)
        if next_qty <= EPS:
            removed_product_ids.append(int(pp.product_id))
            if not dry_run:
                if state is not None:
                    db.delete(state)
                db.delete(pp)
                db.flush()
                remaining_lines = (
                    db.query(func.count(ProductionProduct.product_id))
                    .filter(ProductionProduct.order_id == int(order.order_id))
                    .scalar()
                )
                if not remaining_lines:
                    db.delete(order)
        else:
            reduced.append(
                {
                    "product_id": int(pp.product_id),
                    "from_qty": round(qty, 6),
                    "to_qty": round(next_qty, 6),
                }
            )
            if not dry_run:
                pp.quantity = next_qty
                pp.remaining_qty = next_qty
        removed_qty += delta
        surplus -= delta

    if removed_qty <= EPS:
        return None
    return {
        "item_id": int(item_id),
        "target_qty": round(max(_to_float(target_qty), 0.0), 6),
        "removed_qty": round(removed_qty, 6),
        "removed_product_ids": removed_product_ids,
        "reduced": reduced,
    }


def _own_purchase_coverage(
    db: Session, run: PlanningRun
) -> tuple[Set[int], Dict[int, float], Dict[int, float]]:
    """Own purchase coverage for a run (v2 §5).

    Returns ``(exported_pp_ids, unexported_pp_qty, own_exported_outstanding)``:

    * ``exported_pp_ids`` — PlannedPurchase ids of this run pushed to 1C
      (success SyncLink);
    * ``unexported_pp_qty[item_id]`` — Σ qty of this run's PlannedPurchase rows
      NOT yet exported (still trimmable local recommendations);
    * ``own_exported_outstanding[item_id]`` — Σ max(quantity − received, 0) over
      the non-terminal, non-deleted supplier orders those exported rows created.
      Received is already counted in executed (direct), so coverage+executed does
      not double-count.
    """
    purchases = (
        db.query(PlannedPurchase)
        .filter(PlannedPurchase.run_id == int(run.run_id))
        .all()
    )
    unexported_pp_qty: Dict[int, float] = {}
    own_exported_outstanding: Dict[int, float] = {}
    if not purchases:
        return set(), unexported_pp_qty, own_exported_outstanding

    purchase_ids = [int(pp.purchase_id) for pp in purchases]
    ref_by_purchase: Dict[int, str] = {
        int(source_id): str(ref_key)
        for source_id, ref_key in (
            db.query(SyncLink.source_id, SyncLink.target_ref_key)
            .filter(
                SyncLink.source_system == "PRODPLAN",
                SyncLink.source_doctype == "planned_purchase",
                SyncLink.source_id.in_(purchase_ids),
                SyncLink.target_entity == PURCHASE_ORDER_ENTITY,
                SyncLink.status == "success",
                SyncLink.target_ref_key.isnot(None),
            )
            .all()
        )
        if ref_key
    }
    exported_pp_ids = set(ref_by_purchase)
    for pp in purchases:
        if int(pp.purchase_id) in exported_pp_ids:
            continue
        iid = int(pp.item_id)
        unexported_pp_qty[iid] = unexported_pp_qty.get(iid, 0.0) + _to_float(pp.qty)

    refs = {ref for ref in ref_by_purchase.values() if ref}
    if refs:
        for soi, order in (
            db.query(SupplierOrderItem, SupplierOrder)
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(SupplierOrder.order_ref1c.in_(list(refs)))
            .filter(SupplierOrder.deletion_mark == False)
            .all()
        ):
            if _supplier_order_is_terminal(order.order_state_name):
                continue
            iid = int(soi.item_id_ref)
            outstanding = max(_to_float(soi.quantity) - _to_float(soi.received_qty), 0.0)
            if outstanding > EPS:
                own_exported_outstanding[iid] = own_exported_outstanding.get(iid, 0.0) + outstanding
    return exported_pp_ids, unexported_pp_qty, own_exported_outstanding


def _run_repairs(
    db: Session,
    run: PlanningRun,
    *,
    dry_run: bool,
    manage_tx: bool,
    welded_blocked: Set[int],
    now: datetime,
) -> Dict[str, Any]:
    """Structural repairs (independent of drift sizing): optimal-batch split,
    binding repair, capacity reschedule, then commit/rollback (if this call owns
    the transaction) and the cross-run dedupe."""
    batch_repair = _split_oversized_catchup_batches(
        db, run, dry_run=dry_run, now=now, exclude_item_ids=welded_blocked
    )
    binding_repair = (
        {"checked": 0, "spec_updated": 0, "workshop_auto_cleared": 0, "local_issues_deleted": 0, "blocked": {}}
        if dry_run
        else repair_clean_mrp_bindings(db, run_id=int(run.run_id))
    )
    reschedule = _reschedule_run_journal(
        db, run, dry_run=dry_run, exclude_item_ids=welded_blocked
    )
    if manage_tx:
        if dry_run:
            db.rollback()
        else:
            db.commit()
    mrp_order_repair = dedupe_mrp_production_orders(db, dry_run=dry_run)
    return {
        "rescheduled": reschedule,
        "mrp_order_repair": mrp_order_repair,
        "mrp_batch_repair": batch_repair,
        "binding_repair": binding_repair,
    }


def reconcile_snapshot(
    db: Session,
    run_id: int,
    *,
    dry_run: bool = False,
    ledger_cycle_ran: bool = False,
    manage_tx: bool = True,
) -> Dict[str, Any]:
    """Drift-correct one FIXED_SNAPSHOT run and repair its journal (v2 §8).

    Reads the ledger the cycle persisted (``effective_net`` = frozen net + drift
    adjustment, ``executed_qty``) and sizes each requirement's OWN outstanding
    supply to ``desired_outstanding``. Never re-explodes demand, never writes
    ``net_required_qty``. Returns a per-item summary of what was added / trimmed.
    """
    run = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).one_or_none()
    if run is None:
        raise ValueError(f"run_id={run_id}: прогон не найден")
    if str(run.status or "") != FIXED_SNAPSHOT_STATUS:
        raise ValueError(f"run_id={run_id}: не FIXED_SNAPSHOT (status={run.status})")
    if run.source_plan_id is None:
        raise ValueError(f"run_id={run_id}: прогон не привязан к плану периода")

    now = datetime.now(timezone.utc)
    open_reqs = (
        db.query(MrpRequirement)
        .filter(MrpRequirement.run_id == int(run.run_id))
        .filter(MrpRequirement.status == "open")
        .order_by(MrpRequirement.item_id.asc())
        .all()
    )
    if not open_reqs:
        return {
            "run_id": int(run.run_id),
            "source_plan_id": int(run.source_plan_id),
            "status": "ok",
            "dry_run": bool(dry_run),
            "production_added": [],
            "purchase_added": [],
            "purchase_pruned": [],
            "production_trimmed": [],
            "dbr_owned_skipped": [],
            "note": "в плане нет открытой потребности",
        }

    req_by_item: Dict[int, MrpRequirement] = {int(r.item_id): r for r in open_reqs}
    open_req_ids: Set[int] = {int(r.id) for r in open_reqs}
    item_ids = sorted(req_by_item)
    items_by_id: Dict[int, Item] = {
        int(r.item_id): r
        for r in db.query(Item).filter(Item.item_id.in_(item_ids)).all()
    }
    welded_blocked = is_welded_blocked(db, item_ids)
    orphan_link_repair = _link_orphan_mrp_products_to_requirements(db, run, req_by_item)

    # needs_freeze: an un-frozen run has no authoritative net to size against —
    # run only the structural repairs (a deploy prerequisite is a full-area
    # refreeze). No sizing, no trim.
    if run.active_freeze_version is None:
        repairs = _run_repairs(
            db, run, dry_run=dry_run, manage_tx=manage_tx,
            welded_blocked=welded_blocked, now=now,
        )
        return {
            "run_id": int(run.run_id),
            "source_plan_id": int(run.source_plan_id),
            "status": "needs_freeze",
            "dry_run": bool(dry_run),
            "production_added": [],
            "purchase_added": [],
            "purchase_pruned": [],
            "production_trimmed": [],
            "dbr_owned_skipped": [],
            "orphan_link_repair": orphan_link_repair,
            "effective_net_total": 0.0,
            "drift_adjust_total": 0.0,
            **repairs,
        }

    # The ledger cycle populates executed_qty + drift_adjustment_qty; run it now
    # unless the caller (reconcile_all_active) already ran it for the whole scope.
    if not ledger_cycle_ran:
        run_ledger_cycle(db)

    period_to = run.period_to or max((r.period_to for r in open_reqs), default=date.today())
    own_open_production = _own_open_production_by_item(db, run, open_req_ids)
    dbr_owned_qty = _dbr_owned_qty(db, item_ids, period_to=period_to)
    _exported_pp_ids, unexported_pp_qty, own_exported_outstanding = _own_purchase_coverage(db, run)

    production_added: List[Dict[str, Any]] = []
    purchase_added: List[Dict[str, Any]] = []
    purchase_pruned: List[Dict[str, Any]] = []
    production_trimmed: List[Dict[str, Any]] = []
    dbr_owned_skipped: List[Dict[str, Any]] = []
    effective_net_total = 0.0
    drift_adjust_total = 0.0

    for iid in item_ids:
        item = items_by_id.get(iid)
        req = req_by_item.get(iid)
        if item is None or req is None:
            continue
        flow = classify_replenishment_flow(getattr(item, "replenishment_method", None))
        eff_net = _effective_net(req, db)
        executed = _to_float(req.executed_qty)
        desired = max(eff_net - executed, 0.0)
        effective_net_total += eff_net
        drift_adjust_total += _to_float(req.drift_adjustment_qty)

        # A production/rework-flow item must not carry planned purchases — trim
        # any stale local purchase recommendation to zero (kept unconditionally).
        if flow != REPLENISHMENT_FLOW_PURCHASE and unexported_pp_qty.get(iid, 0.0) > EPS:
            pruned = _trim_unexported_planned_purchases(
                db, run_id=int(run.run_id), item_id=iid, target_qty=0.0, dry_run=dry_run,
            )
            if pruned:
                purchase_pruned.append(pruned)
                unexported_pp_qty[iid] = max(
                    unexported_pp_qty.get(iid, 0.0) - _to_float(pruned.get("removed_qty")), 0.0
                )

        if flow == REPLENISHMENT_FLOW_PRODUCTION:
            dbr_cov = min(_to_float(dbr_owned_qty.get(iid, 0.0)), desired)
            if dbr_cov > EPS:
                dbr_owned_skipped.append({
                    "item_id": int(iid),
                    "item_code": str(item.item_code or ""),
                    "item_name": str(item.item_name or ""),
                    "requirement_id": int(req.id),
                    "qty": round(dbr_cov, 6),
                    "reason": "active_dbr_remaining_qty_applied_before_mrp",
                })
            own_cov = _to_float(own_open_production.get(iid, 0.0))
            gap = desired - dbr_cov - own_cov
            if iid in welded_blocked:
                # Welded pair: catch-up is issued through the paint chain, not
                # here. No materialise / no trim.
                pass
            elif gap > EPS:
                entry = {
                    "item_id": int(iid),
                    "item_code": str(item.item_code or ""),
                    "item_name": str(item.item_name or ""),
                    "qty": round(gap, 6),
                    "requirement_id": int(req.id),
                }
                if not dry_run:
                    products = _materialize_catchup_gap(
                        db, run=run, item=item, req=req, gap=gap, now=now
                    )
                    own_cov += gap
                    own_open_production[iid] = own_cov
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
            elif gap < -EPS:
                trimmed = _trim_unexported_catchup_production(
                    db, run=run, item_id=iid, target_qty=max(desired - dbr_cov, 0.0),
                    welded_blocked=welded_blocked, dry_run=dry_run,
                )
                if trimmed:
                    production_trimmed.append(trimmed)
                    own_cov = max(own_cov - _to_float(trimmed.get("removed_qty")), 0.0)
                    own_open_production[iid] = own_cov
            covered = min(executed + dbr_cov + own_cov, eff_net)
            req.covered_qty = covered
            req.remaining_qty = max(eff_net - covered, 0.0)

        elif flow == REPLENISHMENT_FLOW_PURCHASE:
            own_cov = _to_float(unexported_pp_qty.get(iid, 0.0)) + _to_float(
                own_exported_outstanding.get(iid, 0.0)
            )
            gap = desired - own_cov
            if gap > EPS:
                lead_time = int(getattr(item, "replenishment_time", 0) or 0)
                need_date = period_to
                order_date = max(date.today(), need_date - timedelta(days=lead_time))
                entry = {
                    "item_id": int(iid),
                    "item_code": str(item.item_code or ""),
                    "item_name": str(item.item_name or ""),
                    "qty": round(gap, 6),
                    "requirement_id": int(req.id),
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
                            source_mrp_requirement_id=int(req.id),
                        )
                    )
                    unexported_pp_qty[iid] = _to_float(unexported_pp_qty.get(iid, 0.0)) + gap
                    own_cov += gap
                purchase_added.append(entry)
            elif gap < -EPS:
                target = max(desired - _to_float(own_exported_outstanding.get(iid, 0.0)), 0.0)
                pruned = _trim_unexported_planned_purchases(
                    db, run_id=int(run.run_id), item_id=iid, target_qty=target, dry_run=dry_run,
                )
                if pruned:
                    purchase_pruned.append(pruned)
                    unexported_pp_qty[iid] = max(
                        _to_float(unexported_pp_qty.get(iid, 0.0)) - _to_float(pruned.get("removed_qty")),
                        0.0,
                    )
                    own_cov = _to_float(unexported_pp_qty.get(iid, 0.0)) + _to_float(
                        own_exported_outstanding.get(iid, 0.0)
                    )
            covered = min(executed + own_cov, eff_net)
            req.covered_qty = covered
            req.remaining_qty = max(eff_net - covered, 0.0)
        # rework flow is intentionally not auto-topped-up.

    repairs = _run_repairs(
        db, run, dry_run=dry_run, manage_tx=manage_tx,
        welded_blocked=welded_blocked, now=now,
    )

    return {
        "run_id": int(run.run_id),
        "source_plan_id": int(run.source_plan_id),
        "status": "ok",
        "dry_run": bool(dry_run),
        "production_added": production_added,
        "purchase_added": purchase_added,
        "purchase_pruned": purchase_pruned,
        "production_trimmed": production_trimmed,
        "dbr_owned_skipped": dbr_owned_skipped,
        "orphan_link_repair": orphan_link_repair,
        "effective_net_total": round(effective_net_total, 6),
        "drift_adjust_total": round(drift_adjust_total, 6),
        **repairs,
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


def _reschedule_run_journal(
    db: Session,
    run: PlanningRun,
    *,
    dry_run: bool,
    exclude_item_ids: Optional[Set[int]] = None,
) -> Dict[str, Any]:
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
    if exclude_item_ids:
        rows = [row for row in rows if int(row[0].item_id) not in exclude_item_ids]
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
        state.workshop_id_source = "auto" if state.workshop_id is not None else None
        state.workshop_id_set_at = datetime.now(timezone.utc) if state.workshop_id is not None else None
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
    """Reconcile the latest snapshot of every plan whose period is still open.

    Composite cycle (v2 §6): the execution ledger (verify → executed → drift →
    drift_adjustment) is rebuilt ONCE for the whole canonical scope BEFORE the
    per-run sizing loop, so every run sizes against a freshly persisted
    executed/drift ledger. The scope is re-derived inside run_ledger_cycle (last
    FIXED_SNAPSHOT per plan, NO period filter, plus CLOSED-with-open-req), so it
    is not parameterised by ``run_ids`` here.
    """
    # 1) Ledger cycle first — persist executed_qty + drift_adjustment_qty for the
    # scope. On a non-dry run commit it so the per-run sizers read committed
    # facts; a dry run keeps it in the session and rolls the whole thing back at
    # the end.
    execution_ledger: Dict[str, Any]
    try:
        execution_ledger = run_ledger_cycle(db)
        if not dry_run:
            db.commit()
    except Exception as exc:  # noqa: BLE001 — never let ledger population break reconcile
        db.rollback()
        execution_ledger = {"status": "error", "error": str(exc)}

    # 2) Per-run drift-correction sizing. The ledger already ran, so pass
    # ledger_cycle_ran=True; on a non-dry run each snapshot owns its own commit,
    # on a dry run the outer rollback below is authoritative (manage_tx=False).
    run_ids = _latest_active_snapshot_run_ids(db)
    results: List[Dict[str, Any]] = []
    total_production = 0
    total_purchase = 0
    total_purchase_pruned = 0
    total_production_trimmed = 0
    for rid in run_ids:
        try:
            res = reconcile_snapshot(
                db, rid, dry_run=dry_run, ledger_cycle_ran=True, manage_tx=not dry_run
            )
            total_production += len(res.get("production_added", []))
            total_purchase += len(res.get("purchase_added", []))
            total_purchase_pruned += len(res.get("purchase_pruned", []))
            total_production_trimmed += len(res.get("production_trimmed", []))
            results.append(res)
        except Exception as exc:  # noqa: BLE001 — isolate one bad run from the rest
            db.rollback()
            results.append({"run_id": int(rid), "status": "error", "error": str(exc)})

    if dry_run:
        db.rollback()

    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "runs_checked": len(run_ids),
        "production_lines_added": total_production,
        "purchase_lines_added": total_purchase,
        "purchase_lines_pruned": total_purchase_pruned,
        "production_lines_trimmed": total_production_trimmed,
        "execution_ledger": execution_ledger,
        "results": results,
    }


# ---------------------------------------------------------------------------
# §5 (increment 5) — manual force-close / reopen
# ---------------------------------------------------------------------------
def force_close_run(db: Session, run_id: int, *, dry_run: bool = False) -> Dict[str, Any]:
    """Manually close an under-executed FIXED_SNAPSHOT run (business decision).

    The remaining demand is ABANDONED, never carried: each open requirement is
    stamped closed and its OWN unexported MRP purchase proposals are trimmed to
    zero. Exported-to-1C proposals (a successful ``SyncLink``) are left intact —
    the operator cancels those in 1C (the boundary: proposals go out, cancellation
    is manual). Production lines are 1C-owned / journal-driven and are NOT touched
    here (shop-floor risk; a separate operation). Idempotent on an already-CLOSED
    run. Owns its own transaction: commit on success, rollback on dry_run.
    """
    run = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).one_or_none()
    if run is None:
        raise ValueError(f"run_id={run_id}: прогон не найден")

    if str(run.status or "") == CLOSED_STATUS:
        return {
            "status": "already_closed",
            "run_id": int(run.run_id),
            "dry_run": bool(dry_run),
            "requirements_closed": 0,
            "purchases_pruned": [],
        }
    if str(run.status or "") != FIXED_SNAPSHOT_STATUS:
        raise ValueError(
            f"run_id={run_id}: нельзя force-close (status={run.status}, ожидался FIXED_SNAPSHOT)"
        )

    now = datetime.now(timezone.utc)
    open_reqs = (
        db.query(MrpRequirement)
        .filter(MrpRequirement.run_id == int(run.run_id))
        .filter(MrpRequirement.status == "open")
        .order_by(MrpRequirement.id.asc())
        .all()
    )

    # Abandon the unexported purchase proposals of every open requirement's item.
    purchases_pruned: List[Dict[str, Any]] = []
    for req in open_reqs:
        pruned = _trim_unexported_planned_purchases(
            db,
            run_id=int(run.run_id),
            item_id=int(req.item_id),
            target_qty=0.0,
            dry_run=dry_run,
        )
        if pruned is not None:
            purchases_pruned.append(pruned)

    # Forced closure: the remainder is dropped, not carried.
    for req in open_reqs:
        req.status = "closed"
        req.closed_at = now
    run.status = CLOSED_STATUS
    run.finished_at = now

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "status": "closed",
        "run_id": int(run_id),
        "dry_run": bool(dry_run),
        "requirements_closed": len(open_reqs),
        "purchases_pruned": purchases_pruned,
    }


def reopen_run(db: Session, run_id: int, *, dry_run: bool = False) -> Dict[str, Any]:
    """Undo an erroneous / premature closure — the operator will keep executing.

    A CLOSED run is flipped back to FIXED_SNAPSHOT (``finished_at`` cleared) and
    its closed requirements are explicitly reopened so they return to the ledger
    scope; the next ledger cycle recomputes their executed/drift and re-closes
    any that are still satisfied (auto-reopen in §1 is off by design). Without a
    carry there is no successor holding an overlapping claim, so reopen is safe.
    Owns its own transaction: commit on success, rollback on dry_run.
    """
    run = db.query(PlanningRun).filter(PlanningRun.run_id == int(run_id)).one_or_none()
    if run is None:
        raise ValueError(f"run_id={run_id}: прогон не найден")
    if str(run.status or "") != CLOSED_STATUS:
        raise ValueError(
            f"run_id={run_id}: не CLOSED (status={run.status}), реопен невозможен"
        )

    run.status = FIXED_SNAPSHOT_STATUS
    run.finished_at = None

    closed_reqs = (
        db.query(MrpRequirement)
        .filter(MrpRequirement.run_id == int(run.run_id))
        .filter(MrpRequirement.status == "closed")
        .all()
    )
    for req in closed_reqs:
        req.status = "open"
        req.closed_at = None

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "status": "reopened",
        "run_id": int(run_id),
        "dry_run": bool(dry_run),
        "requirements_reopened": len(closed_reqs),
    }
