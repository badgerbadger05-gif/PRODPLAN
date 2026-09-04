"""Reconcile safe local executor orders with a new MRP obligation snapshot.

The MRP run owns the obligation, while an order already exported to 1C or
touched by execution is an external commitment.  During obligation refresh we
may therefore resize only a completely local, single-line order.  Locked work
is preserved and the normal production-journal netting exposes any positive
delta as another proposal.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app import models
from app.services.item_ledger.production_output_cache import (
    accepted_product_output,
    update_accepted_product_output_cache,
)
from app.services.one_c_production_order_export import PRODUCTION_ORDER_ENTITY


_EPSILON = Decimal("0.000001")


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _set_quantity(product: models.ProductionProduct, quantity: Decimal) -> None:
    product.quantity = quantity
    update_accepted_product_output_cache(product, produced_qty=Decimal("0"))


def reconcile_local_mrp_orders(
    db: Session,
    *,
    ledger_generation_id: int,
    live_run_ids: Iterable[int],
) -> dict[str, Any]:
    """Resize untouched local MRP orders to the current ``plan + item`` target.

    The caller owns the transaction.  This function is invoked while the new
    generation is still BUILDING, before its custody and journal snapshots are
    persisted, so readers can never observe half-reconciled truth.
    """
    generation = db.get(models.LedgerGeneration, int(ledger_generation_id))
    if generation is None or str(generation.status) != "building":
        raise ValueError("local MRP order reconciliation requires a BUILDING generation")

    run_ids = sorted({int(value) for value in live_run_ids})
    if not run_ids:
        return {
            "ledger_generation_id": int(generation.id),
            "resized": 0,
            "cancelled": 0,
            "locked": 0,
            "entries": [],
        }

    live_runs = (
        db.query(models.PlanningRun)
        .filter(models.PlanningRun.run_id.in_(run_ids))
        .all()
    )
    if len(live_runs) != len(run_ids):
        raise ValueError("local MRP order reconciliation has missing live runs")
    live_plan_ids = {
        int(run.source_plan_id)
        for run in live_runs
        if run.source_plan_id is not None
    }
    if not live_plan_ids:
        return {
            "ledger_generation_id": int(generation.id),
            "resized": 0,
            "cancelled": 0,
            "locked": 0,
            "entries": [],
        }

    targets: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for plan_id, item_id, remaining in (
        db.query(
            models.ReplenishmentWorkItem.plan_id,
            models.ReplenishmentWorkItem.item_id,
            models.ReplenishmentWorkItem.replenishment_remaining_qty,
        )
        .filter(
            models.ReplenishmentWorkItem.ledger_generation_id
            == int(generation.id),
            models.ReplenishmentWorkItem.run_id.in_(run_ids),
            models.ReplenishmentWorkItem.replenishment_method == "make",
        )
        .all()
    ):
        targets[(int(plan_id), int(item_id))] += _decimal(remaining)

    rows = (
        db.query(
            models.ProductionProduct,
            models.ProductionOrder,
            models.PlanningRun,
        )
        .join(
            models.ProductionOrder,
            models.ProductionOrder.order_id == models.ProductionProduct.order_id,
        )
        .join(
            models.PlanningRun,
            models.PlanningRun.run_id == models.ProductionOrder.source_run_id,
        )
        .options(selectinload(models.ProductionProduct.control_state))
        .filter(
            models.ProductionOrder.source == "mrp",
            models.ProductionOrder.deletion_mark.is_(False),
            models.PlanningRun.source_plan_id.in_(sorted(live_plan_ids)),
            func.coalesce(models.ProductionProduct.quantity, 0)
            > func.coalesce(models.ProductionProduct.produced_qty, 0),
        )
        .order_by(models.ProductionProduct.product_id.asc())
        .with_for_update()
        .all()
    )
    if not rows:
        return {
            "ledger_generation_id": int(generation.id),
            "resized": 0,
            "cancelled": 0,
            "locked": 0,
            "entries": [],
        }

    product_ids = {int(product.product_id) for product, _order, _run in rows}
    order_ids = {int(order.order_id) for _product, order, _run in rows}
    order_line_counts = {
        int(order_id): int(count)
        for order_id, count in db.query(
            models.ProductionProduct.order_id,
            func.count(models.ProductionProduct.product_id),
        )
        .filter(models.ProductionProduct.order_id.in_(sorted(order_ids)))
        .group_by(models.ProductionProduct.order_id)
        .all()
    }
    export_attempted_order_ids = {
        int(source_id)
        for (source_id,) in db.query(models.SyncLink.source_id)
        .filter(
            models.SyncLink.source_system == "PRODPLAN",
            models.SyncLink.source_doctype == "production_order",
            models.SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
            models.SyncLink.source_id.in_(sorted(order_ids)),
        )
        .all()
    }
    issue_product_ids = {
        int(product_id)
        for (product_id,) in db.query(models.ProductionMaterialIssue.product_id)
        .filter(models.ProductionMaterialIssue.product_id.in_(sorted(product_ids)))
        .distinct()
        .all()
    }
    manufacture_product_ids = {
        int(product_id)
        for (product_id,) in db.query(models.ProductionManufacture.product_id)
        .filter(
            models.ProductionManufacture.product_id.in_(sorted(product_ids)),
            models.ProductionManufacture.status != "cancelled",
        )
        .distinct()
        .all()
    }
    chain_order_ids = {
        int(value)
        for painted_id, welded_id in db.query(
            models.PaintWeldChainLink.painted_order_id,
            models.PaintWeldChainLink.welded_order_id,
        )
        .filter(
            (models.PaintWeldChainLink.painted_order_id.in_(sorted(order_ids)))
            | (models.PaintWeldChainLink.welded_order_id.in_(sorted(order_ids)))
        )
        .all()
        for value in (painted_id, welded_id)
        if value is not None
    }

    grouped: dict[
        tuple[int, int],
        list[tuple[models.ProductionProduct, models.ProductionOrder]],
    ] = defaultdict(list)
    locked_product_ids: set[int] = set()
    for product, order, source_run in rows:
        key = (int(source_run.source_plan_id), int(product.item_id))
        grouped[key].append((product, order))
        output = accepted_product_output(product)
        locked = bool(
            str(order.order_ref1c or "").strip()
            or int(order.order_id) in export_attempted_order_ids
            or order_line_counts.get(int(order.order_id), 0) != 1
            or int(product.product_id) in issue_product_ids
            or int(product.product_id) in manufacture_product_ids
            or int(order.order_id) in chain_order_ids
            or _decimal(output.produced_qty) > _EPSILON
        )
        if locked:
            locked_product_ids.add(int(product.product_id))

    entries: list[dict[str, Any]] = []
    cancelled = 0
    resized = 0
    for key in sorted(grouped):
        products = grouped[key]
        locked_open = sum(
            (
                _decimal(accepted_product_output(product).remaining_qty)
                for product, _order in products
                if int(product.product_id) in locked_product_ids
            ),
            Decimal("0"),
        )
        desired_mutable = max(targets.get(key, Decimal("0")) - locked_open, Decimal("0"))
        mutable = [
            (product, order)
            for product, order in products
            if int(product.product_id) not in locked_product_ids
        ]
        if not mutable:
            continue

        mutable_open = sum(
            (_decimal(product.quantity) for product, _order in mutable),
            Decimal("0"),
        )
        if abs(mutable_open - desired_mutable) <= _EPSILON:
            continue

        allocations = [_decimal(product.quantity) for product, _order in mutable]
        if desired_mutable > mutable_open:
            allocations[-1] += desired_mutable - mutable_open
        else:
            left = desired_mutable
            for index, current in enumerate(allocations):
                allocations[index] = min(current, left)
                left -= allocations[index]

        for (product, order), next_qty in zip(mutable, allocations):
            previous_qty = _decimal(product.quantity)
            if next_qty <= _EPSILON:
                state = product.control_state
                if state is None:
                    state = models.ProductionOrderLineState(
                        status="cancelled",
                        issue_status="not_requested",
                    )
                    product.control_state = state
                else:
                    state.status = "cancelled"
                    state.issue_status = "not_requested"
                order.deletion_mark = True
                cancelled += 1
                entries.append(
                    {
                        "product_id": int(product.product_id),
                        "order_id": int(order.order_id),
                        "plan_id": key[0],
                        "item_id": key[1],
                        "previous_quantity": str(previous_qty),
                        "quantity": "0",
                        "action": "cancelled",
                    }
                )
                continue
            if abs(previous_qty - next_qty) <= _EPSILON:
                continue
            _set_quantity(product, next_qty)
            resized += 1
            entries.append(
                {
                    "product_id": int(product.product_id),
                    "order_id": int(order.order_id),
                    "plan_id": key[0],
                    "item_id": key[1],
                    "previous_quantity": str(previous_qty),
                    "quantity": str(next_qty),
                    "action": "resized",
                }
            )

    db.flush()
    return {
        "ledger_generation_id": int(generation.id),
        "resized": resized,
        "cancelled": cancelled,
        "locked": len(locked_product_ids),
        "entries": entries,
    }
