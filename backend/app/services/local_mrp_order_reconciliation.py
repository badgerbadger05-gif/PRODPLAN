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
from app.services.item_ledger.r3_contract import (
    CurrentMrpResolutionError,
    current_live_run,
)
from app.services.one_c_production_order_export import PRODUCTION_ORDER_ENTITY
from app.services.bom_specification_resolver import (
    BomSpecificationResolutionError,
    BomSpecificationResolver,
)


_EPSILON = Decimal("0.000001")
_SPEC_UNTOUCHED_STATUSES = {"", "created", "shortage", "partial", "ready"}


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _set_quantity(product: models.ProductionProduct, quantity: Decimal) -> None:
    product.quantity = quantity
    update_accepted_product_output_cache(product, produced_qty=Decimal("0"))


def _successor_reaches_live_run(
    db: Session,
    *,
    plan_id: int,
    source_run_id: int,
    live_run_id: int,
) -> bool:
    """Prove the local order's MRP belongs to the current business lineage."""
    if int(source_run_id) == int(live_run_id):
        return True
    edges: dict[int, set[int]] = defaultdict(set)
    for predecessor, successor in db.query(
        models.PlanningRunSuccessor.predecessor_run_id,
        models.PlanningRunSuccessor.successor_run_id,
    ).filter(models.PlanningRunSuccessor.plan_id == int(plan_id)).all():
        edges[int(predecessor)].add(int(successor))
    pending = [int(source_run_id)]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        for successor in edges.get(current, set()):
            if successor == int(live_run_id):
                return True
            pending.append(successor)
    return False


def transition_local_mrp_order_spec(
    db: Session,
    *,
    product_id: int,
    new_spec_id: int,
    expected_old_spec_id: int,
    expected_old_spec_revision_hash: str | None,
    expected_new_spec_revision_hash: str,
) -> dict[str, Any]:
    """Move one explicitly selected untouched local order to a frozen spec.

    This is an operator-facing guarded transition, not an automatic default
    reconciliation.  The requested target must be the item's current default
    and must appear with the exact revision in the current live MRP's active
    frozen graph.  Other legitimate frozen refs for the same item are allowed:
    the explicit target identity is the user's disambiguation.

    The caller owns the transaction and publishes the resulting executor
    current-state change.  Blocked calls are read-only and return their exact
    reasons; a retry after the exact transition is idempotent.
    """
    product, order, source_run = (
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
        .filter(models.ProductionProduct.product_id == int(product_id))
        .with_for_update()
        .one_or_none()
        or (None, None, None)
    )
    if product is None or order is None or source_run is None:
        raise ValueError(f"local MRP product {int(product_id)} does not exist")

    old_spec_id = int(product.spec_id) if product.spec_id is not None else None
    old_hash = str(product.spec_revision_hash or "") or None
    new_spec = db.get(models.Specification, int(new_spec_id))
    expected_old_hash = str(expected_old_spec_revision_hash or "") or None
    expected_new_hash = str(expected_new_spec_revision_hash or "").strip()
    audit: dict[str, Any] = {
        "product_id": int(product.product_id),
        "order_id": int(order.order_id),
        "order_number": str(order.order_number or ""),
        "item_id": int(product.item_id),
        "old_spec_id": old_spec_id,
        "old_spec_revision_hash": old_hash,
        "expected_old_spec_id": int(expected_old_spec_id),
        "expected_old_spec_revision_hash": expected_old_hash,
        "new_spec_id": int(new_spec_id),
        "expected_new_spec_revision_hash": expected_new_hash,
        "quantity": str(_decimal(product.quantity)),
    }

    blockers: list[str] = []
    if str(order.source or "") != "mrp":
        blockers.append("order_is_not_local_mrp")
    if bool(order.deletion_mark):
        blockers.append("order_is_deleted")
    if source_run.source_plan_id is None:
        blockers.append("source_plan_missing")
        live_run = None
    else:
        try:
            live_run = current_live_run(db, int(source_run.source_plan_id))
        except CurrentMrpResolutionError:
            live_run = None
            blockers.append("current_live_run_unavailable")

    if live_run is not None:
        audit["plan_id"] = int(live_run.source_plan_id)
        audit["source_run_id"] = int(source_run.run_id)
        audit["live_run_id"] = int(live_run.run_id)
        audit["active_freeze_version"] = (
            int(live_run.active_freeze_version)
            if live_run.active_freeze_version is not None else None
        )
        if not _successor_reaches_live_run(
            db,
            plan_id=int(live_run.source_plan_id),
            source_run_id=int(source_run.run_id),
            live_run_id=int(live_run.run_id),
        ):
            blockers.append("source_run_not_in_live_successor_lineage")
        if live_run.active_freeze_version is None:
            blockers.append("active_freeze_version_missing")

    if new_spec is None:
        blockers.append("new_specification_missing")
        new_spec_ref = ""
        new_spec_hash = ""
    else:
        new_spec_ref = str(new_spec.spec_ref1c or "").strip()
        new_spec_hash = str(new_spec.content_hash or "").strip()
        audit["new_spec_ref"] = new_spec_ref or None
        audit["new_spec_revision_hash"] = new_spec_hash or None
        if not new_spec_ref:
            blockers.append("new_specification_ref_missing")
        if not expected_new_hash:
            blockers.append("expected_new_spec_revision_missing")
        elif new_spec_hash != expected_new_hash:
            blockers.append("new_specification_revision_changed")

    try:
        default_spec_id = BomSpecificationResolver(db).default_spec_id(
            int(product.item_id)
        )
    except BomSpecificationResolutionError:
        default_spec_id = None
        blockers.append("current_default_specification_ambiguous")
    audit["current_default_spec_id"] = (
        int(default_spec_id) if default_spec_id is not None else None
    )
    if default_spec_id is None or int(default_spec_id) != int(new_spec_id):
        blockers.append("explicit_target_is_not_current_default")

    frozen_rows: list[models.MrpFreezeComponent] = []
    route_rows: list[models.MrpFreezeBomNode] = []
    frozen_target_versions: set[str] = set()
    if (
        live_run is not None
        and live_run.active_freeze_version is not None
        and new_spec_ref
        and expected_new_hash
    ):
        frozen_target_versions = {
            str(version or "").strip()
            for (version,) in db.query(models.MrpFreezeComponent.spec_version)
            .filter(
                models.MrpFreezeComponent.run_id == int(live_run.run_id),
                models.MrpFreezeComponent.freeze_version
                == int(live_run.active_freeze_version),
                models.MrpFreezeComponent.parent_item_id == int(product.item_id),
                models.MrpFreezeComponent.spec_ref == new_spec_ref,
            )
            .distinct()
            .all()
        }
        frozen_rows = (
            db.query(models.MrpFreezeComponent)
            .filter(
                models.MrpFreezeComponent.run_id == int(live_run.run_id),
                models.MrpFreezeComponent.freeze_version
                == int(live_run.active_freeze_version),
                models.MrpFreezeComponent.parent_item_id == int(product.item_id),
                models.MrpFreezeComponent.spec_ref == new_spec_ref,
                models.MrpFreezeComponent.spec_version == expected_new_hash,
            )
            .all()
        )
        route_rows = (
            db.query(models.MrpFreezeBomNode)
            .filter(
                models.MrpFreezeBomNode.run_id == int(live_run.run_id),
                models.MrpFreezeBomNode.freeze_version
                == int(live_run.active_freeze_version),
                models.MrpFreezeBomNode.item_id == int(product.item_id),
                models.MrpFreezeBomNode.spec_ref == new_spec_ref,
                models.MrpFreezeBomNode.spec_version == expected_new_hash,
            )
            .all()
        )
    audit["frozen_target_edge_count"] = len(frozen_rows)
    audit["frozen_target_node_count"] = len(route_rows)
    audit["frozen_target_revisions"] = sorted(frozen_target_versions)
    if frozen_target_versions and frozen_target_versions != {expected_new_hash}:
        blockers.append("explicit_target_frozen_revision_ambiguous")
    if not frozen_rows:
        blockers.append("explicit_target_absent_from_current_frozen_graph")

    state = product.control_state
    explicit_workshop_id = (
        int(state.workshop_id)
        if state is not None
        and state.workshop_id is not None
        and str(state.workshop_id_source or "") not in {"auto", "legacy"}
        else None
    )
    route_action = "manual_override_preserved" if explicit_workshop_id else "automatic_route_followed"
    selected_resource_id: int | None = explicit_workshop_id
    selected_material_warehouse = ""
    selected_output_warehouse = ""
    if explicit_workshop_id is not None:
        manual_binding = (
            db.query(models.WorkshopWarehouseBinding)
            .filter(models.WorkshopWarehouseBinding.workshop_id == explicit_workshop_id)
            .one_or_none()
        )
        if manual_binding is None or not str(manual_binding.warehouse_ref1c or "").strip():
            blockers.append("manual_route_warehouse_binding_missing")
        else:
            selected_material_warehouse = str(manual_binding.warehouse_ref1c or "")
            selected_output_warehouse = (
                str(product.destination_warehouse_ref1c or "").strip()
                or str(manual_binding.production_warehouse_ref1c or "").strip()
            )
            if not selected_output_warehouse:
                blockers.append("manual_route_output_warehouse_missing")
    else:
        frozen_routes = {
            (
                int(row.resource_id) if row.resource_id is not None else None,
                str(row.material_warehouse_ref1c or ""),
                str(row.output_warehouse_ref1c or ""),
                str(row.route_reason or ""),
            )
            for row in route_rows
        }
        if not frozen_routes:
            blockers.append("explicit_target_frozen_route_missing")
        elif len(frozen_routes) != 1:
            blockers.append("explicit_target_frozen_route_ambiguous")
        else:
            (
                selected_resource_id,
                selected_material_warehouse,
                selected_output_warehouse,
                route_reason,
            ) = next(iter(frozen_routes))
            if route_reason or selected_resource_id is None:
                blockers.append(
                    f"explicit_target_frozen_route_unavailable:{route_reason or 'NO_RESOURCE'}"
                )
            elif not selected_material_warehouse or not selected_output_warehouse:
                blockers.append("explicit_target_frozen_route_warehouse_missing")
            else:
                resource_ids = {
                    int(resource_id)
                    for (resource_id,) in db.query(
                        models.ResourceProductionKind.resource_id
                    ).filter(
                        models.ResourceProductionKind.production_kind_id
                        == int(new_spec.production_kind_id or -1)
                    ).all()
                }
                if resource_ids != {int(selected_resource_id)}:
                    blockers.append("current_target_kind_route_ambiguous")
                binding = (
                    db.query(models.WorkshopWarehouseBinding)
                    .filter(
                        models.WorkshopWarehouseBinding.workshop_id
                        == int(selected_resource_id)
                    )
                    .one_or_none()
                )
                if binding is None:
                    blockers.append("current_target_route_warehouse_binding_missing")
                elif (
                    str(binding.warehouse_ref1c or "") != selected_material_warehouse
                    or str(binding.production_warehouse_ref1c or "")
                    != selected_output_warehouse
                ):
                    blockers.append("current_target_route_differs_from_frozen_route")

    audit.update({
        "route_action": route_action,
        "selected_resource_id": selected_resource_id,
        "selected_material_warehouse_ref1c": selected_material_warehouse or None,
        "selected_output_warehouse_ref1c": selected_output_warehouse or None,
    })

    exact_target_already_applied = (
        old_spec_id == int(new_spec_id)
        and str(old_hash or "") == expected_new_hash
    )
    if exact_target_already_applied and not blockers:
        return {
            **audit,
            "action": "unchanged",
            "skip_reason": "already_exact_explicit_target",
            "blocked_reasons": [],
        }

    if old_spec_id != int(expected_old_spec_id):
        blockers.append("old_specification_guard_mismatch")
    if old_hash != expected_old_hash:
        blockers.append("old_specification_revision_guard_mismatch")

    line_count = db.query(models.ProductionProduct.product_id).filter(
        models.ProductionProduct.order_id == int(order.order_id)
    ).count()
    if int(line_count) != 1:
        blockers.append("order_not_single_line")
    if str(order.order_ref1c or "").strip():
        blockers.append("order_has_1c_ref")
    if bool(order.is_posted):
        blockers.append("order_is_posted")
    if str(order.order_state_key or "").strip():
        blockers.append("order_has_1c_state")
    if db.query(models.SyncLink.link_id).filter(
        models.SyncLink.source_system == "PRODPLAN",
        models.SyncLink.source_doctype == "production_order",
        models.SyncLink.source_id == int(order.order_id),
    ).first() is not None:
        blockers.append("order_has_sync_attempt")
    if db.query(models.ProductionMaterialIssue.issue_id).filter(
        models.ProductionMaterialIssue.product_id == int(product.product_id)
    ).first() is not None:
        blockers.append("material_issue_exists")
    if db.query(models.ProductionMaterialCustodyEvent.id).filter(
        models.ProductionMaterialCustodyEvent.product_id == int(product.product_id)
    ).first() is not None:
        blockers.append("custody_event_exists")
    if db.query(models.ProductionManufacture.manufacture_id).filter(
        models.ProductionManufacture.product_id == int(product.product_id)
    ).first() is not None:
        blockers.append("manufacture_exists")
    if db.query(models.PaintWeldChainLink.id).filter(
        (models.PaintWeldChainLink.painted_order_id == int(order.order_id))
        | (models.PaintWeldChainLink.welded_order_id == int(order.order_id))
    ).first() is not None:
        blockers.append("paint_weld_chain_exists")
    if _decimal(accepted_product_output(product).produced_qty) > _EPSILON:
        blockers.append("accepted_output_exists")
    if state is not None:
        line_status = str(state.status or "")
        if line_status not in _SPEC_UNTOUCHED_STATUSES:
            blockers.append(f"line_status:{line_status or 'empty'}")
        if state.opened_at is not None:
            blockers.append("line_opened")
        if state.route_sheet_printed_at is not None:
            blockers.append("route_sheet_printed")
        if str(state.issue_status or "not_requested") != "not_requested":
            blockers.append(f"issue_status:{state.issue_status}")

    if blockers:
        reasons = sorted(set(blockers))
        return {
            **audit,
            "action": "blocked",
            "skip_reason": ",".join(reasons),
            "blocked_reasons": reasons,
        }

    previous_workshop_id = (
        int(state.workshop_id)
        if state is not None and state.workshop_id is not None else None
    )
    previous_workshop_source = (
        str(state.workshop_id_source or "") or None if state is not None else None
    )
    product.spec_id = int(new_spec_id)
    product.spec_revision_hash = expected_new_hash
    if (
        state is not None
        and explicit_workshop_id is None
        and str(state.workshop_id_source or "") in {"auto", "legacy"}
    ):
        state.workshop_id = int(selected_resource_id)
        state.workshop_id_source = "auto"
    db.flush()
    return {
        **audit,
        "action": "updated",
        "skip_reason": None,
        "blocked_reasons": [],
        "previous_workshop_id": previous_workshop_id,
        "previous_workshop_source": previous_workshop_source,
        "current_workshop_id": (
            int(state.workshop_id)
            if state is not None and state.workshop_id is not None else None
        ),
        "current_workshop_source": (
            str(state.workshop_id_source or "") or None if state is not None else None
        ),
    }


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
    issue_rows = db.query(
        models.ProductionMaterialIssue.issue_id,
        models.ProductionMaterialIssue.product_id,
        models.ProductionMaterialIssue.exported_ref1c,
    ).filter(
        models.ProductionMaterialIssue.product_id.in_(sorted(product_ids))
    ).all()
    issue_ids = {int(issue_id) for issue_id, _product_id, _ref in issue_rows}
    linked_issue_ids = {
        int(source_id)
        for (source_id,) in db.query(models.SyncLink.source_id)
        .filter(
            models.SyncLink.source_system == "PRODPLAN",
            models.SyncLink.source_doctype == "material_issue",
            models.SyncLink.target_entity == "Document_ПеремещениеЗапасов",
            models.SyncLink.source_id.in_(sorted(issue_ids)),
        )
        .all()
    }
    issue_product_ids = {
        int(product_id)
        for issue_id, product_id, exported_ref1c in issue_rows
        if str(exported_ref1c or "").strip() or int(issue_id) in linked_issue_ids
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
