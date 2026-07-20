"""Populate MrpRequirement.executed_qty from actual production/receipt facts.

Fixed-MRP execution ledger, PHASE 2. This module recomputes, from scratch on
every run, how much of each open MRP requirement has actually been executed —
produced (assembled) or received (purchased) — and stores it on
``MrpRequirement.executed_qty``.

Why recompute-aggregate instead of event-increment
---------------------------------------------------
``ProductionProduct.produced_qty`` (production_order_sync) and
``SupplierOrderItem.received_qty`` (supplier_order_sync) are OVERWRITTEN
snapshots synced from 1C, not append-only events. Incrementing on each sync
would double-count. So we zero ``executed_qty`` and re-derive it wholesale each
run. The computation never reads ``executed_qty`` as input, which makes it
idempotent: running twice yields identical values.

Global FIFO
-----------
Production/receipt facts that are not linked to a specific requirement form a
per-item pool (a 1C production order or a supplier receipt has no
``source_mrp_requirement_id``). That pool is distributed FIFO across ALL open
requirements for the item, oldest plan first (by the owning run's period), so a
newer plan can never look executed while an older plan for the same item is
still open. This mirrors the read-time FIFO in
``period_plan_service.get_period_plan_execution_journal`` but persists the
result across every active plan at once (the pool is per-item, spanning plans).

Nothing reads ``executed_qty`` yet (requirement closure is a later phase), so
populating it is a near-zero behavior change.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (
    MrpRequirement,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    PlanningRun,
    SupplierOrder,
    SupplierOrderItem,
)
from .period_plan_service import _to_float
from .supplier_order_status import state_counts_in_mrp as _supplier_order_counts_in_mrp

EPS = 1e-9
_CANCELLED = "cancelled"


def _min_produced(quantity: Any, produced: Any) -> float:
    """Executed output of one production line, clamped to its own quantity."""
    qty = _to_float(quantity)
    done = max(0.0, _to_float(produced))
    return min(qty, done)


def populate_executed_qty(
    db: Session,
    run_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Recompute ``MrpRequirement.executed_qty`` for the given active runs.

    Operates GLOBALLY across ``run_ids`` because the unlinked production and
    receipt pools are per-item and span plans. Only ``status='open'``
    requirements are populated. Does NOT commit — the caller owns the
    transaction (matching how reconciliation persists/rolls back).

    Returns a summary ``{"runs", "items_touched", "total_executed"}``.
    """
    if run_ids is None:
        # Local import to avoid a circular import at module load time.
        from .mrp_reconciliation import _latest_active_snapshot_run_ids

        run_ids = _latest_active_snapshot_run_ids(db)

    run_ids = [int(rid) for rid in run_ids]
    if not run_ids:
        return {"runs": [], "items_touched": 0, "total_executed": 0.0}

    # --- Step 1: load open requirements, group by item, zero executed_qty. ---
    requirements: List[MrpRequirement] = (
        db.query(MrpRequirement)
        .filter(MrpRequirement.run_id.in_(run_ids))
        .filter(MrpRequirement.status == "open")
        .all()
    )
    if not requirements:
        return {"runs": run_ids, "items_touched": 0, "total_executed": 0.0}

    for req in requirements:
        req.executed_qty = 0.0

    reqs_by_item: Dict[int, List[MrpRequirement]] = {}
    for req in requirements:
        reqs_by_item.setdefault(int(req.item_id), []).append(req)

    req_ids = [int(req.id) for req in requirements]
    item_ids = sorted(reqs_by_item.keys())

    # Run periods drive the FIFO order (oldest plan first).
    run_period: Dict[int, tuple] = {
        int(run.run_id): (run.period_from, run.period_to)
        for run in db.query(PlanningRun).filter(PlanningRun.run_id.in_(run_ids)).all()
    }

    # --- Step 2: PASS A — direct (linked) production. ---
    # For each production line linked to one of our requirements and not
    # cancelled, accumulate min(produced, quantity); then cap the requirement's
    # executed_qty at its net. Overflow on a linked line is NOT redistributed.
    direct_sum_by_req: Dict[int, float] = {}
    linked_rows = (
        db.query(ProductionProduct, ProductionOrderLineState.status)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionProduct.source_mrp_requirement_id.in_(req_ids))
        .all()
    )
    for product, state_status in linked_rows:
        if str(state_status or "").strip().lower() == _CANCELLED:
            continue
        rid = int(product.source_mrp_requirement_id)
        direct_sum_by_req[rid] = direct_sum_by_req.get(rid, 0.0) + _min_produced(
            product.quantity, product.produced_qty
        )

    for req in requirements:
        rid = int(req.id)
        direct = direct_sum_by_req.get(rid, 0.0)
        if direct <= EPS:
            continue
        req.executed_qty = min(direct, _to_float(req.net_required_qty))

    # --- Step 3: PASS B — unlinked 1C production pool per item. ---
    pool_by_item: Dict[int, float] = {}
    unlinked_rows = (
        db.query(ProductionProduct, ProductionOrderLineState.status)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(
            ProductionOrderLineState,
            ProductionOrderLineState.product_id == ProductionProduct.product_id,
        )
        .filter(ProductionProduct.item_id.in_(item_ids))
        .filter(ProductionProduct.source_mrp_requirement_id.is_(None))
        .filter(ProductionOrder.source == "1c")
        .filter(ProductionOrder.deletion_mark.is_(False))
        .all()
    )
    for product, state_status in unlinked_rows:
        if str(state_status or "").strip().lower() == _CANCELLED:
            continue
        iid = int(product.item_id)
        pool_by_item[iid] = pool_by_item.get(iid, 0.0) + _min_produced(
            product.quantity, product.produced_qty
        )

    # --- Step 4: PASS C — receipt pool per item. ---
    receipt_rows = (
        db.query(
            SupplierOrderItem.item_id_ref,
            SupplierOrderItem.received_qty,
            SupplierOrder.order_state_name,
        )
        .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
        .filter(SupplierOrderItem.item_id_ref.in_(item_ids))
        .filter(SupplierOrder.deletion_mark.is_(False))
        .all()
    )
    for iid, received_qty, state_name in receipt_rows:
        if not _supplier_order_counts_in_mrp(state_name):
            continue
        received = _to_float(received_qty)
        if received <= EPS:
            continue
        pool_by_item[int(iid)] = pool_by_item.get(int(iid), 0.0) + received

    # --- Step 5: FIFO-distribute the per-item pool across open requirements. ---
    def _sort_key(req: MrpRequirement) -> tuple:
        period_from, period_to = run_period.get(int(req.run_id), (None, None))
        return (
            period_from or date.min,
            period_to or date.max,
            int(req.run_id),
            int(req.bom_level or 0),
            int(req.id),
        )

    for iid, reqs in reqs_by_item.items():
        pool = pool_by_item.get(iid, 0.0)
        if pool <= EPS:
            continue
        for req in sorted(reqs, key=_sort_key):
            capacity = max(0.0, _to_float(req.net_required_qty) - _to_float(req.executed_qty))
            if capacity <= EPS:
                continue
            take = min(pool, capacity)
            if take <= EPS:
                continue
            req.executed_qty = _to_float(req.executed_qty) + take
            pool -= take
            if pool <= EPS:
                break

    # --- Step 6: summary. ---
    total_executed = 0.0
    items_touched = 0
    for iid, reqs in reqs_by_item.items():
        item_total = sum(_to_float(req.executed_qty) for req in reqs)
        total_executed += item_total
        if item_total > EPS:
            items_touched += 1

    return {
        "runs": run_ids,
        "items_touched": items_touched,
        "total_executed": round(total_executed, 6),
    }
