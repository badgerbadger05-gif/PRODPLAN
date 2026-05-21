from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from ..models import (
    DefaultSpecification,
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    Operation,
    PlannedOrder,
    PlannedPurchase,
    PlanningRun,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    ProductionResource,
    ProductionStage,
    ResourceStage,
    SpecComponent,
    SpecOperation,
    SupplierOrder,
    SupplierOrderItem,
    Unit,
    WorkshopWarehouseBinding,
)
from ..schemas import ODataSyncRequest
from .planning_service import (
    SUPPLIER_ORDER_EXCLUDED_STATE_NAMES,
    _normalize_supplier_order_state_name,
)


DONE_STATE_KEY = "ad28565a-991b-11eb-e39a-fa163e61326a"
# Plan: колонка "Обеспечение" — состояния обеспечения компонентами.
# 'cancelled' добавлено как out-of-band статус для админских отмен.
LINE_STATUSES = {
    "shortage",
    "partial",
    "ready",
    "to_move",
    "assembled",
    "produced_partial",
    "produced",
    "cancelled",
}
# 'exported' = PRODPLAN posted the draft into 1C (Posted=false there).
# 'posted'   = 1C admin провёл документ (we discovered Posted=true on sync).
ISSUE_STATUSES = {"not_requested", "requested", "issued", "exported", "posted", "error"}


def _norm_guid(val: Any) -> str:
    s = str(val or "").strip().lower()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1].strip()
    if s.startswith("guid'") and s.endswith("'"):
        s = s[len("guid'") : -1].strip()
    return s


def _looks_like_guid(val: Any) -> bool:
    s = _norm_guid(val)
    if len(s) != 36:
        return False
    parts = s.split("-")
    return [len(p) for p in parts] == [8, 4, 4, 4, 12] and all(
        all(ch in "0123456789abcdef" for ch in part) for part in parts
    )


def _unit_display(db: Session, raw_unit: Any) -> str:
    raw = str(raw_unit or "").strip()
    if not raw:
        return ""
    unit = db.query(Unit).filter(Unit.unit_ref1c == raw).first()
    if unit:
        return str(unit.short_name or unit.unit_name or unit.unit_code or "").strip()
    return "" if _looks_like_guid(raw) else raw


def _to_float(val: Any) -> float:
    try:
        return float(val or 0.0)
    except Exception:
        return 0.0


def _date_to_iso(val: Any) -> Optional[str]:
    if not val:
        return None
    if hasattr(val, "date") and not isinstance(val, date):
        val = val.date()
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val).split("T")[0].split(" ")[0]


def _parse_date(val: Any) -> Optional[date]:
    if not val:
        return None
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    try:
        return date.fromisoformat(str(val)[:10])
    except Exception:
        return None


def _line_number(product: ProductionProduct) -> int:
    try:
        return int(product.line_number or product.product_id or 0)
    except Exception:
        return 0


def _ensure_state(db: Session, product: ProductionProduct) -> ProductionOrderLineState:
    state = getattr(product, "control_state", None)
    if state:
        return state
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == product.product_id)
        .first()
    )
    if state:
        return state
    state = ProductionOrderLineState(
        product_id=product.product_id,
        status="shortage",
        issue_status="not_requested",
    )
    db.add(state)
    db.flush()
    return state


def _default_spec_id(db: Session, product: ProductionProduct) -> Optional[int]:
    if product.spec_id:
        return int(product.spec_id)
    item_id = int(product.item_id)
    default_spec = (
        db.query(DefaultSpecification)
        .filter(DefaultSpecification.item_id == item_id)
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(default_spec.spec_id) if default_spec else None


def _main_workshop_for_spec(db: Session, spec_id: Optional[int]) -> Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]:
    if not spec_id:
        return (None, None, None, None)

    stage_hours = (
        db.query(SpecOperation.stage_id, func.sum(SpecOperation.time_norm).label("hours"))
        .filter(SpecOperation.spec_id == spec_id, SpecOperation.stage_id.isnot(None))
        .group_by(SpecOperation.stage_id)
        .all()
    )
    stage_id: Optional[int] = None
    if stage_hours:
        stage_id = int(max(stage_hours, key=lambda r: _to_float(r.hours)).stage_id)
    else:
        comp_stage = (
            db.query(SpecComponent.stage_id)
            .filter(SpecComponent.spec_id == spec_id, SpecComponent.stage_id.isnot(None))
            .first()
        )
        if comp_stage:
            stage_id = int(comp_stage.stage_id)

    stage_name: Optional[str] = None
    if stage_id:
        stage = db.query(ProductionStage).filter(ProductionStage.stage_id == stage_id).first()
        stage_name = str(stage.stage_name) if stage else None

    workshop_id: Optional[int] = None
    workshop_name: Optional[str] = None
    if stage_id:
        resource_stage = (
            db.query(ResourceStage)
            .options(joinedload(ResourceStage.resource))
            .filter(ResourceStage.stage_id == stage_id)
            .order_by(ResourceStage.id.asc())
            .first()
        )
        if resource_stage and resource_stage.resource:
            workshop_id = int(resource_stage.resource_id)
            workshop_name = str(resource_stage.resource.resource_name)

    return (workshop_id, workshop_name, stage_id, stage_name)


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

    today = datetime.utcnow()
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
            errors.append(f"planned_order_id={pid}: planned_qty={planned.planned_qty!r} — нечего материализовать")
            continue

        # Deterministic, traceable internal number — also unique because
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


def _default_spec_id_for_item(db: Session, item_id: int) -> Optional[int]:
    row = (
        db.query(DefaultSpecification)
        .filter(DefaultSpecification.item_id == int(item_id))
        .order_by(DefaultSpecification.id.asc())
        .first()
    )
    return int(row.spec_id) if row else None


def _latest_run_id(db: Session) -> Optional[int]:
    row = (
        db.query(PlanningRun)
        .filter(PlanningRun.status.in_(["DONE", "SUCCESS", "FINISHED", "COMPLETED"]))
        .order_by(PlanningRun.finished_at.desc().nullslast(), PlanningRun.run_id.desc())
        .first()
    )
    if not row:
        row = db.query(PlanningRun).order_by(PlanningRun.run_id.desc()).first()
    return int(row.run_id) if row else None


def _planned_dates_by_item(db: Session, run_id: Optional[int]) -> Dict[int, Tuple[Optional[date], Optional[date]]]:
    if not run_id:
        return {}
    rows = (
        db.query(
            PlannedOrder.item_id,
            func.min(PlannedOrder.start_date).label("start_date"),
            func.max(PlannedOrder.finish_date).label("finish_date"),
        )
        .filter(PlannedOrder.run_id == run_id)
        .group_by(PlannedOrder.item_id)
        .all()
    )
    return {int(r.item_id): (r.start_date, r.finish_date) for r in rows}


def list_journal(
    db: Session,
    *,
    workshop_id: Optional[int] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    run_id = _latest_run_id(db)
    plan_dates = _planned_dates_by_item(db, run_id)

    query = (
        db.query(ProductionProduct)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .join(Item, Item.item_id == ProductionProduct.item_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionOrder.deletion_mark == False)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(func.coalesce(ProductionProduct.remaining_qty, ProductionProduct.quantity) > 0)
        .options(
            joinedload(ProductionProduct.order),
            joinedload(ProductionProduct.item),
            joinedload(ProductionProduct.control_state).joinedload(ProductionOrderLineState.workshop),
        )
    )

    if status:
        query = query.filter(func.coalesce(ProductionOrderLineState.status, "shortage") == status)
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

    rows = query.order_by(ProductionOrder.order_date.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc()).all()

    result: List[Dict[str, Any]] = []
    for product in rows:
        state = getattr(product, "control_state", None)
        spec_id = _default_spec_id(db, product)
        inferred_workshop_id, inferred_workshop_name, stage_id, stage_name = _main_workshop_for_spec(db, spec_id)
        state_workshop_id = int(state.workshop_id) if state and state.workshop_id else None
        resolved_workshop_id = state_workshop_id or inferred_workshop_id
        if workshop_id and resolved_workshop_id != int(workshop_id):
            continue

        planned_start, planned_finish = plan_dates.get(int(product.item_id), (None, None))
        if state and state.planned_start_date:
            planned_start = state.planned_start_date
        if state and state.planned_finish_date:
            planned_finish = state.planned_finish_date

        issue_count = db.query(ProductionMaterialIssue).filter(ProductionMaterialIssue.product_id == product.product_id).count()
        result.append(
            {
                "product_id": int(product.product_id),
                "order_id": int(product.order_id),
                "order_number": str(product.order.order_number or ""),
                "order_date": _date_to_iso(product.order.order_date),
                # 'mrp' = generated by PRODPLAN (eligible for /orders/export-to-1c);
                # '1c'  = synced from 1C (already there, do not export).
                "order_source": str(product.order.source or "1c"),
                "order_ref1c": str(product.order.order_ref1c or "") if product.order.order_ref1c else None,
                "line_number": product.line_number,
                "item_id": int(product.item_id),
                "item_code": str(product.item.item_code or ""),
                "item_name": str(product.item.item_name or ""),
                "item_article": str(product.item.item_article or ""),
                "unit": _unit_display(db, product.item.unit),
                "quantity": _to_float(product.quantity),
                "produced_qty": _to_float(product.produced_qty),
                "remaining_qty": _to_float(product.remaining_qty),
                "status": str(state.status if state else "shortage"),
                "issue_status": str(state.issue_status if state else "not_requested"),
                "planned_start_date": _date_to_iso(planned_start),
                "planned_finish_date": _date_to_iso(planned_finish),
                "opened_at": _date_to_iso(state.opened_at) if state else None,
                "workshop_id": resolved_workshop_id,
                "workshop_name": (state.workshop.resource_name if state and state.workshop else inferred_workshop_name),
                "stage_id": stage_id,
                "stage_name": stage_name,
                "spec_id": spec_id,
                "issue_count": int(issue_count),
                "route_sheet_printed_at": _date_to_iso(state.route_sheet_printed_at) if state else None,
                "comment": str(state.comment or "") if state else "",
            }
        )

    total = len(result)
    effective_limit = max(1, min(int(limit or 100), 500))
    effective_offset = max(0, int(offset or 0))
    return {
        "rows": result[effective_offset : effective_offset + effective_limit],
        "total": total,
        "limit": effective_limit,
        "offset": effective_offset,
        "latest_run_id": run_id,
    }


def update_line_state(db: Session, product_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    product = db.query(ProductionProduct).filter(ProductionProduct.product_id == int(product_id)).first()
    if not product:
        raise ValueError("Строка заказа не найдена")

    state = _ensure_state(db, product)
    if "status" in payload and payload.get("status"):
        status = str(payload.get("status")).strip()
        if status not in LINE_STATUSES:
            raise ValueError(f"Недопустимый статус: {status}")
        state.status = status
        # First time the journal moves the line past 'shortage' / 'partial',
        # stamp opened_at — it acts as a workshop-side timestamp.
        if status in {"ready", "to_move", "assembled", "produced_partial", "produced"} and not state.opened_at:
            state.opened_at = datetime.utcnow()
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


def _components_for_product(db: Session, product: ProductionProduct) -> Tuple[Optional[int], List[Dict[str, Any]]]:
    spec_id = _default_spec_id(db, product)
    if not spec_id:
        return None, []
    rows = (
        db.query(SpecComponent, Item)
        .join(Item, Item.item_id == SpecComponent.item_id)
        .filter(SpecComponent.spec_id == spec_id)
        .order_by(Item.item_name.asc())
        .all()
    )
    qty = _to_float(product.remaining_qty) or _to_float(product.quantity)
    components: List[Dict[str, Any]] = []
    for comp, item in rows:
        required = _to_float(comp.quantity) * qty
        if required <= 0:
            continue
        components.append(
            {
                "component_item_id": int(item.item_id),
                "item_code": str(item.item_code or ""),
                "item_name": str(item.item_name or ""),
                "item_article": str(item.item_article or ""),
                "unit": _unit_display(db, item.unit),
                "qty_per_unit": _to_float(comp.quantity),
                "required_qty": required,
                "source_spec_id": spec_id,
            }
        )
    return spec_id, components


def _stock_by_item(db: Session, item_ids: Sequence[int]) -> Dict[int, float]:
    """
    Return per-item available stock with `ignored_warehouses` excluded.

    Resolution order:
    1. If `ignored_warehouses` is empty -> aggregated Item.stock_qty (legacy
       behavior, fast).
    2. Else use item_warehouse_stock filtered by warehouse_ref1c NOT IN
       (ignored). Items that have ANY rows in item_warehouse_stock are
       considered "covered by the breakdown" — if all of their stock is in
       ignored warehouses they end up with 0, which is the desired effect.
    3. Items without any breakdown rows fallback to Item.stock_qty so a
       partially-synced DB doesn't blank coverage. After a full re-sync the
       breakdown path becomes authoritative for everything.
    """
    ids = [int(x) for x in item_ids if x is not None]
    if not ids:
        return {}

    ignored_refs_rows = db.query(IgnoredWarehouse.warehouse_ref1c).all()
    ignored_refs = {str(r[0]) for r in ignored_refs_rows if r and r[0]}

    if not ignored_refs:
        result: Dict[int, float] = {}
        for iid, stock in (
            db.query(Item.item_id, Item.stock_qty).filter(Item.item_id.in_(ids)).all()
        ):
            result[int(iid)] = _to_float(stock)
        return result

    # Per-warehouse path: sum non-ignored buckets per item.
    sum_rows = (
        db.query(ItemWarehouseStock.item_id, func.sum(ItemWarehouseStock.qty))
        .filter(ItemWarehouseStock.item_id.in_(ids))
        .filter(~ItemWarehouseStock.warehouse_ref1c.in_(ignored_refs))
        .group_by(ItemWarehouseStock.item_id)
        .all()
    )
    breakdown_stocks: Dict[int, float] = {int(iid): _to_float(qty) for iid, qty in sum_rows}

    # Items that have ANY breakdown rows at all (even if 0 after ignored
    # filter). These items are "authoritative" via the breakdown table.
    has_any_rows = {
        int(iid)
        for (iid,) in db.query(ItemWarehouseStock.item_id)
        .filter(ItemWarehouseStock.item_id.in_(ids))
        .distinct()
        .all()
    }

    result: Dict[int, float] = {}
    missing_ids = [iid for iid in ids if iid not in has_any_rows]
    if missing_ids:
        # No breakdown yet for these items -> fallback to aggregated.
        for iid, stock in (
            db.query(Item.item_id, Item.stock_qty).filter(Item.item_id.in_(missing_ids)).all()
        ):
            result[int(iid)] = _to_float(stock)
    for iid in ids:
        if iid in has_any_rows:
            result[iid] = breakdown_stocks.get(iid, 0.0)
    return result


def _open_issue_reservations_by_item(db: Session, item_ids: Sequence[int]) -> Dict[int, float]:
    """
    Material already committed but not yet physically moved: sum of
    (required_qty - issued_qty) across production_material_issue_lines whose
    parent issue is in an active state ('draft', 'requested', 'issued',
    'exported'). 'error' and 'cancelled' issues do not reserve.
    """
    ids = [int(x) for x in item_ids if x is not None]
    if not ids:
        return {}
    rows = (
        db.query(
            ProductionMaterialIssueLine.component_item_id,
            func.coalesce(
                func.sum(
                    ProductionMaterialIssueLine.required_qty - func.coalesce(ProductionMaterialIssueLine.issued_qty, 0)
                ),
                0,
            ),
        )
        .join(ProductionMaterialIssue, ProductionMaterialIssue.issue_id == ProductionMaterialIssueLine.issue_id)
        .filter(ProductionMaterialIssueLine.component_item_id.in_(ids))
        .filter(ProductionMaterialIssue.status.in_(("draft", "requested", "issued", "exported")))
        .group_by(ProductionMaterialIssueLine.component_item_id)
        .all()
    )
    return {int(iid): max(0.0, _to_float(amount)) for iid, amount in rows}


def _supplier_eta_by_item(db: Session, item_ids: Sequence[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Per-item list of expected arrivals from active supplier orders (excluded
    states are filtered out, see SUPPLIER_ORDER_EXCLUDED_STATE_NAMES). Sorted
    by delivery_date ascending. Only deliveries with a positive remaining_qty
    are included; rows without delivery_date are skipped.
    """
    ids = [int(x) for x in item_ids if x is not None]
    if not ids:
        return {}
    try:
        rows = (
            db.query(
                SupplierOrderItem.item_id_ref,
                SupplierOrderItem.delivery_date,
                SupplierOrderItem.remaining_qty,
                SupplierOrder.order_number,
                SupplierOrder.order_state_name,
            )
            .join(SupplierOrder, SupplierOrder.order_id == SupplierOrderItem.order_id)
            .filter(SupplierOrderItem.item_id_ref.in_(ids))
            .filter(SupplierOrder.deletion_mark.is_(False))
            .filter(SupplierOrderItem.delivery_date.isnot(None))
            .filter(func.coalesce(SupplierOrderItem.remaining_qty, 0) > 0)
            .order_by(SupplierOrderItem.delivery_date.asc())
            .all()
        )
    except Exception:
        return {}

    result: Dict[int, List[Dict[str, Any]]] = {}
    for iid, deliv, remaining, order_number, state_name in rows:
        norm_state = _normalize_supplier_order_state_name(state_name)
        if norm_state in SUPPLIER_ORDER_EXCLUDED_STATE_NAMES:
            continue
        deliv_dt = deliv.date() if isinstance(deliv, datetime) else deliv
        result.setdefault(int(iid), []).append(
            {
                "source": "supplier_order",
                "date": _date_to_iso(deliv_dt),
                "qty": _to_float(remaining),
                "ref": str(order_number or ""),
            }
        )
    return result


def _planned_eta_by_item(db: Session, item_ids: Sequence[int], run_id: Optional[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Per-item list of expected internal production / purchases from the latest
    completed planning_run. Useful as fallback ETA when the supplier-orders
    list is empty for a component. Sorted by need_date.
    """
    ids = [int(x) for x in item_ids if x is not None]
    if not ids or not run_id:
        return {}

    result: Dict[int, List[Dict[str, Any]]] = {}

    prod_rows = (
        db.query(
            PlannedOrder.item_id,
            PlannedOrder.need_date,
            PlannedOrder.planned_qty,
            PlannedOrder.order_id,
        )
        .filter(PlannedOrder.run_id == run_id)
        .filter(PlannedOrder.item_id.in_(ids))
        .filter(func.coalesce(PlannedOrder.planned_qty, 0) > 0)
        .order_by(PlannedOrder.need_date.asc())
        .all()
    )
    for iid, need_date, qty, order_id in prod_rows:
        result.setdefault(int(iid), []).append(
            {
                "source": "planned_production",
                "date": _date_to_iso(need_date),
                "qty": _to_float(qty),
                "ref": f"MRP-{run_id}-{order_id}",
            }
        )

    purch_rows = (
        db.query(
            PlannedPurchase.item_id,
            PlannedPurchase.need_date,
            PlannedPurchase.planned_qty,
            PlannedPurchase.purchase_id,
        )
        .filter(PlannedPurchase.run_id == run_id)
        .filter(PlannedPurchase.item_id.in_(ids))
        .filter(func.coalesce(PlannedPurchase.planned_qty, 0) > 0)
        .order_by(PlannedPurchase.need_date.asc())
        .all()
    )
    for iid, need_date, qty, purchase_id in purch_rows:
        result.setdefault(int(iid), []).append(
            {
                "source": "planned_purchase",
                "date": _date_to_iso(need_date),
                "qty": _to_float(qty),
                "ref": f"MRP-PURCH-{run_id}-{purchase_id}",
            }
        )
    return result


def _component_coverage_label(required: float, available: float) -> str:
    if required <= 1e-9:
        return "ok"
    if available + 1e-9 >= required:
        return "ok"
    if available > 1e-9:
        return "partial"
    return "shortage"


def _aggregate_coverage(component_labels: Sequence[str]) -> str:
    """
    Plan rules:
    - any 'shortage' -> 'shortage' (whole order blocked on at least one comp)
    - any 'partial' (and no shortage) -> 'partial'
    - all 'ok' -> 'ready'
    Empty list -> 'shortage' (no spec / no materials = nothing to cover with).
    """
    if not component_labels:
        return "shortage"
    if any(label == "shortage" for label in component_labels):
        return "shortage"
    if any(label == "partial" for label in component_labels):
        return "partial"
    return "ready"


def _maybe_bump_coverage_status(state: ProductionOrderLineState, new_status: str) -> None:
    """
    Auto-refresh the line status only while it sits in one of the coverage-
    evaluation states. Once the user / system moves past (to_move, assembled,
    produced_*, cancelled), the status is sticky and we don't override it.
    """
    if state.status in {"shortage", "partial", "ready"} and state.status != new_status:
        state.status = new_status


def preview_materials(db: Session, product_id: int) -> Dict[str, Any]:
    """
    Return the BOM components required for a production line plus per-component
    availability and ETA, and refresh the line's coverage status accordingly.

    Per-component fields:
      required_qty   — needed for this order line
      available_qty  — items.stock_qty minus open material-issue reservations,
                       clamped to >=0
      missing_qty    — max(0, required - available)
      coverage       — 'ok' | 'partial' | 'shortage'
      eta_dates      — chronological list of {source, date, qty, ref} from
                       active supplier orders and the latest planning run,
                       only populated when coverage is not 'ok'

    Order-level field `coverage` aggregates per-component labels per the plan
    rules (any shortage -> shortage, else any partial -> partial, else ready).
    """
    product = (
        db.query(ProductionProduct)
        .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.item))
        .filter(ProductionProduct.product_id == int(product_id))
        .first()
    )
    if not product:
        raise ValueError("Строка заказа не найдена")
    spec_id, components = _components_for_product(db, product)

    comp_ids = [int(c["component_item_id"]) for c in components]
    # Stock honours `ignored_warehouses`: items lying in е.g. изоляторе брака
    # are not counted as available (plan rule).
    stock_by_item = _stock_by_item(db, comp_ids)

    reservations = _open_issue_reservations_by_item(db, comp_ids)
    run_id = _latest_run_id(db)
    supplier_eta = _supplier_eta_by_item(db, comp_ids)
    planned_eta = _planned_eta_by_item(db, comp_ids, run_id)

    for comp in components:
        iid = int(comp["component_item_id"])
        required = _to_float(comp["required_qty"])
        raw_stock = stock_by_item.get(iid, 0.0)
        reserved = reservations.get(iid, 0.0)
        available = max(0.0, raw_stock - reserved)
        missing = max(0.0, required - available)
        label = _component_coverage_label(required, available)
        comp["available_qty"] = available
        comp["stock_qty"] = raw_stock
        comp["reserved_qty"] = reserved
        comp["missing_qty"] = missing
        comp["coverage"] = label
        if label == "ok":
            comp["eta_dates"] = []
        else:
            etas: List[Dict[str, Any]] = list(supplier_eta.get(iid, [])) + list(planned_eta.get(iid, []))
            etas.sort(key=lambda e: e.get("date") or "")
            comp["eta_dates"] = etas

    order_coverage = _aggregate_coverage([str(c["coverage"]) for c in components])

    state = _ensure_state(db, product)
    _maybe_bump_coverage_status(state, order_coverage)
    db.commit()

    return {
        "product_id": int(product.product_id),
        "order_number": str(product.order.order_number or ""),
        "item_name": str(product.item.item_name or ""),
        "item_article": str(product.item.item_article or ""),
        "qty": _to_float(product.remaining_qty) or _to_float(product.quantity),
        "spec_id": spec_id,
        "components": components,
        "coverage": order_coverage,
    }


def _next_issue_number(db: Session) -> str:
    today = datetime.utcnow().strftime("%Y%m%d")
    prefix = f"MI-{today}-"
    count = db.query(ProductionMaterialIssue).filter(ProductionMaterialIssue.document_number.like(f"{prefix}%")).count()
    return f"{prefix}{count + 1:04d}"


def create_material_issues(
    db: Session,
    product_ids: Sequence[int],
    *,
    initiated_by: Optional[str] = None,
    warehouse_ref1c: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Idempotent per the plan: a repeated click on "prepare issue" for the same
    production line must not create a duplicate document.

    If an active (draft|requested) ProductionMaterialIssue already exists for
    the product, return its descriptor in `reused` instead of creating a new
    one. Issues already exported to 1C or in error state are treated as
    archived — a fresh draft can be created in their place.
    """
    created: List[Dict[str, Any]] = []
    reused: List[Dict[str, Any]] = []
    errors: List[str] = []
    for pid in product_ids:
        product = (
            db.query(ProductionProduct)
            .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.item))
            .filter(ProductionProduct.product_id == int(pid))
            .first()
        )
        if not product:
            errors.append(f"product_id={pid}: строка заказа не найдена")
            continue

        existing = (
            db.query(ProductionMaterialIssue)
            .filter(
                ProductionMaterialIssue.product_id == int(product.product_id),
                ProductionMaterialIssue.status.in_(("draft", "requested")),
            )
            .order_by(ProductionMaterialIssue.issue_id.desc())
            .first()
        )
        if existing is not None:
            reused.append(
                {
                    "issue_id": int(existing.issue_id),
                    "document_number": str(existing.document_number),
                    "product_id": int(product.product_id),
                    "order_number": str(product.order.order_number or ""),
                    "item_name": str(product.item.item_name or ""),
                    "status": str(existing.status),
                }
            )
            continue

        spec_id, components = _components_for_product(db, product)
        if not components:
            errors.append(f"product_id={pid}: не найдена спецификация или материалы")
            continue

        # If the caller did not pin a destination warehouse, fall back to the
        # workshop->warehouse binding from settings. Plan rule:
        # "привязка участок -> склад получатель".
        resolved_warehouse = warehouse_ref1c
        if not resolved_warehouse:
            state_obj = (
                db.query(ProductionOrderLineState)
                .filter(ProductionOrderLineState.product_id == int(product.product_id))
                .first()
            )
            workshop_id_resolved: Optional[int] = (
                int(state_obj.workshop_id) if state_obj and state_obj.workshop_id else None
            )
            if workshop_id_resolved:
                binding = (
                    db.query(WorkshopWarehouseBinding)
                    .filter(WorkshopWarehouseBinding.workshop_id == workshop_id_resolved)
                    .first()
                )
                if binding:
                    resolved_warehouse = str(binding.warehouse_ref1c)

        issue = ProductionMaterialIssue(
            document_number=_next_issue_number(db),
            product_id=int(product.product_id),
            order_id=int(product.order_id),
            status="draft",
            warehouse_ref1c=resolved_warehouse,
            initiated_by=initiated_by,
        )
        db.add(issue)
        db.flush()
        for comp in components:
            db.add(
                ProductionMaterialIssueLine(
                    issue_id=int(issue.issue_id),
                    component_item_id=int(comp["component_item_id"]),
                    required_qty=float(comp["required_qty"]),
                    issued_qty=0.0,
                    unit=comp.get("unit"),
                    source_spec_id=spec_id,
                    line_status="planned",
                )
            )
        state = _ensure_state(db, product)
        state.issue_status = "requested"
        # Once a material-issue draft is open, the line has moved beyond the
        # "no coverage yet" phase. Bump status to 'to_move' (документы созданы,
        # ждём проведения) unless it's already further along.
        if state.status in {"shortage", "partial", "ready"}:
            state.status = "to_move"
        created.append(
            {
                "issue_id": int(issue.issue_id),
                "document_number": issue.document_number,
                "product_id": int(product.product_id),
                "order_number": str(product.order.order_number or ""),
                "item_name": str(product.item.item_name or ""),
                "lines_count": len(components),
            }
        )
    db.commit()
    return {"status": "ok", "created": created, "reused": reused, "errors": errors}


def get_issue(db: Session, issue_id: int) -> Dict[str, Any]:
    issue = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.lines).joinedload(ProductionMaterialIssueLine.component_item),
        )
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .first()
    )
    if not issue:
        raise ValueError("Документ выдачи не найден")
    return {
        "issue_id": int(issue.issue_id),
        "document_number": str(issue.document_number),
        "status": str(issue.status),
        "warehouse_ref1c": str(issue.warehouse_ref1c or ""),
        "initiated_by": str(issue.initiated_by or ""),
        "order_number": str(issue.order.order_number or ""),
        "product_id": int(issue.product_id),
        "item_name": str(issue.product.item.item_name or "") if issue.product and issue.product.item else "",
        "item_article": str(issue.product.item.item_article or "") if issue.product and issue.product.item else "",
        "created_at": _date_to_iso(issue.created_at),
        "exported_ref1c": str(issue.exported_ref1c or ""),
        "export_error": str(issue.export_error or ""),
        "lines": [
            {
                "line_id": int(line.line_id),
                "component_item_id": int(line.component_item_id),
                "item_code": str(line.component_item.item_code or ""),
                "item_name": str(line.component_item.item_name or ""),
                "item_article": str(line.component_item.item_article or ""),
                "required_qty": _to_float(line.required_qty),
                "issued_qty": _to_float(line.issued_qty),
                "unit": _unit_display(db, line.unit or line.component_item.unit),
                "line_status": str(line.line_status),
            }
            for line in sorted(issue.lines, key=lambda x: x.line_id)
        ],
    }


def build_issue_1c_payload(db: Session, issue_id: int) -> Dict[str, Any]:
    issue_data = get_issue(db, issue_id)
    issue = (
        db.query(ProductionMaterialIssue)
        .options(
            joinedload(ProductionMaterialIssue.order),
            joinedload(ProductionMaterialIssue.product).joinedload(ProductionProduct.item),
            joinedload(ProductionMaterialIssue.lines).joinedload(ProductionMaterialIssueLine.component_item),
        )
        .filter(ProductionMaterialIssue.issue_id == int(issue_id))
        .first()
    )
    if not issue:
        raise ValueError("Документ выдачи не найден")

    return {
        "Number": str(issue.document_number),
        "Date": datetime.utcnow().replace(microsecond=0).isoformat(),
        "Posted": False,
        "Комментарий": f"PRODPLAN: выдача под заказ {issue.order.order_number}, строка {issue.product.line_number or issue.product_id}",
        "ЗаказНаПроизводство_Key": str(issue.order.order_ref1c or ""),
        "Склад_Key": str(issue.warehouse_ref1c or ""),
        "Продукция_Key": str(issue.product.item.item_ref1c or "") if issue.product and issue.product.item else "",
        "ПродукцияКоличество": _to_float(issue.product.remaining_qty) or _to_float(issue.product.quantity),
        "Материалы": [
            {
                "LineNumber": idx + 1,
                "Номенклатура_Key": str(line.component_item.item_ref1c or ""),
                "Количество": _to_float(line.required_qty),
                "Единица": _unit_display(db, line.unit or line.component_item.unit),
            }
            for idx, line in enumerate(sorted(issue.lines, key=lambda x: x.line_id))
        ],
        "_prodplan": {
            "issue_id": issue_data["issue_id"],
            "document_number": issue_data["document_number"],
            "product_id": issue_data["product_id"],
        },
    }


def export_issue_to_1c(db: Session, issue_id: int, req: ODataSyncRequest) -> Dict[str, Any]:
    issue = db.query(ProductionMaterialIssue).filter(ProductionMaterialIssue.issue_id == int(issue_id)).first()
    if not issue:
        raise ValueError("Документ выдачи не найден")
    payload = build_issue_1c_payload(db, issue_id)

    if req.dry_run:
        return {
            "status": "dry_run",
            "entity_name": req.entity_name,
            "payload": payload,
        }

    from ..services.odata_client import OData1CClient

    client = OData1CClient(req.base_url, req.username, req.password, req.token)
    try:
        response = client.post(req.entity_name, payload, timeout=120)
        ref = str(response.get("Ref_Key") or response.get("ref") or response.get("Ref") or "")
        issue.status = "exported"
        issue.exported_ref1c = ref or None
        issue.exported_at = datetime.utcnow()
        issue.export_error = None
        state = (
            db.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id == issue.product_id)
            .first()
        )
        if state:
            state.issue_status = "exported"
        db.commit()
        return {
            "status": "ok",
            "issue_id": int(issue.issue_id),
            "document_number": str(issue.document_number),
            "exported_ref1c": ref,
            "response": response,
        }
    except Exception as e:
        issue.status = "error"
        issue.export_error = str(e)
        state = (
            db.query(ProductionOrderLineState)
            .filter(ProductionOrderLineState.product_id == issue.product_id)
            .first()
        )
        if state:
            state.issue_status = "error"
        db.commit()
        raise


def mark_route_sheets_printed(db: Session, product_ids: Iterable[int]) -> int:
    count = 0
    for pid in product_ids:
        product = db.query(ProductionProduct).filter(ProductionProduct.product_id == int(pid)).first()
        if not product:
            continue
        state = _ensure_state(db, product)
        state.route_sheet_printed_at = datetime.utcnow()
        count += 1
    db.commit()
    return count


def _operation_rows(db: Session, spec_id: Optional[int]) -> List[Dict[str, Any]]:
    if not spec_id:
        return []
    rows = (
        db.query(SpecOperation, ProductionStage, Operation)
        .outerjoin(ProductionStage, ProductionStage.stage_id == SpecOperation.stage_id)
        .outerjoin(Operation, Operation.operation_id == SpecOperation.operation_id)
        .filter(SpecOperation.spec_id == spec_id)
        .order_by(SpecOperation.spec_operation_id.asc())
        .all()
    )
    return [
        {
            "number": idx + 1,
            "stage_name": str(stage.stage_name or "") if stage else "",
            "operation_name": str(operation.operation_name or "") if operation else "",
            "time_norm": _to_float(op.time_norm),
        }
        for idx, (op, stage, operation) in enumerate(rows)
    ]


def render_route_sheets_html(db: Session, product_ids: Sequence[int]) -> str:
    products = (
        db.query(ProductionProduct)
        .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.item))
        .filter(ProductionProduct.product_id.in_([int(x) for x in product_ids]))
        .all()
    )
    product_map = {int(p.product_id): p for p in products}
    ordered = [product_map[int(pid)] for pid in product_ids if int(pid) in product_map]
    now = datetime.now().strftime("%d.%m.%Y")
    sheets: List[str] = []
    for product in ordered:
        spec_id, components = _components_for_product(db, product)
        operations = _operation_rows(db, spec_id)
        order_date = _date_to_iso(product.order.order_date) or ""
        title = f"МАРШРУТНЫЙ ЛИСТ № {html.escape(str(product.order.order_number or ''))}/{_line_number(product)} от {now}"
        component_rows = "".join(
            "<tr>"
            f"<td>{html.escape(c['item_name'])}</td>"
            f"<td>{html.escape(c['item_article'])}</td>"
            f"<td class='num'>{c['qty_per_unit']:.3f}</td>"
            f"<td class='num'>{c['required_qty']:.3f}</td>"
            "</tr>"
            for c in components
        ) or "<tr><td colspan='4'>Материалы по спецификации не найдены</td></tr>"
        op_rows = "".join(
            "<tr>"
            f"<td class='num'>{op['number']}</td>"
            f"<td>{html.escape(op['stage_name'])}</td>"
            f"<td>{html.escape(op['operation_name'] or op['stage_name'] or 'Операция')}</td>"
            f"<td class='num'>{op['time_norm']:.3f}</td>"
            "<td></td><td></td><td></td>"
            "</tr>"
            for op in operations
        ) or "<tr><td colspan='7'>Операции по спецификации не найдены</td></tr>"
        sheets.append(
            f"""
            <section class="sheet">
              <table class="route">
                <tr>
                  <td colspan="4" class="title">{title}<br><span>(Изготовление новых)</span></td>
                  <td colspan="3" class="order">Заказ на производство №{html.escape(str(product.order.order_number or ""))}<br>Дата заказа: {html.escape(order_date)}</td>
                </tr>
                <tr>
                  <td colspan="3"><b>Наименование:</b><br>{html.escape(str(product.item.item_name or ""))}</td>
                  <td colspan="2"><b>Артикул:</b><br>{html.escape(str(product.item.item_article or ""))}</td>
                  <td colspan="2"><b>Количество:</b><br>{_to_float(product.remaining_qty) or _to_float(product.quantity):g} {html.escape(_unit_display(db, product.item.unit))}</td>
                </tr>
                <tr><td colspan="7"><b>Материалы и заготовки</b></td></tr>
                <tr><th colspan="2">Материал</th><th>Артикул</th><th>Кол-во на ед.</th><th colspan="3">Кол-во по заказу</th></tr>
                {component_rows}
                <tr><th>№</th><th>Цех / участок</th><th colspan="2">Операция</th><th>Трудоемкость</th><th>Исполнитель</th><th>ОТК</th></tr>
                {op_rows}
                <tr><td colspan="7" class="notes"><b>Дополнительная информация:</b><br><br><br><br></td></tr>
              </table>
            </section>
            """
        )
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Маршрутные листы</title>
  <style>
    @page {{ size: A4 landscape; margin: 8mm; }}
    body {{ font-family: "Times New Roman", serif; color: #000; margin: 0; }}
    .toolbar {{ position: sticky; top: 0; padding: 8px; background: #f4f6f8; border-bottom: 1px solid #cfd8dc; font-family: Arial, sans-serif; }}
    .toolbar button {{ padding: 6px 12px; }}
    .sheet {{ page-break-after: always; padding: 6px; }}
    table.route {{ border-collapse: collapse; width: 100%; font-size: 15px; }}
    .route td, .route th {{ border: 1px solid #000; padding: 4px; vertical-align: top; }}
    .title {{ font-size: 22px; line-height: 1.25; }}
    .title span {{ font-size: 20px; }}
    .order {{ font-size: 18px; vertical-align: middle; }}
    th {{ text-align: center; font-weight: bold; }}
    .num {{ text-align: center; white-space: nowrap; }}
    .notes {{ height: 90px; }}
    @media print {{ .toolbar {{ display: none; }} .sheet {{ padding: 0; }} }}
  </style>
</head>
<body>
  <div class="toolbar"><button onclick="window.print()">Печать</button> <span>Листов: {len(sheets)}</span></div>
  {''.join(sheets)}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Warehouse settings (workshop -> warehouse bindings + ignored warehouses)
# Plan: "В журнале есть окно настроек: привязка участок -> склад получатель;
# список игнорируемых складов".
# ---------------------------------------------------------------------------


def _binding_payload(b: WorkshopWarehouseBinding) -> Dict[str, Any]:
    name = None
    try:
        if b.workshop:
            name = str(b.workshop.resource_name or "")
    except Exception:
        name = None
    return {
        "binding_id": int(b.binding_id),
        "workshop_id": int(b.workshop_id),
        "workshop_name": name,
        "warehouse_ref1c": str(b.warehouse_ref1c or ""),
    }


def _ignored_payload(row: IgnoredWarehouse) -> Dict[str, Any]:
    return {
        "warehouse_ref1c": str(row.warehouse_ref1c),
        "warehouse_name": str(row.warehouse_name or "") if row.warehouse_name else None,
        "reason": str(row.reason or "") if row.reason else None,
    }


def list_settings(db: Session) -> Dict[str, Any]:
    bindings = (
        db.query(WorkshopWarehouseBinding)
        .options(joinedload(WorkshopWarehouseBinding.workshop))
        .order_by(WorkshopWarehouseBinding.workshop_id.asc())
        .all()
    )
    ignored = (
        db.query(IgnoredWarehouse)
        .order_by(IgnoredWarehouse.warehouse_ref1c.asc())
        .all()
    )
    return {
        "workshop_warehouse_bindings": [_binding_payload(b) for b in bindings],
        "ignored_warehouses": [_ignored_payload(r) for r in ignored],
    }


def upsert_workshop_binding(db: Session, workshop_id: int, warehouse_ref1c: str) -> Dict[str, Any]:
    workshop_id_int = int(workshop_id)
    wh = str(warehouse_ref1c or "").strip()
    if not wh:
        raise ValueError("warehouse_ref1c is required")
    # Verify workshop exists
    workshop = db.query(ProductionResource).filter(ProductionResource.resource_id == workshop_id_int).first()
    if not workshop:
        raise ValueError(f"workshop_id={workshop_id_int}: участок не найден")
    binding = (
        db.query(WorkshopWarehouseBinding)
        .filter(WorkshopWarehouseBinding.workshop_id == workshop_id_int)
        .first()
    )
    if binding is None:
        binding = WorkshopWarehouseBinding(workshop_id=workshop_id_int, warehouse_ref1c=wh)
        db.add(binding)
    else:
        binding.warehouse_ref1c = wh
    db.commit()
    # Re-load with workshop for the response
    binding = (
        db.query(WorkshopWarehouseBinding)
        .options(joinedload(WorkshopWarehouseBinding.workshop))
        .filter(WorkshopWarehouseBinding.workshop_id == workshop_id_int)
        .first()
    )
    return _binding_payload(binding)


def delete_workshop_binding(db: Session, workshop_id: int) -> Dict[str, Any]:
    workshop_id_int = int(workshop_id)
    deleted = (
        db.query(WorkshopWarehouseBinding)
        .filter(WorkshopWarehouseBinding.workshop_id == workshop_id_int)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": int(deleted), "workshop_id": workshop_id_int}


def upsert_ignored_warehouse(
    db: Session,
    warehouse_ref1c: str,
    *,
    warehouse_name: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    wh = str(warehouse_ref1c or "").strip()
    if not wh:
        raise ValueError("warehouse_ref1c is required")
    row = db.query(IgnoredWarehouse).filter(IgnoredWarehouse.warehouse_ref1c == wh).first()
    if row is None:
        row = IgnoredWarehouse(
            warehouse_ref1c=wh,
            warehouse_name=warehouse_name or None,
            reason=reason or None,
        )
        db.add(row)
    else:
        if warehouse_name is not None:
            row.warehouse_name = warehouse_name or None
        if reason is not None:
            row.reason = reason or None
    db.commit()
    return _ignored_payload(row)


def delete_ignored_warehouse(db: Session, warehouse_ref1c: str) -> Dict[str, Any]:
    wh = str(warehouse_ref1c or "").strip()
    if not wh:
        raise ValueError("warehouse_ref1c is required")
    deleted = (
        db.query(IgnoredWarehouse)
        .filter(IgnoredWarehouse.warehouse_ref1c == wh)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"deleted": int(deleted), "warehouse_ref1c": wh}
