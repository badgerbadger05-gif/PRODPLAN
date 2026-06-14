from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ..models import (
    DefaultSpecification,
    Item,
    MrpRequirement,
    PlannedOrder,
    PlanningRun,
    ProductionPlanHeader,
    ProductionManufacture,
    ProductionMaterialIssue,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ResourceProductionKind,
    SpecComponent,
    Specification,
    SyncLink,
    Unit,
)
from .production_control_common import (
    date_to_iso as _date_to_iso,
    looks_like_guid as _looks_like_guid,
    parse_date as _parse_date,
    to_float as _to_float,
)
from .production_control_domain import (
    ensure_state as _ensure_state,
    latest_run_id as _latest_run_id,
)
from .replenishment import REPLENISHMENT_FLOW_PRODUCTION, classify_replenishment_flow


DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"
# Plan: line statuses shown in the production journal.
# Legacy technical states are kept for compatibility and mapped to the compact
# workshop-facing labels below.
LINE_STATUSES = {
    "shortage",
    "partial",
    "ready",
    "to_move",
    "assembled",
    "in_progress",
    "done",
    "produced_partial",
    "produced",
    "production_error",
    "completed",
    "cancelled",
}
# 'exported' = PRODPLAN posted the draft into 1C (Posted=false there).
# 'posted'   = 1C admin провёл документ (we discovered Posted=true on sync).
ISSUE_STATUSES = {"not_requested", "requested", "issued", "exported", "posted", "error"}
PRODUCTION_ORDER_ENTITY = "Document_ЗаказНаПроизводство"
COVERAGE_LABELS = {
    "shortage": "Дефицит",
    "partial": "Частично",
    "ready": "Обеспечен",
    "to_move": "К перемещению",
    "assembled": "Собрано",
    "in_progress": "В работе",
    "done": "Готов",
    "produced_partial": "Готов",
    "produced": "Готов",
    "production_error": "Ошибка выпуска",
    "completed": "Завершён",
    "cancelled": "Отменен",
}
STATUS_FILTER_GROUPS = {
    "shortage": ("shortage",),
    "partial": ("partial",),
    "ready": ("ready",),
    "assembled": ("assembled",),
    "done": ("done", "produced_partial", "produced"),
}

ACTIVE_COVERAGE_STATUSES = {"shortage", "partial", "ready"}


def _journal_work_status(line_status: str) -> str:
    # "assembled" describes material coverage in the journal, not the workshop
    # action state. Keep the row actionable as "В работу".
    if line_status == "assembled":
        return "ready"
    return line_status


def _journal_coverage_status(
    line_status: str,
    issue_status: str,
    material_coverage_status: Optional[str] = None,
) -> str:
    if issue_status == "posted":
        return "assembled"
    if issue_status in {"requested", "issued", "exported"}:
        return "to_move"
    if material_coverage_status in ACTIVE_COVERAGE_STATUSES:
        return material_coverage_status
    return line_status


def _active_mrp_products_for_requirement(db: Session, req: MrpRequirement) -> List[Tuple[ProductionProduct, ProductionOrder]]:
    """
    Existing open MRP journal lines for the same item and planning scope.

    MRP snapshots are recreated during manual refreshes and autoruns, so a new
    MrpRequirement.id can describe demand already covered by an older open
    PRODPLAN order. Treat those open lines as coverage before materialising
    another order.
    """
    current_run = db.query(PlanningRun).filter(PlanningRun.run_id == int(req.run_id)).first()
    source_plan_id = int(current_run.source_plan_id) if current_run and current_run.source_plan_id is not None else None

    rows = (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState, PlanningRun)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .outerjoin(PlanningRun, PlanningRun.run_id == ProductionOrder.source_run_id)
        .filter(ProductionProduct.item_id == int(req.item_id))
        .filter(ProductionOrder.source == "mrp")
        .filter(ProductionOrder.deletion_mark == False)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(func.coalesce(ProductionProduct.remaining_qty, 0) > 0)
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(tuple(_TERMINAL_LINE_STATUSES)))
        .all()
    )

    result: List[Tuple[ProductionProduct, ProductionOrder]] = []
    for product, order, state, run in rows:
        if source_plan_id is not None:
            if run is not None and run.source_plan_id == source_plan_id:
                result.append((product, order))
            continue

        start = state.planned_start_date if state and state.planned_start_date else None
        finish = state.planned_finish_date if state and state.planned_finish_date else None
        if start is None and finish is None:
            if order.source_run_id == req.run_id or product.source_mrp_requirement_id == req.id:
                result.append((product, order))
            continue
        interval_start = start or finish
        interval_finish = finish or start
        if interval_start and interval_finish and interval_start <= req.period_to and interval_finish >= req.period_from:
            result.append((product, order))
    return result


def _reused_product_payload(
    product: ProductionProduct,
    order: Optional[ProductionOrder],
    *,
    requirement_id: int,
) -> Dict[str, Any]:
    item = getattr(product, "item", None)
    return {
        "requirement_id": int(requirement_id),
        "product_id": int(product.product_id),
        "order_id": int(product.order_id),
        "order_number": str(order.order_number or "") if order else None,
        "item_id": int(product.item_id),
        "item_name": str(item.item_name or "") if item else "",
        "qty": _to_float(product.remaining_qty) or _to_float(product.quantity),
    }


def _prodplan_order_display_number(product: ProductionProduct, order: ProductionOrder) -> str:
    order_source = str(order.source or "1c")
    if order_source != "mrp":
        return str(order.order_number or "")

    run_id = int(order.source_run_id) if order.source_run_id is not None else None
    planned_order_id = (
        int(product.source_planned_order_id)
        if getattr(product, "source_planned_order_id", None) is not None
        else None
    )
    if run_id is not None and planned_order_id is not None:
        return f"MRP-{run_id}-{planned_order_id}"

    requirement_id = (
        int(product.source_mrp_requirement_id)
        if getattr(product, "source_mrp_requirement_id", None) is not None
        else None
    )
    allocation_key = str(product.source_mrp_allocation_key or "")
    if requirement_id is not None and allocation_key.startswith(f"mrp_requirement:{requirement_id}:order:"):
        try:
            seq = int(allocation_key.rsplit(":", 1)[-1])
        except Exception:
            seq = 1
        return f"MRP-R-{requirement_id}" if seq <= 1 else f"MRP-R-{requirement_id}-{seq}"

    if run_id is not None and product.item_id is not None:
        return f"MRP-RC-{run_id}-{int(product.item_id)}-{int(order.order_id)}"

    return str(order.order_number or "")


def _mrp_duplicate_scope_key(req: MrpRequirement, run: Optional[PlanningRun]) -> Tuple[Any, ...]:
    source_plan_id = int(run.source_plan_id) if run and run.source_plan_id is not None else None
    if source_plan_id is not None:
        return ("plan", source_plan_id, int(req.item_id))
    return ("period", int(req.item_id), req.period_from, req.period_to)


def _production_order_1c_linked_order_ids(db: Session, order_ids: Sequence[int]) -> set[int]:
    ids = sorted({int(order_id) for order_id in order_ids if order_id is not None})
    if not ids:
        return set()
    return {
        int(row.source_id)
        for row in (
            db.query(SyncLink.source_id)
            .filter(
                SyncLink.source_system == "PRODPLAN",
                SyncLink.source_doctype == "production_order",
                SyncLink.source_id.in_(ids),
                SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
                SyncLink.status == "success",
                SyncLink.target_ref_key.isnot(None),
            )
            .all()
        )
    }


def dedupe_mrp_production_orders(db: Session, *, dry_run: bool = True) -> Dict[str, Any]:
    """
    Cancel local MRP duplicates left by earlier non-idempotent recalculations.

    Hard rule: production orders already opened in 1C are immutable. They are
    counted as coverage and never cancelled; only local PRODPLAN MRP rows
    without a 1C ref/success sync-link can be cancelled.
    """
    rows = (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState, MrpRequirement, PlanningRun)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .join(MrpRequirement, MrpRequirement.id == ProductionProduct.source_mrp_requirement_id)
        .join(PlanningRun, PlanningRun.run_id == MrpRequirement.run_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionOrder.source == "mrp")
        .filter(ProductionOrder.deletion_mark == False)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(func.coalesce(ProductionProduct.remaining_qty, 0) > 0)
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(tuple(_TERMINAL_LINE_STATUSES)))
        .all()
    )
    linked_order_ids = _production_order_1c_linked_order_ids(db, [int(order.order_id) for _p, order, _s, _r, _run in rows])

    groups: Dict[Tuple[Any, ...], List[Tuple[ProductionProduct, ProductionOrder, Optional[ProductionOrderLineState], MrpRequirement, PlanningRun]]] = {}
    for row in rows:
        _product, _order, _state, req, run = row
        groups.setdefault(_mrp_duplicate_scope_key(req, run), []).append(row)

    repaired: List[Dict[str, Any]] = []
    untouched: List[Dict[str, Any]] = []
    cancelled_count = 0
    reduced_count = 0
    released_qty_total = 0.0
    reduced_qty_total = 0.0

    for key, group_rows in groups.items():
        latest_req = max(group_rows, key=lambda row: (row[4].started_at or row[3].created_at, int(row[4].run_id), int(row[3].id)))[3]
        required_qty = _to_float(latest_req.net_required_qty)

        def keep_priority(row: Tuple[ProductionProduct, ProductionOrder, Optional[ProductionOrderLineState], MrpRequirement, PlanningRun]) -> Tuple[int, int, int, Any, int]:
            product, order, state, req, _run = row
            is_1c = bool(order.order_ref1c) or int(order.order_id) in linked_order_ids
            status = str(state.status if state else "shortage")
            is_work_started = status not in ACTIVE_COVERAGE_STATUSES
            is_latest = int(req.id) == int(latest_req.id)
            return (
                1 if is_1c else 0,
                1 if is_work_started else 0,
                1 if is_latest else 0,
                order.order_date,
                -int(product.product_id),
            )

        kept: List[Tuple[ProductionProduct, ProductionOrder, Optional[ProductionOrderLineState], MrpRequirement, PlanningRun]] = []
        cancelled: List[Tuple[ProductionProduct, ProductionOrder, Optional[ProductionOrderLineState], MrpRequirement, PlanningRun]] = []
        reduced: List[Tuple[ProductionProduct, ProductionOrder, Optional[ProductionOrderLineState], MrpRequirement, PlanningRun, float, float]] = []
        kept_qty = 0.0
        protected_qty = 0.0
        editable_target_qty = required_qty
        for row in sorted(group_rows, key=keep_priority, reverse=True):
            product, order, state, _req, _run = row
            qty = _to_float(product.remaining_qty)
            is_1c = bool(order.order_ref1c) or int(order.order_id) in linked_order_ids
            if is_1c:
                kept.append(row)
                kept_qty += qty
                protected_qty += qty
                editable_target_qty = max(0.0, required_qty - protected_qty)
                continue

            editable_kept_qty = max(0.0, kept_qty - protected_qty)
            remaining_editable_target = max(0.0, editable_target_qty - editable_kept_qty)
            if remaining_editable_target <= 1e-9:
                cancelled.append(row)
                continue
            if qty <= remaining_editable_target + 1e-9:
                kept.append(row)
                kept_qty += qty
            else:
                new_qty = remaining_editable_target
                kept.append(row)
                kept_qty += new_qty
                reduced.append((product, order, state, _req, _run, qty, new_qty))

        if not cancelled and not reduced:
            if protected_qty > required_qty + 1e-9:
                untouched.append({
                    "scope": list(key),
                    "item_id": int(latest_req.item_id),
                    "required_qty": required_qty,
                    "protected_qty": protected_qty,
                    "reason": "1C-заказы покрывают больше потребности; локальных дублей для отмены нет",
                })
            continue

        cancelled_payload: List[Dict[str, Any]] = []
        for product, order, state, req, _run in cancelled:
            qty = _to_float(product.remaining_qty)
            cancelled_payload.append({
                "product_id": int(product.product_id),
                "order_id": int(order.order_id),
                "order_number": str(order.order_number or ""),
                "requirement_id": int(req.id),
                "qty": qty,
            })
            if not dry_run:
                actual_state = state or _ensure_state(db, product)
                if actual_state.status not in _TERMINAL_LINE_STATUSES:
                    _adjust_requirement_coverage(db, product, -qty)
                    actual_state.status = "cancelled"
                    product.remaining_qty = 0.0
            cancelled_count += 1
            released_qty_total += qty

        reduced_payload: List[Dict[str, Any]] = []
        for product, order, _state, req, _run, old_qty, new_qty in reduced:
            delta = max(0.0, old_qty - new_qty)
            reduced_payload.append({
                "product_id": int(product.product_id),
                "order_id": int(order.order_id),
                "order_number": str(order.order_number or ""),
                "requirement_id": int(req.id),
                "old_qty": old_qty,
                "new_qty": new_qty,
                "delta_qty": delta,
            })
            if not dry_run and delta > 1e-9:
                _adjust_requirement_coverage(db, product, -delta)
                product.remaining_qty = new_qty
                produced_qty = _to_float(product.produced_qty)
                product.quantity = max(produced_qty + new_qty, produced_qty)
            if delta > 1e-9:
                reduced_count += 1
                reduced_qty_total += delta

        if not dry_run:
            latest_req.covered_qty = min(kept_qty, required_qty)
            latest_req.remaining_qty = max(required_qty - _to_float(latest_req.covered_qty), 0.0)

        repaired.append({
            "scope": list(key),
            "item_id": int(latest_req.item_id),
            "latest_requirement_id": int(latest_req.id),
            "required_qty": required_qty,
            "kept_qty": kept_qty,
            "protected_1c_qty": protected_qty,
            "cancelled_qty": sum(row["qty"] for row in cancelled_payload),
            "cancelled": cancelled_payload,
            "reduced_qty": sum(row["delta_qty"] for row in reduced_payload),
            "reduced": reduced_payload,
        })

    if not dry_run:
        db.commit()

    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "groups_repaired": len(repaired),
        "cancelled_count": cancelled_count,
        "reduced_count": reduced_count,
        "released_qty": released_qty_total,
        "reduced_qty": reduced_qty_total,
        "repaired": repaired,
        "untouched": untouched,
    }


def _bom_descendant_ids_for_root(db: Session, root_item_id: int) -> set[int]:
    result = {int(root_item_id)}
    spec_by_item: Dict[int, int] = {
        int(row.item_id): int(row.spec_id)
        for row in db.query(DefaultSpecification.item_id, DefaultSpecification.spec_id)
        .filter(DefaultSpecification.item_id == int(root_item_id))
        .all()
    }

    def visit(item_id: int, seen_specs: set[int]) -> None:
        spec_id = spec_by_item.get(int(item_id))
        if not spec_id or spec_id in seen_specs:
            return
        next_seen = set(seen_specs)
        next_seen.add(int(spec_id))
        for row in db.query(SpecComponent.item_id).filter(SpecComponent.spec_id == int(spec_id)).all():
            child_id = int(row.item_id)
            result.add(child_id)
            if child_id not in spec_by_item:
                ds = db.query(DefaultSpecification.spec_id).filter(DefaultSpecification.item_id == child_id).first()
                if ds:
                    spec_by_item[child_id] = int(ds.spec_id)
            visit(child_id, next_seen)

    visit(int(root_item_id), set())
    return result


def _forecast_payload(forecast_date: Optional[date], due_date: Optional[date]) -> Dict[str, Any]:
    if not forecast_date or not due_date:
        return {
            "forecast_date": _date_to_iso(forecast_date),
            "forecast_shift_days": None,
            "forecast_reason": None,
        }
    shift = (forecast_date - due_date).days
    return {
        "forecast_date": forecast_date.isoformat(),
        "forecast_shift_days": shift,
        "forecast_reason": "смещение по мощностям" if shift > 0 else ("раньше плановой даты" if shift < 0 else "в срок"),
    }


def _default_spec_ids_by_item(db: Session, item_ids: Sequence[int]) -> Dict[int, int]:
    from .workshop_resolution import default_spec_ids_for_items

    return default_spec_ids_for_items(db, item_ids)


def _main_workshops_for_specs(
    db: Session,
    spec_ids: Sequence[int],
) -> Dict[int, Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]]:
    from .workshop_resolution import main_stages_for_specs

    ids = sorted({int(spec_id) for spec_id in spec_ids if spec_id})
    if not ids:
        return {}

    # Stage is display-only (the journal's "этап" column). The workshop comes
    # exclusively from the spec's production kind — the legacy stage->resource
    # fallback used to mask specs with an unfilled kind.
    stage_by_spec = main_stages_for_specs(db, ids)

    resource_by_spec: Dict[int, Tuple[int, str]] = {}
    for row in (
        db.query(Specification.spec_id, ResourceProductionKind.resource_id, ProductionResource.resource_name)
        .join(
            ResourceProductionKind,
            ResourceProductionKind.production_kind_id == Specification.production_kind_id,
        )
        .join(ProductionResource, ProductionResource.resource_id == ResourceProductionKind.resource_id)
        .filter(Specification.spec_id.in_(ids))
        .order_by(ResourceProductionKind.id.asc())
        .all()
    ):
        resource_by_spec.setdefault(
            int(row.spec_id),
            (int(row.resource_id), str(row.resource_name or "")),
        )

    result: Dict[int, Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]] = {}
    for spec_id in ids:
        stage_id, stage_name = stage_by_spec.get(spec_id, (None, None))
        resource_id, resource_name = resource_by_spec.get(spec_id, (None, None))
        result[spec_id] = (resource_id, resource_name, stage_id, stage_name)
    return result


def _unit_display_by_raw(db: Session, raw_units: Sequence[Any]) -> Dict[str, str]:
    raw_values = {str(raw or "").strip() for raw in raw_units if str(raw or "").strip()}
    if not raw_values:
        return {}
    result: Dict[str, str] = {}
    guid_values = {raw for raw in raw_values if _looks_like_guid(raw)}
    if guid_values:
        for unit in db.query(Unit).filter(Unit.unit_ref1c.in_(list(guid_values))).all():
            result[str(unit.unit_ref1c or "").strip()] = str(
                unit.short_name or unit.unit_name or unit.unit_code or ""
            ).strip()
    for raw in raw_values:
        result.setdefault(raw, "" if _looks_like_guid(raw) else raw)
    return result


def create_orders_from_mrp(
    db: Session,
    planned_order_ids: Sequence[int],
    *,
    initiated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Materialize selected MRP planned_order rows into internal production
    orders (ProductionOrder.source='mrp', ProductionProduct.source_planned_
    order_id=...).

    Idempotent per the plan: if a planned_order is already backed by a
    ProductionProduct, it is returned in `reused` and no duplicate is made.
    The partial UNIQUE INDEX ux_production_products_source_planned_order
    enforces this at the DB layer as a safety net.

    Each internal order gets a single line with quantity / remaining_qty
    equal to the planned_order's planned_qty, and a ProductionOrderLineState
    row in status='new' / issue_status='not_requested'.
    """
    created: List[Dict[str, Any]] = []
    reused: List[Dict[str, Any]] = []
    errors: List[str] = []

    today = datetime.now(timezone.utc)
    for pid_raw in planned_order_ids:
        try:
            pid = int(pid_raw)
        except Exception:
            errors.append(f"planned_order_id={pid_raw!r}: невалидный идентификатор")
            continue

        planned = db.query(PlannedOrder).filter(PlannedOrder.order_id == pid).first()
        if not planned:
            errors.append(f"planned_order_id={pid}: запись MRP не найдена")
            continue

        item = db.query(Item).filter(Item.item_id == int(planned.item_id)).first()
        if not item:
            errors.append(f"planned_order_id={pid}: номенклатура {planned.item_id} не найдена")
            continue

        # Idempotency check at the application layer (cheap, friendly error)
        existing_product = (
            db.query(ProductionProduct)
            .filter(ProductionProduct.source_planned_order_id == pid)
            .order_by(ProductionProduct.product_id.desc())
            .first()
        )
        if existing_product is not None:
            existing_order = (
                db.query(ProductionOrder)
                .filter(ProductionOrder.order_id == existing_product.order_id)
                .first()
            )
            reused.append(
                {
                    "planned_order_id": pid,
                    "product_id": int(existing_product.product_id),
                    "order_id": int(existing_product.order_id),
                    "order_number": str(existing_order.order_number) if existing_order else None,
                    "item_id": int(planned.item_id),
                    "item_name": str(item.item_name or ""),
                }
            )
            continue

        qty = _to_float(planned.planned_qty) or _to_float(planned.qty)
        if qty <= 0:
            errors.append(f"planned_order_id={pid}: planned_qty={planned.planned_qty!r} вЂ” нечего материализовать")
            continue

        # Deterministic, traceable internal number вЂ” also unique because
        # production_orders.order_number is indexed (not unique-constrained,
        # but planned_order.order_id never repeats within a planning_run).
        order_number = f"MRP-{int(planned.run_id)}-{pid}"
        order = ProductionOrder(
            order_number=order_number,
            order_date=today,
            order_ref1c=None,
            is_posted=False,
            deletion_mark=False,
            source="mrp",
            source_run_id=int(planned.run_id),
        )
        db.add(order)
        db.flush()

        product = ProductionProduct(
            order_id=int(order.order_id),
            item_id=int(planned.item_id),
            line_number=1,
            quantity=qty,
            produced_qty=0,
            remaining_qty=qty,
            spec_id=_default_spec_id_for_item(db, int(planned.item_id)),
            source_planned_order_id=pid,
        )
        db.add(product)
        db.flush()

        state = ProductionOrderLineState(
            product_id=int(product.product_id),
            status="shortage",
            issue_status="not_requested",
            planned_start_date=planned.start_date,
            planned_finish_date=planned.finish_date or planned.need_date,
        )
        db.add(state)

        created.append(
            {
                "planned_order_id": pid,
                "product_id": int(product.product_id),
                "order_id": int(order.order_id),
                "order_number": order_number,
                "item_id": int(planned.item_id),
                "item_name": str(item.item_name or ""),
                "qty": qty,
            }
        )

    db.commit()
    return {"status": "ok", "created": created, "reused": reused, "errors": errors, "initiated_by": initiated_by}


def create_production_orders_from_mrp_requirements(
    db: Session,
    requirement_ids: Sequence[int],
    *,
    initiated_by: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Materialize selected MrpRequirement rows (production-flow items) into
    internal production orders (ProductionOrder.source='mrp').

    Only production-flow items are processed; purchase/rework requirements are
    skipped (they are covered by PlannedPurchase / PlannedRework instead).

    Idempotent: if a ProductionProduct already links to this requirement via
    source_mrp_requirement_id, the existing order is returned in `reused` and
    no duplicate is created.

    MrpRequirement.covered_qty / remaining_qty are updated to reflect newly
    created orders.
    """
    created: List[Dict[str, Any]] = []
    reused: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errors: List[str] = []

    today = datetime.now(timezone.utc)

    for rid_raw in requirement_ids:
        try:
            rid = int(rid_raw)
        except Exception:
            errors.append(f"requirement_id={rid_raw!r}: невалидный идентификатор")
            continue

        req = db.query(MrpRequirement).filter(MrpRequirement.id == rid).first()
        if not req:
            errors.append(f"requirement_id={rid}: требование не найдено")
            continue

        item = db.query(Item).filter(Item.item_id == int(req.item_id)).first()
        if not item:
            errors.append(f"requirement_id={rid}: номенклатура {req.item_id} не найдена")
            continue

        # Only production-flow items get production orders
        flow = classify_replenishment_flow(getattr(item, "replenishment_method", None))
        if flow != REPLENISHMENT_FLOW_PRODUCTION:
            skipped.append({
                "requirement_id": rid,
                "item_id": int(req.item_id),
                "reason": f"flow={flow}",
            })
            continue

        net_qty = _to_float(req.net_required_qty)
        active_products = _active_mrp_products_for_requirement(db, req)
        active_open_qty = sum(_to_float(product.remaining_qty) for product, _order in active_products)
        remaining = max(0.0, net_qty - active_open_qty)
        if remaining <= 1e-9:
            req.covered_qty = min(active_open_qty, net_qty)
            req.remaining_qty = max(net_qty - _to_float(req.covered_qty), 0.0)
            for product, order in active_products:
                reused.append(_reused_product_payload(product, order, requirement_id=rid))
            skipped.append({
                "requirement_id": rid,
                "item_id": int(req.item_id),
                "reason": "remaining_qty=0 (уже покрыто открытыми заказами)",
            })
            continue

        existing_count = (
            db.query(ProductionProduct)
            .filter(ProductionProduct.source_mrp_requirement_id == rid)
            .count()
        )
        planned_tasks = (
            db.query(PlannedOrder)
            .filter(
                PlannedOrder.run_id == int(req.run_id),
                PlannedOrder.demand_ref == f"mrp_requirement:{rid}",
            )
            .all()
        )
        planned_start = min((task.start_date for task in planned_tasks if task.start_date), default=req.period_from)
        planned_finish = max((task.finish_date or task.need_date for task in planned_tasks if task.finish_date or task.need_date), default=req.period_to)
        spec_id = _default_spec_id_for_item(db, int(req.item_id))
        batches = _split_qty_by_optimal_batch(remaining, getattr(item, "optimal_batch", None))
        created_total = 0.0

        for index, qty in enumerate(batches, start=1):
            seq = existing_count + index
            order_number = f"MRP-R-{rid}" if seq == 1 and len(batches) == 1 else f"MRP-R-{rid}-{seq}"
            order = ProductionOrder(
                order_number=order_number,
                order_date=today,
                order_ref1c=None,
                is_posted=False,
                deletion_mark=False,
                source="mrp",
                source_run_id=int(req.run_id),
            )
            db.add(order)
            db.flush()

            product = ProductionProduct(
                order_id=int(order.order_id),
                item_id=int(req.item_id),
                line_number=1,
                quantity=qty,
                produced_qty=0,
                remaining_qty=qty,
                spec_id=spec_id,
                source_mrp_requirement_id=rid,
                source_mrp_allocation_key=f"mrp_requirement:{rid}:order:{seq}",
            )
            db.add(product)
            db.flush()

            state = ProductionOrderLineState(
                product_id=int(product.product_id),
                status="shortage",
                issue_status="not_requested",
                planned_start_date=planned_start,
                planned_finish_date=planned_finish,
            )
            db.add(state)

            created_total += _to_float(qty)
            created.append({
                "requirement_id": rid,
                "product_id": int(product.product_id),
                "order_id": int(order.order_id),
                "order_number": order_number,
                "item_id": int(req.item_id),
                "item_name": str(item.item_name or ""),
                "qty": qty,
            })

        # Reflect coverage in the requirement row
        new_covered = min(active_open_qty + created_total, net_qty)
        req.covered_qty = new_covered
        req.remaining_qty = max(net_qty - new_covered, 0.0)

    db.commit()
    return {
        "status": "ok",
        "created": created,
        "reused": reused,
        "skipped": skipped,
        "errors": errors,
        "initiated_by": initiated_by,
    }


def _split_qty_by_optimal_batch(qty: float, optimal_batch: Optional[float]) -> List[float]:
    total = _to_float(qty)
    batch = _to_float(optimal_batch)
    if total <= 1e-9:
        return []
    if batch <= 1e-9 or batch >= total - 1e-9:
        return [total]

    result: List[float] = []
    remaining = total
    while remaining > 1e-9:
        part = min(batch, remaining)
        result.append(float(part))
        remaining -= part
    return result


def _default_spec_id_for_item(db: Session, item_id: int) -> Optional[int]:
    row = (
        db.query(DefaultSpecification)
        .filter(DefaultSpecification.item_id == int(item_id))
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(row.spec_id) if row else None


def _planned_dates_by_item(
    db: Session,
    run_id: Optional[int],
    item_ids: Optional[Sequence[int]] = None,
) -> Dict[int, Tuple[Optional[date], Optional[date]]]:
    if not run_id:
        return {}
    q = (
        db.query(
            PlannedOrder.item_id,
            func.min(PlannedOrder.start_date).label("start_date"),
            func.max(PlannedOrder.finish_date).label("finish_date"),
        )
        .filter(PlannedOrder.run_id == run_id)
    )
    ids = sorted({int(item_id) for item_id in (item_ids or []) if item_id is not None})
    if ids:
        q = q.filter(PlannedOrder.item_id.in_(ids))
    rows = q.group_by(PlannedOrder.item_id).all()
    return {int(r.item_id): (r.start_date, r.finish_date) for r in rows}


def list_journal(
    db: Session,
    *,
    product_id: Optional[int] = None,
    order_id: Optional[int] = None,
    root_item_id: Optional[int] = None,
    workshop_id: Optional[int] = None,
    status: Optional[str] = None,
    coverage_status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    run_id = _latest_run_id(db)
    latest_run = db.query(PlanningRun).filter(PlanningRun.run_id == run_id).first() if run_id else None
    requested_coverage_status = str(coverage_status) if coverage_status else None
    failed_manufacture_products = (
        select(ProductionManufacture.product_id)
        .filter(ProductionManufacture.status == "error")
    )

    query = (
        db.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .join(Item, Item.item_id == ProductionProduct.item_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionOrder.deletion_mark == False)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(or_(
            func.coalesce(ProductionProduct.remaining_qty, ProductionProduct.quantity) > 0,
            ProductionProduct.product_id.in_(failed_manufacture_products),
        ))
        .filter(or_(
            func.coalesce(ProductionOrderLineState.status, "shortage").notin_(tuple(_TERMINAL_LINE_STATUSES)),
            ProductionProduct.product_id.in_(failed_manufacture_products),
        ))
        .options(
            joinedload(ProductionProduct.order),
            joinedload(ProductionProduct.item),
            joinedload(ProductionProduct.control_state).joinedload(ProductionOrderLineState.workshop),
        )
    )

    if product_id is not None:
        query = query.filter(ProductionProduct.product_id == int(product_id))
    if order_id is not None:
        query = query.filter(ProductionOrder.order_id == int(order_id))
    if root_item_id is not None:
        related_ids = _bom_descendant_ids_for_root(db, int(root_item_id))
        query = query.filter(ProductionProduct.item_id.in_(related_ids))
    if status:
        status_values = STATUS_FILTER_GROUPS.get(str(status), (str(status),))
        query = query.filter(func.coalesce(ProductionOrderLineState.status, "shortage").in_(status_values))
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            or_(
                ProductionOrder.order_number.ilike(like),
                Item.item_name.ilike(like),
                Item.item_article.ilike(like),
                Item.item_code.ilike(like),
            )
        )
    start = _parse_date(date_from)
    finish = _parse_date(date_to)
    if start:
        query = query.filter(ProductionOrder.order_date >= datetime.combine(start, datetime.min.time()))
    if finish:
        query = query.filter(ProductionOrder.order_date < datetime.combine(finish, datetime.max.time()))

    sort_field = (sort_by or "").strip().lower()
    fast_page = not workshop_id and not requested_coverage_status
    if fast_page:
        total = int(query.count())
        effective_limit = max(1, min(int(limit or 100), 500))
        requested_offset = max(0, int(offset or 0))
        max_offset = max(0, ((total - 1) // effective_limit) * effective_limit) if total else 0
        effective_offset = min(requested_offset, max_offset)
        if sort_field in {"planned_start_date", "planned_finish_date"}:
            planned_dates_sq = (
                db.query(
                    PlannedOrder.item_id.label("item_id"),
                    func.min(PlannedOrder.start_date).label("start_date"),
                    func.max(PlannedOrder.finish_date).label("finish_date"),
                )
                .filter(PlannedOrder.run_id == run_id)
                .group_by(PlannedOrder.item_id)
                .subquery()
            )
            query = query.outerjoin(planned_dates_sq, planned_dates_sq.c.item_id == ProductionProduct.item_id)
            date_expr = (
                func.coalesce(ProductionOrderLineState.planned_finish_date, planned_dates_sq.c.finish_date)
                if sort_field == "planned_finish_date"
                else func.coalesce(ProductionOrderLineState.planned_start_date, planned_dates_sq.c.start_date)
            )
            if (sort_dir or "").strip().lower() == "desc":
                query = query.order_by(date_expr.is_(None), date_expr.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc())
            else:
                query = query.order_by(date_expr.is_(None), date_expr.asc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc())
        else:
            query = query.order_by(ProductionOrder.order_date.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc())
        rows = query.offset(effective_offset).limit(effective_limit).all()
    else:
        rows = query.order_by(ProductionOrder.order_date.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc()).all()
        total = 0
        effective_limit = max(1, min(int(limit or 100), 500))
        effective_offset = max(0, int(offset or 0))

    page_item_ids = sorted({int(product.item_id) for product in rows if product.item_id is not None})
    plan_dates = _planned_dates_by_item(db, run_id, page_item_ids)
    order_ids = sorted({int(product.order_id) for product in rows})
    run_ids = sorted({
        int(product.order.source_run_id)
        for product in rows
        if product.order and product.order.source_run_id is not None
    })
    order_one_c_number_by_id: Dict[int, str] = {}
    if order_ids:
        for row in (
            db.query(SyncLink.source_id, SyncLink.target_number)
            .filter(
                SyncLink.source_doctype == "production_order",
                SyncLink.source_id.in_(order_ids),
                SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
                SyncLink.target_number.isnot(None),
            )
            .all()
        ):
            order_one_c_number_by_id[int(row.source_id)] = str(row.target_number or "")
    source_plan_by_run_id: Dict[int, Dict[str, Any]] = {}
    if run_ids:
        run_rows = (
            db.query(PlanningRun, ProductionPlanHeader)
            .outerjoin(ProductionPlanHeader, ProductionPlanHeader.id == PlanningRun.source_plan_id)
            .filter(PlanningRun.run_id.in_(run_ids))
            .all()
        )
        for run, plan in run_rows:
            period_from = run.period_from or (plan.period_from if plan else None)
            period_to = run.period_to or (plan.period_to if plan else None)
            source_plan_by_run_id[int(run.run_id)] = {
                "source_plan_id": int(run.source_plan_id) if run.source_plan_id is not None else None,
                "source_plan_name": str(plan.name or "") if plan else "",
                "source_plan_period_from": _date_to_iso(period_from),
                "source_plan_period_to": _date_to_iso(period_to),
            }
    planned_order_ids = sorted({
        int(product.source_planned_order_id)
        for product in rows
        if getattr(product, "source_planned_order_id", None) is not None
    })
    req_ids = sorted({
        int(product.source_mrp_requirement_id)
        for product in rows
        if getattr(product, "source_mrp_requirement_id", None) is not None
    })
    planned_due_by_id: Dict[int, date] = {}
    if planned_order_ids:
        for row in (
            db.query(PlannedOrder.order_id, PlannedOrder.need_date)
            .filter(PlannedOrder.order_id.in_(planned_order_ids))
            .all()
        ):
            if row.need_date:
                planned_due_by_id[int(row.order_id)] = row.need_date
    req_meta_by_id: Dict[int, Dict[str, Any]] = {}
    if req_ids:
        for row in (
            db.query(
                MrpRequirement.id,
                MrpRequirement.period_to,
                MrpRequirement.net_required_qty,
                MrpRequirement.covered_qty,
                MrpRequirement.remaining_qty,
            )
            .filter(MrpRequirement.id.in_(req_ids))
            .all()
        ):
            req_meta_by_id[int(row.id)] = {
                "period_to": row.period_to,
                "net_required_qty": _to_float(row.net_required_qty),
                "covered_qty": _to_float(row.covered_qty),
                "remaining_qty": _to_float(row.remaining_qty),
            }
    item_ids = sorted({int(product.item_id) for product in rows if product.item_id is not None})
    product_ids = sorted({int(product.product_id) for product in rows if product.product_id is not None})
    default_spec_by_item = _default_spec_ids_by_item(db, item_ids)
    spec_ids = sorted({
        int(product.spec_id or default_spec_by_item.get(int(product.item_id)) or 0)
        for product in rows
        if product.spec_id or default_spec_by_item.get(int(product.item_id))
    })
    workshop_by_spec = _main_workshops_for_specs(db, spec_ids)
    issue_count_by_product: Dict[int, int] = {}
    if product_ids:
        for row in (
            db.query(ProductionMaterialIssue.product_id, func.count(ProductionMaterialIssue.issue_id).label("issue_count"))
            .filter(ProductionMaterialIssue.product_id.in_(product_ids))
            .group_by(ProductionMaterialIssue.product_id)
            .all()
        ):
            issue_count_by_product[int(row.product_id)] = int(row.issue_count or 0)
    failed_manufacture_by_product: Dict[int, ProductionManufacture] = {}
    if product_ids:
        failed_rows = (
            db.query(ProductionManufacture)
            .filter(ProductionManufacture.product_id.in_(product_ids))
            .filter(ProductionManufacture.status == "error")
            .order_by(ProductionManufacture.product_id.asc(), ProductionManufacture.manufacture_id.desc())
            .all()
        )
        for manufacture in failed_rows:
            failed_manufacture_by_product.setdefault(int(manufacture.product_id), manufacture)
    unit_by_raw = _unit_display_by_raw(db, [getattr(product.item, "unit", None) for product in rows if product.item])

    result: List[Dict[str, Any]] = []
    for product in rows:
        state = getattr(product, "control_state", None)
        spec_id = int(product.spec_id or default_spec_by_item.get(int(product.item_id)) or 0) or None
        inferred_workshop_id, inferred_workshop_name, stage_id, stage_name = workshop_by_spec.get(
            spec_id or 0,
            (None, None, None, None),
        )
        state_workshop_id = int(state.workshop_id) if state and state.workshop_id else None
        resolved_workshop_id = state_workshop_id or inferred_workshop_id
        if workshop_id and resolved_workshop_id != int(workshop_id):
            continue

        planned_start, planned_finish = plan_dates.get(int(product.item_id), (None, None))
        if state and state.planned_start_date:
            planned_start = state.planned_start_date
        if state and state.planned_finish_date:
            planned_finish = state.planned_finish_date
        due_date = None
        source_planned_order_id = int(product.source_planned_order_id) if product.source_planned_order_id is not None else None
        source_mrp_requirement_id = int(product.source_mrp_requirement_id) if product.source_mrp_requirement_id is not None else None
        if source_planned_order_id is not None:
            due_date = planned_due_by_id.get(source_planned_order_id)
        req_meta = req_meta_by_id.get(source_mrp_requirement_id or 0, {})
        if due_date is None and source_mrp_requirement_id is not None:
            due_date = req_meta.get("period_to")
        forecast = _forecast_payload(planned_finish, due_date or planned_finish)
        order_source = str(product.order.source or "1c")
        run_id_for_source = int(product.order.source_run_id) if product.order.source_run_id is not None else None
        source_plan = source_plan_by_run_id.get(run_id_for_source or 0, {})
        order_one_c_number = order_one_c_number_by_id.get(int(product.order_id), "")
        if not order_one_c_number and product.order.order_ref1c and order_source == "1c":
            order_one_c_number = str(product.order.order_number or "")

        issue_count = issue_count_by_product.get(int(product.product_id), 0)
        failed_manufacture = failed_manufacture_by_product.get(int(product.product_id))
        line_status = str(state.status if state else "shortage")
        issue_status = str(state.issue_status if state else "not_requested")
        material_coverage_status = str(getattr(state, "material_coverage_status", "") or "")
        material_coverage_label = str(getattr(state, "material_coverage_label", "") or "")
        row_coverage_status = _journal_coverage_status(line_status, issue_status, material_coverage_status)
        if issue_status in {"", "not_requested"} and row_coverage_status in ACTIVE_COVERAGE_STATUSES:
            work_status = _journal_work_status(row_coverage_status)
        else:
            work_status = _journal_work_status(line_status)
        if failed_manufacture is not None:
            work_status = "production_error"
        coverage_label = (
            material_coverage_label
            if row_coverage_status == material_coverage_status and material_coverage_label
            else COVERAGE_LABELS.get(row_coverage_status, row_coverage_status)
        )
        result.append(
            {
                "product_id": int(product.product_id),
                "order_id": int(product.order_id),
                "order_number": str(product.order.order_number or ""),
                "order_prodplan_number": _prodplan_order_display_number(product, product.order),
                "order_date": _date_to_iso(product.order.order_date),
                # 'mrp' = generated by PRODPLAN (eligible for /orders/export-to-1c);
                # '1c'  = synced from 1C (already there, do not export).
                "order_source": order_source,
                "source": order_source,
                "order_ref1c": str(product.order.order_ref1c or "") if product.order.order_ref1c else None,
                "order_one_c_number": order_one_c_number or None,
                "line_number": product.line_number,
                "item_id": int(product.item_id),
                "item_code": str(product.item.item_code or ""),
                "item_name": str(product.item.item_name or ""),
                "item_article": str(product.item.item_article or ""),
                "optimal_batch": _to_float(product.item.optimal_batch) if product.item.optimal_batch is not None else None,
                "unit": unit_by_raw.get(str(product.item.unit or "").strip(), ""),
                "quantity": _to_float(product.quantity),
                "produced_qty": _to_float(product.produced_qty),
                "remaining_qty": _to_float(product.remaining_qty),
                "status": work_status,
                "coverage_status": row_coverage_status,
                "coverage_label": coverage_label,
                "issue_status": issue_status,
                "material_coverage_status": material_coverage_status or None,
                "material_coverage_label": material_coverage_label or None,
                "material_coverage_calculated_at": _date_to_iso(getattr(state, "material_coverage_calculated_at", None)) if state else None,
                "planned_start_date": _date_to_iso(planned_start),
                "planned_finish_date": _date_to_iso(planned_finish),
                **forecast,
                "opened_at": _date_to_iso(state.opened_at) if state else None,
                "workshop_id": resolved_workshop_id,
                "workshop_name": (state.workshop.resource_name if state and state.workshop else inferred_workshop_name),
                "stage_id": stage_id,
                "stage_name": stage_name,
                "spec_id": spec_id,
                "issue_count": int(issue_count),
                "route_sheet_printed_at": _date_to_iso(state.route_sheet_printed_at) if state else None,
                "comment": str(state.comment or "") if state else "",
                "failed_manufacture_id": int(failed_manufacture.manufacture_id) if failed_manufacture else None,
                "failed_manufacture_error": str(failed_manufacture.export_error or "") if failed_manufacture else None,
                "source_run_id": int(product.order.source_run_id) if product.order.source_run_id is not None else None,
                **source_plan,
                "source_planned_order_id": source_planned_order_id,
                "source_mrp_requirement_id": source_mrp_requirement_id,
                "source_mrp_allocation_key": str(product.source_mrp_allocation_key or "") if product.source_mrp_allocation_key else None,
                "mrp_req_net_qty": req_meta.get("net_required_qty"),
                "mrp_req_covered_qty": req_meta.get("covered_qty"),
                "mrp_req_remaining_qty": req_meta.get("remaining_qty"),
            }
        )

    if requested_coverage_status:
        result = [row for row in result if str(row.get("coverage_status") or "") == requested_coverage_status]

    if not fast_page and sort_field in {"planned_start_date", "planned_finish_date"}:
        descending = (sort_dir or "").strip().lower() == "desc"
        result.sort(key=lambda row: (row.get("order_number") or "", row.get("line_number") or 0))
        result.sort(key=lambda row: row.get(sort_field) or "", reverse=descending)
        result.sort(key=lambda row: row.get(sort_field) in (None, ""))

    if not fast_page:
        total = len(result)
        requested_offset = max(0, int(offset or 0))
        max_offset = max(0, ((total - 1) // effective_limit) * effective_limit) if total else 0
        effective_offset = min(requested_offset, max_offset)
        result = result[effective_offset : effective_offset + effective_limit]
    return {
        "rows": result,
        "total": total,
        "limit": effective_limit,
        "offset": effective_offset,
        "latest_run_id": run_id,
        "latest_source_plan_id": int(latest_run.source_plan_id) if latest_run and latest_run.source_plan_id is not None else None,
    }


def _adjust_requirement_coverage(db: Session, product: ProductionProduct, delta_covered: float) -> None:
    """
    Shift the backing MrpRequirement.covered_qty by `delta_covered` (signed) and
    recompute remaining_qty, clamped to [0, net_required_qty].

    Mirrors the coverage bump done at materialization
    (create_production_orders_from_mrp_requirements). A negative delta releases
    coverage so the residual demand becomes visible again — used when a line is
    closed/cancelled with an un-produced remainder, or its planned quantity is
    reduced. No-op for lines that are not backed by an MrpRequirement
    (1C-source lines, planned-order-source lines).
    """
    rid = getattr(product, "source_mrp_requirement_id", None)
    if rid is None:
        return
    req = db.query(MrpRequirement).filter(MrpRequirement.id == int(rid)).first()
    if req is None:
        return
    net_qty = _to_float(req.net_required_qty)
    new_covered = _to_float(req.covered_qty) + float(delta_covered)
    new_covered = max(0.0, min(new_covered, net_qty))
    req.covered_qty = new_covered
    req.remaining_qty = max(net_qty - new_covered, 0.0)


# Terminal states that close a line: a remainder left un-produced here is never
# going to be made on this line, so its coverage must be released back to the
# requirement.
_TERMINAL_LINE_STATUSES = {"completed", "cancelled"}


def update_line_state(db: Session, product_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    product = db.query(ProductionProduct).filter(ProductionProduct.product_id == int(product_id)).first()
    if not product:
        raise ValueError("Строка заказа не найдена")

    state = _ensure_state(db, product)
    prev_status = str(state.status or "")
    if "status" in payload and payload.get("status"):
        status = str(payload.get("status")).strip()
        if status not in LINE_STATUSES:
            raise ValueError(f"Недопустимый статус: {status}")
        if status == "completed" and (
            _to_float(product.remaining_qty) > 1e-9
            or _to_float(product.produced_qty) <= 1e-9
        ):
            raise ValueError("Нельзя вручную завершить строку с невыпущенным остатком. Используйте выпуск.")
        state.status = status
        # First time the journal moves the line past 'shortage' / 'partial',
        # stamp opened_at вЂ” it acts as a workshop-side timestamp.
        if status in {"ready", "to_move", "assembled", "in_progress", "done", "produced_partial", "produced", "completed"} and not state.opened_at:
            state.opened_at = datetime.now(timezone.utc)
        # Closing the line with an un-produced remainder: release that remainder
        # from the requirement's coverage so the reconciliation job (and the
        # journal) can see the demand is no longer fully ordered.
        if status in _TERMINAL_LINE_STATUSES and prev_status not in _TERMINAL_LINE_STATUSES:
            released = _to_float(product.remaining_qty)
            if released > 1e-9:
                _adjust_requirement_coverage(db, product, -released)
                product.remaining_qty = 0.0
    if "issue_status" in payload and payload.get("issue_status"):
        issue_status = str(payload.get("issue_status")).strip()
        if issue_status not in ISSUE_STATUSES:
            raise ValueError(f"Недопустимый статус выдачи: {issue_status}")
        state.issue_status = issue_status
    if "workshop_id" in payload:
        state.workshop_id = int(payload["workshop_id"]) if payload.get("workshop_id") else None
    if "planned_start_date" in payload:
        state.planned_start_date = _parse_date(payload.get("planned_start_date"))
    if "planned_finish_date" in payload:
        state.planned_finish_date = _parse_date(payload.get("planned_finish_date"))
    if "comment" in payload:
        state.comment = str(payload.get("comment") or "")

    db.commit()
    return {"status": "ok", "product_id": int(product_id)}


def _production_order_has_1c_link(db: Session, order: ProductionOrder) -> bool:
    if order.order_ref1c:
        return True
    return (
        db.query(SyncLink.source_id)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "production_order",
            SyncLink.source_id == int(order.order_id),
            SyncLink.target_entity == PRODUCTION_ORDER_ENTITY,
        )
        .first()
        is not None
    )


def _material_issue_has_1c_link(db: Session, issue: ProductionMaterialIssue) -> bool:
    if issue.exported_ref1c:
        return True
    return (
        db.query(SyncLink.source_id)
        .filter(
            SyncLink.source_system == "PRODPLAN",
            SyncLink.source_doctype == "material_issue",
            SyncLink.source_id == int(issue.issue_id),
            SyncLink.target_entity == "Document_ПеремещениеЗапасов",
        )
        .first()
        is not None
    )


def cancel_local_order(db: Session, product_id: int) -> Dict[str, Any]:
    product = (
        db.query(ProductionProduct)
        .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.control_state))
        .filter(ProductionProduct.product_id == int(product_id))
        .one_or_none()
    )
    if product is None or product.order is None:
        raise ValueError("Строка заказа не найдена")
    order = product.order
    if _production_order_has_1c_link(db, order):
        raise ValueError("Заказ уже открыт в 1С, локальное удаление запрещено")

    products = (
        db.query(ProductionProduct)
        .options(joinedload(ProductionProduct.control_state))
        .filter(ProductionProduct.order_id == int(order.order_id))
        .all()
    )
    product_ids = [int(row.product_id) for row in products]
    issues = (
        db.query(ProductionMaterialIssue)
        .filter(ProductionMaterialIssue.product_id.in_(product_ids))
        .filter(ProductionMaterialIssue.direction.in_(("issue", "in_place")))
        .all()
    )
    linked_issues = [issue for issue in issues if _material_issue_has_1c_link(db, issue)]
    if linked_issues:
        numbers = ", ".join(str(issue.document_number or issue.issue_id) for issue in linked_issues[:5])
        raise ValueError(f"Есть перемещения, уже открытые в 1С: {numbers}")

    deleted_issues = 0
    for issue in issues:
        db.delete(issue)
        deleted_issues += 1

    released_qty = 0.0
    for row in products:
        state = row.control_state or _ensure_state(db, row)
        if state.status != "cancelled":
            qty_to_release = _to_float(row.remaining_qty)
            if qty_to_release > 1e-9:
                _adjust_requirement_coverage(db, row, -qty_to_release)
                released_qty += qty_to_release
                row.remaining_qty = 0.0
            state.status = "cancelled"
        state.issue_status = "not_requested"

    order.deletion_mark = True
    db.commit()
    return {
        "status": "ok",
        "order_id": int(order.order_id),
        "product_ids": product_ids,
        "deleted_issues": deleted_issues,
        "released_qty": released_qty,
    }


def update_product_quantity(db: Session, product_id: int, quantity: float) -> Dict[str, Any]:
    """
    Adjust the planned quantity on a ProductionProduct line.

    Rules:
    - quantity must be > 0.
    - quantity cannot be set below already produced_qty (can't un-produce).
    - remaining_qty is recalculated as max(0, quantity - produced_qty).
    """
    product = db.query(ProductionProduct).filter(ProductionProduct.product_id == int(product_id)).one_or_none()
    if product is None:
        raise ValueError(f"product_id={product_id}: строка заказа не найдена")

    qty = float(quantity)
    if qty <= 0:
        raise ValueError("quantity должен быть положительным")

    produced = _to_float(product.produced_qty)
    if qty < produced - 1e-9:
        raise ValueError(
            f"quantity={qty} меньше уже выпущенного ({produced}). "
            "Нельзя уменьшить заказ ниже факта."
        )

    old_qty = _to_float(product.quantity)
    product.quantity = qty
    product.remaining_qty = max(0.0, qty - produced)
    # Keep the backing requirement's coverage in sync: shrinking the line frees
    # coverage (exposes residual demand), growing it consumes more.
    delta = qty - old_qty
    if abs(delta) > 1e-9:
        _adjust_requirement_coverage(db, product, delta)
    db.commit()
    payload: Dict[str, Any] = {
        "status": "ok",
        "product_id": int(product_id),
        "quantity": float(qty),
        "remaining_qty": float(product.remaining_qty),
    }
    rid = getattr(product, "source_mrp_requirement_id", None)
    if rid is not None:
        req = db.query(MrpRequirement).filter(MrpRequirement.id == int(rid)).first()
        if req is not None:
            payload.update(
                {
                    "mrp_req_net_qty": _to_float(req.net_required_qty),
                    "mrp_req_covered_qty": _to_float(req.covered_qty),
                    "mrp_req_remaining_qty": _to_float(req.remaining_qty),
                }
            )
    return payload
