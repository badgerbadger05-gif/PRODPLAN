from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from ..models import (
    Item,
    PlannedOrder,
    PlannedPurchase,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    SupplierOrder,
    SupplierOrderItem,
)
from .supplier_order_status import state_counts_in_mrp as _supplier_order_counts_in_mrp
from .production_control_common import date_to_iso as _date_to_iso, to_float as _to_float
from .production_control_domain import (
    default_spec_id as _default_spec_id,
    ensure_state as _ensure_state,
    latest_run_id as _latest_run_id,
    unit_display as _unit_display,
)
from .planning_truth import require_accepted_truth
from .production_output_truth import (
    accepted_product_output,
    accepted_product_remaining_expr,
)


class MaterialCoverageSnapshotUnavailable(RuntimeError):
    def __init__(
        self,
        *,
        product_id: int,
        expected_generation_id: int,
        stored_generation_id: int | None,
    ) -> None:
        self.detail = {
            "code": "material_coverage_snapshot_unavailable",
            "status": "unavailable",
            "product_id": int(product_id),
            "expected_generation_id": int(expected_generation_id),
            "stored_generation_id": stored_generation_id,
            "reason": (
                "material coverage snapshot is missing or belongs to another "
                "Ledger generation; run explicit refresh"
            ),
        }
        super().__init__(self.detail["reason"])


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
    qty = _to_float(accepted_product_output(product).remaining_qty)
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


def _open_issue_reservations_by_item(db: Session, item_ids: Sequence[int]) -> Dict[int, float]:
    """
    Components held for production lines: kits in transit (draft..exported
    transfers) plus kits already delivered to workshop warehouses (posted
    transfers and local zero-distance claims) that production has not consumed yet.
    """
    from .production_material_custody import committed_material_by_item

    return committed_material_by_item(db, item_ids)


def _supplier_eta_by_item(db: Session, item_ids: Sequence[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Per-item list of expected arrivals from active supplier orders (учитываются
    только фазы «в пути» / «на складе», см. supplier_order_status.state_counts_in_mrp).
    Sorted by delivery_date ascending. Only deliveries with a positive remaining_qty
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
        if not _supplier_order_counts_in_mrp(state_name):
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


def _production_eta_by_item(db: Session, item_ids: Sequence[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Per-item expected arrivals from actual PRODPLAN production journal lines.

    PlannedOrder rows can disappear or be superseded once an MRP recommendation
    is materialised into production_products. The material card still needs to
    show those active orders as expected supply instead of "В заказах нет".
    """
    ids = [int(x) for x in item_ids if x is not None]
    if not ids:
        return {}

    from .production_control_journal import DONE_STATE_KEY, _TERMINAL_LINE_STATUSES

    remaining_expr = accepted_product_remaining_expr(
        ProductionProduct.quantity,
        ProductionProduct.produced_qty,
    )
    rows = (
        db.query(ProductionProduct, ProductionOrder, ProductionOrderLineState)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionProduct.item_id.in_(ids))
        .filter(ProductionOrder.deletion_mark == False)
        .filter(remaining_expr > 0)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(tuple(_TERMINAL_LINE_STATUSES)))
        .order_by(
            ProductionOrderLineState.planned_finish_date.asc().nulls_last(),
            ProductionOrder.order_date.asc(),
            ProductionOrder.order_number.asc(),
        )
        .all()
    )

    result: Dict[int, List[Dict[str, Any]]] = {}
    for product, order, state in rows:
        finish = state.planned_finish_date if state and state.planned_finish_date else None
        order_dt = order.order_date.date() if isinstance(order.order_date, datetime) else order.order_date
        result.setdefault(int(product.item_id), []).append(
            {
                "source": "production_order",
                "date": _date_to_iso(finish or order_dt),
                "qty": _to_float(accepted_product_output(product).remaining_qty),
                "ref": str(order.order_number or ""),
                "product_id": int(product.product_id),
                "order_id": int(order.order_id),
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


def _ui_coverage_status(label: str) -> str:
    return "ready" if label == "ok" else label


def _ui_coverage_label(label: str) -> str:
    return {
        "ok": "Обеспечен",
        "ready": "Обеспечен",
        "partial": "Частично",
        "shortage": "Дефицит",
    }.get(label, label)


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


def _store_material_coverage_status(
    state: ProductionOrderLineState,
    new_status: str,
    label: str,
    snapshot: Dict[str, Any],
    *,
    ledger_generation_id: int,
) -> None:
    """
    Persist material availability separately from the workflow state.

    A line can be "to_move" while current stock says "ready"; those meanings
    must not overwrite each other.
    """
    state.material_coverage_status = new_status
    state.material_coverage_label = label
    state.material_coverage_calculated_at = datetime.now(timezone.utc)
    state.material_coverage_ledger_generation_id = int(ledger_generation_id)
    state.material_coverage_snapshot = snapshot
    if (
        state.issue_status in {None, "", "not_requested"}
        and state.status in {"shortage", "partial", "ready"}
        and state.status != new_status
    ):
        state.status = new_status


def _reservation_orders_by_item(
    db: Session,
    reservation_state: Any,
    *,
    exclude_product_id: int,
) -> Dict[int, List[Dict[str, Any]]]:
    product_ids: List[int] = []
    for product_id, reservation in reservation_state.by_product.items():
        if int(product_id) == int(exclude_product_id):
            continue
        has_reservation = any(qty > 1e-9 for qty in reservation.in_transit.values()) or any(
            qty > 1e-9 for qty in reservation.at_workshop.values()
        )
        if has_reservation:
            product_ids.append(int(product_id))
    product_ids.sort()
    if not product_ids:
        return {}

    products = (
        db.query(ProductionProduct)
        .options(joinedload(ProductionProduct.order), joinedload(ProductionProduct.item))
        .filter(ProductionProduct.product_id.in_(product_ids))
        .all()
    )
    product_by_id = {int(product.product_id): product for product in products}
    result: Dict[int, List[Dict[str, Any]]] = {}
    for product_id in product_ids:
        product = product_by_id.get(product_id)
        reservation = reservation_state.by_product.get(product_id)
        if product is None or reservation is None:
            continue
        order = product.order
        display_number = str(getattr(order, "order_number", "") or "")
        for cid in sorted(set(reservation.in_transit) | set(reservation.at_workshop)):
            qty_total = reservation.total(cid)
            if qty_total <= 1e-9:
                continue
            result.setdefault(int(cid), []).append(
                {
                    "product_id": int(product.product_id),
                    "order_id": int(product.order_id),
                    "order_number": display_number,
                    "order_ref1c": str(getattr(order, "order_ref1c", "") or "") or None,
                    "item_name": str(getattr(product.item, "item_name", "") or ""),
                    "reserved_qty": qty_total,
                    "reserved_at_workshop_qty": reservation.at_workshop.get(cid, 0.0),
                    "reserved_in_transit_qty": reservation.in_transit.get(cid, 0.0),
                }
            )

    for rows in result.values():
        rows.sort(key=lambda row: (-_to_float(row.get("reserved_qty")), str(row.get("order_number") or "")))
    return result


def preview_materials(db: Session, product_id: int, *, refresh_state: bool = False) -> Dict[str, Any]:
    """
    Return the BOM components required for a production line plus per-component
    availability and ETA. Status persistence is deliberately opt-in: the UI
    preview must not rewrite the journal while the user browses rows.

    Per-component fields:
      required_qty   вЂ” needed for this order line
      available_qty  вЂ” signed stock minus open material-issue reservations
      missing_qty    вЂ” max(0, required - available)
      coverage       вЂ” 'ok' | 'partial' | 'shortage'
      eta_dates      вЂ” chronological list of {source, date, qty, ref} from
                       active supplier orders and the latest planning run,
                       only populated when coverage is not 'ok'

    Order-level field `coverage` aggregates per-component labels per the plan
    rules (any shortage -> shortage, else any partial -> partial, else ready).
    """
    truth = require_accepted_truth(db, "production_control.material_coverage")
    ledger_generation_id = int(truth.generation_id)
    product = (
        db.query(ProductionProduct)
        .options(
            joinedload(ProductionProduct.order),
            joinedload(ProductionProduct.item),
            joinedload(ProductionProduct.control_state),
        )
        .filter(ProductionProduct.product_id == int(product_id))
        .first()
    )
    if not product:
        raise ValueError("Строка заказа не найдена")
    spec_id, components = _components_for_product(db, product)

    comp_ids = [int(c["component_item_id"]) for c in components]
    from .item_ledger import item_ledger_position

    ledger_positions = item_ledger_position(
        db,
        comp_ids,
        ledger_generation_id=ledger_generation_id,
    )
    stock_by_item = {
        item_id: float(position["on_hand"])
        for item_id, position in ledger_positions.items()
    }

    from .production_material_custody import load_material_custody

    reservation_state = load_material_custody(db, item_ids=comp_ids)
    # Components held by OTHER lines are unavailable; components this line
    # already holds (in transit or delivered to its workshop) count as its own
    # coverage instead of re-entering the pool.
    reservations = reservation_state.total_by_item(
        exclude_product_id=int(product.product_id)
    )
    own_reservation = reservation_state.for_product(int(product.product_id))
    reserved_orders_by_item = _reservation_orders_by_item(
        db,
        reservation_state,
        exclude_product_id=int(product.product_id),
    )
    run_id = _latest_run_id(db)
    supplier_eta = _supplier_eta_by_item(db, comp_ids)
    production_eta = _production_eta_by_item(db, comp_ids)
    planned_eta = _planned_eta_by_item(db, comp_ids, run_id)
    state = product.control_state
    require_reserved_at_workshop = bool(
        state
        and (
            str(state.issue_status or "") == "posted"
            or str(state.status or "") in {"assembled", "in_progress", "done", "produced_partial", "produced"}
        )
    )

    for comp in components:
        iid = int(comp["component_item_id"])
        required = _to_float(comp["required_qty"])
        raw_stock = stock_by_item.get(iid, 0.0)
        reserved = reservations.get(iid, 0.0)
        own_reserved = own_reservation.total(iid)
        available = raw_stock - reserved - own_reserved
        own_at_workshop = own_reservation.at_workshop.get(iid, 0.0)
        own_in_transit = own_reservation.in_transit.get(iid, 0.0)
        covering = own_at_workshop if require_reserved_at_workshop else available + own_reserved
        missing = max(0.0, required - covering)
        label = _component_coverage_label(required, covering)
        comp["available_qty"] = available
        comp["stock_qty"] = raw_stock
        comp["reserved_qty"] = reserved
        comp["reserved_for_order_qty"] = own_reserved
        comp["reserved_at_workshop_qty"] = own_at_workshop
        comp["reserved_in_transit_qty"] = own_in_transit
        comp["reserved_orders"] = reserved_orders_by_item.get(iid, [])
        comp["missing_qty"] = missing
        comp["coverage"] = label
        comp["availability_status"] = _ui_coverage_status(label)
        comp["coverage_status"] = _ui_coverage_status(label)
        comp["coverage_label"] = _ui_coverage_label(label)
        if ledger_positions:
            pos = ledger_positions.get(iid)
            if pos is not None:
                comp["ledger_available"] = pos["available"]
                comp["ledger_projected"] = pos["projected"]
                comp["ledger_uncovered"] = pos["uncovered"]
        if label == "ok":
            comp["eta_dates"] = []
            comp["expected_dates"] = []
        else:
            etas: List[Dict[str, Any]] = (
                list(supplier_eta.get(iid, []))
                + list(production_eta.get(iid, []))
                + list(planned_eta.get(iid, []))
            )
            etas.sort(key=lambda e: e.get("date") or "")
            comp["eta_dates"] = etas
            comp["expected_dates"] = [
                {
                    "source": eta.get("source"),
                    "date": eta.get("date"),
                    "qty": eta.get("qty"),
                    "order_number": eta.get("ref"),
                    "ref": eta.get("ref"),
                }
                for eta in etas
            ]

    order_coverage = _aggregate_coverage([str(c["coverage"]) for c in components])

    payload = {
        "ledger_generation_id": ledger_generation_id,
        "product_id": int(product.product_id),
        "order_number": str(product.order.order_number or ""),
        "item_name": str(product.item.item_name or ""),
        "item_article": str(product.item.item_article or ""),
        "qty": _to_float(accepted_product_output(product).remaining_qty),
        "spec_id": spec_id,
        "components": components,
        "coverage": order_coverage,
        "coverage_status": order_coverage,
        "coverage_label": _ui_coverage_label(order_coverage),
    }
    if refresh_state:
        state = _ensure_state(db, product)
        _store_material_coverage_status(
            state,
            order_coverage,
            _ui_coverage_label(order_coverage),
            payload,
            ledger_generation_id=ledger_generation_id,
        )
        db.commit()
    return payload


def get_materials_snapshot(db: Session, product_id: int) -> Dict[str, Any]:
    """Read only coverage persisted for the currently accepted generation."""
    truth = require_accepted_truth(db, "production_control.material_coverage")
    generation_id = int(truth.generation_id)
    state = (
        db.query(ProductionOrderLineState)
        .filter(ProductionOrderLineState.product_id == int(product_id))
        .first()
    )
    snapshot = state.material_coverage_snapshot if state else None
    snapshot_generation_id = (
        int(state.material_coverage_ledger_generation_id)
        if state is not None
        and state.material_coverage_ledger_generation_id is not None
        else None
    )
    payload_generation_id = (
        int(snapshot.get("ledger_generation_id"))
        if isinstance(snapshot, dict)
        and snapshot.get("ledger_generation_id") is not None
        else None
    )
    if (
        isinstance(snapshot, dict)
        and snapshot_generation_id == generation_id
        and payload_generation_id == generation_id
    ):
        return dict(snapshot)
    raise MaterialCoverageSnapshotUnavailable(
        product_id=int(product_id),
        expected_generation_id=generation_id,
        stored_generation_id=snapshot_generation_id,
    )


def refresh_materials_snapshot(db: Session, product_id: int) -> Dict[str, Any]:
    """Explicit mutation path used by POST/background workers."""
    return preview_materials(db, int(product_id), refresh_state=True)


def _active_product_ids(db: Session, *, limit: int = 0) -> List[int]:
    from sqlalchemy import or_

    from ..models import ProductionOrder
    from .production_control_journal import DONE_STATE_KEY, _TERMINAL_LINE_STATUSES

    remaining_expr = accepted_product_remaining_expr(
        ProductionProduct.quantity,
        ProductionProduct.produced_qty,
    )
    query = (
        db.query(ProductionProduct.product_id)
        .join(ProductionOrder, ProductionOrder.order_id == ProductionProduct.order_id)
        .outerjoin(ProductionOrderLineState, ProductionOrderLineState.product_id == ProductionProduct.product_id)
        .filter(ProductionOrder.deletion_mark == False)
        .filter(or_(ProductionOrder.order_state_key.is_(None), func.lower(ProductionOrder.order_state_key) != DONE_STATE_KEY))
        .filter(remaining_expr > 0)
        .filter(func.coalesce(ProductionOrderLineState.status, "shortage").notin_(tuple(_TERMINAL_LINE_STATUSES)))
        .order_by(ProductionOrder.order_date.desc(), ProductionOrder.order_number.asc(), ProductionProduct.line_number.asc())
    )
    if limit:
        query = query.limit(max(0, int(limit)))
    return [int(row[0]) for row in query.all()]


def recalculate_production_coverage(db: Session, *, limit: int = 0) -> Dict[str, Any]:
    from collections import Counter

    statuses: Counter[str] = Counter()
    errors: List[Dict[str, Any]] = []
    processed = 0
    for product_id in _active_product_ids(db, limit=limit):
        try:
            result = preview_materials(db, product_id, refresh_state=True)
            statuses[str(result.get("coverage_status") or "unknown")] += 1
            processed += 1
        except Exception as exc:  # pragma: no cover - operational safety net
            db.rollback()
            errors.append({"product_id": product_id, "error": str(exc)})
    return {
        "status": "ok" if not errors else "partial",
        "processed": processed,
        "errors": len(errors),
        "coverage": dict(statuses),
        "sample_errors": errors[:20],
    }
