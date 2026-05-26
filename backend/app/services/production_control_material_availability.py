from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ..models import (
    IgnoredWarehouse,
    Item,
    ItemWarehouseStock,
    PlannedOrder,
    PlannedPurchase,
    ProductionMaterialIssue,
    ProductionMaterialIssueLine,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
    SupplierOrder,
    SupplierOrderItem,
)
from .planning_service import (
    SUPPLIER_ORDER_EXCLUDED_STATE_NAMES,
    _normalize_supplier_order_state_name,
)
from .production_control_common import date_to_iso as _date_to_iso, to_float as _to_float
from .production_control_domain import (
    default_spec_id as _default_spec_id,
    ensure_state as _ensure_state,
    latest_run_id as _latest_run_id,
    unit_display as _unit_display,
)


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
       considered "covered by the breakdown" вЂ” if all of their stock is in
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


def _maybe_bump_coverage_status(state: ProductionOrderLineState, new_status: str) -> None:
    """
    Auto-refresh the line status only while it sits in one of the coverage-
    evaluation states. Once the user / system moves past (to_move, assembled,
    produced_*, cancelled), the status is sticky and we don't override it.
    """
    if state.status in {"shortage", "partial", "ready"} and state.status != new_status:
        state.status = new_status


def preview_materials(db: Session, product_id: int, *, refresh_state: bool = False) -> Dict[str, Any]:
    """
    Return the BOM components required for a production line plus per-component
    availability and ETA. Status persistence is deliberately opt-in: the UI
    preview must not rewrite the journal while the user browses rows.

    Per-component fields:
      required_qty   вЂ” needed for this order line
      available_qty  вЂ” items.stock_qty minus open material-issue reservations,
                       clamped to >=0
      missing_qty    вЂ” max(0, required - available)
      coverage       вЂ” 'ok' | 'partial' | 'shortage'
      eta_dates      вЂ” chronological list of {source, date, qty, ref} from
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
        comp["availability_status"] = _ui_coverage_status(label)
        comp["coverage_status"] = _ui_coverage_status(label)
        comp["coverage_label"] = _ui_coverage_label(label)
        if label == "ok":
            comp["eta_dates"] = []
            comp["expected_dates"] = []
        else:
            etas: List[Dict[str, Any]] = list(supplier_eta.get(iid, [])) + list(planned_eta.get(iid, []))
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

    if refresh_state:
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
        "coverage_status": order_coverage,
        "coverage_label": _ui_coverage_label(order_coverage),
    }
