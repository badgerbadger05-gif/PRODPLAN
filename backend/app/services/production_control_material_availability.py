from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from ..models import (
    Item,
    LedgerFutureSupply,
    ProductionOrder,
    ProductionOrderLineState,
    ProductionProduct,
    SpecComponent,
)
from .production_control_common import (
    date_to_iso as _date_to_iso,
    to_float as _to_float,
    to_float_strict as _to_float_strict,
)
from .production_control_domain import (
    default_spec_id as _default_spec_id,
    unit_display as _unit_display,
)
from .planning_truth import (
    CAPABILITY_FUTURE_SUPPLY,
    CAPABILITY_PHYSICAL_LEDGER,
    require_accepted_truth,
)
from .production_output_truth import (
    accepted_product_output,
    accepted_product_remaining_expr,
)
from .production_material_custody_projection import (
    MaterialCustodySnapshotUnavailable,
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
                "Ledger generation; publish a complete Ledger generation"
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
    qty = _to_float_strict(accepted_product_output(product).remaining_qty, field="production_output.remaining_qty")
    components: List[Dict[str, Any]] = []
    for comp, item in rows:
        required = _to_float_strict(comp.quantity, field="spec_component.quantity") * qty
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


def _future_supply_eta_by_item(
    db: Session,
    item_ids: Sequence[int],
    *,
    ledger_generation_id: Optional[int],
) -> Dict[int, List[Dict[str, Any]]]:
    """
    Per-item future ETA from one Ledger generation's ``ledger_future_supply``
    rows.

    Only exact rows with positive open quantity and ETA date are included.
    """
    ids = [int(x) for x in item_ids if x is not None]
    if not ids or ledger_generation_id is None:
        return {}

    rows = (
        db.query(
            LedgerFutureSupply.item_id,
            LedgerFutureSupply.eta_date,
            LedgerFutureSupply.open_qty_at_cutoff,
            LedgerFutureSupply.supply_kind,
            LedgerFutureSupply.source_ref,
        )
        .filter(LedgerFutureSupply.ledger_generation_id == int(ledger_generation_id))
        .filter(LedgerFutureSupply.item_id.in_(ids))
        .filter(LedgerFutureSupply.evidence_status == "exact")
        .filter(LedgerFutureSupply.open_qty_at_cutoff > 0)
        .filter(LedgerFutureSupply.eta_date.isnot(None))
        .filter(LedgerFutureSupply.supply_kind.in_(("supplier_order", "wip_order")))
        .order_by(
            LedgerFutureSupply.eta_date.asc().nulls_last(),
            LedgerFutureSupply.source_ref.asc().nulls_last(),
        )
        .all()
    )

    result: Dict[int, List[Dict[str, Any]]] = {}
    for iid, eta_date, open_qty, supply_kind, source_ref in rows:
        result.setdefault(int(iid), []).append(
            {
                "source": str(supply_kind),
                "date": _date_to_iso(eta_date),
                "qty": _to_float_strict(open_qty, field="ledger_future_supply.open_qty_at_cutoff"),
                "ref": str(source_ref or ""),
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


def preview_materials(
    db: Session,
    product_id: int,
    *,
    ledger_generation_id: int | None = None,
    _product_override: ProductionProduct | None = None,
) -> Dict[str, Any]:
    """
    Return the BOM components required for a production line plus per-component
    availability and ETA. This is a pure candidate-builder calculation: it
    never mutates workflow state and can be pinned to a BUILDING generation.

    Per-component fields:
      required_qty   вЂ” needed for this order line
      available_qty  вЂ” signed stock minus open material-issue reservations
      missing_qty    вЂ” max(0, required - available)
      coverage       вЂ” 'ok' | 'partial' | 'shortage'
      eta_dates      вЂ” chronological list of {source, date, qty, ref} from
                       LedgerFutureSupply rows for the selected Ledger generation,
                       only populated when coverage is not 'ok'

    Order-level field `coverage` aggregates per-component labels per the plan
    rules (any shortage -> shortage, else any partial -> partial, else ready).
    """
    allow_building_read = ledger_generation_id is not None
    if ledger_generation_id is None:
        truth = require_accepted_truth(
            db,
            "production_control.material_coverage",
            required_capabilities=(
                CAPABILITY_PHYSICAL_LEDGER,
                CAPABILITY_FUTURE_SUPPLY,
            ),
        )
        ledger_generation_id = int(truth.generation_id)
    else:
        ledger_generation_id = int(ledger_generation_id)
    product = _product_override or (
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
        allow_building_read=allow_building_read,
    )
    stock_by_item = {
        item_id: _to_float_strict(position["on_hand"], field="item_ledger_position.on_hand")
        for item_id, position in ledger_positions.items()
    }

    from .production_material_custody_projection import load_material_custody_projection

    reservation_state = load_material_custody_projection(
        db,
        ledger_generation_id=int(ledger_generation_id),
    )
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
    future_supply_eta = _future_supply_eta_by_item(
        db,
        comp_ids,
        ledger_generation_id=ledger_generation_id,
    )
    use_projection_coverage = bool(comp_ids)
    live_require_reserved_at_workshop = False

    for comp in components:
        iid = int(comp["component_item_id"])
        required = _to_float_strict(comp["required_qty"], field="component.required_qty")
        raw_stock = stock_by_item.get(iid, 0.0)
        reserved = reservations.get(iid, 0.0)
        own_reserved = own_reservation.total(iid)
        available = raw_stock - reserved - own_reserved
        own_at_workshop = own_reservation.at_workshop.get(iid, 0.0)
        own_in_transit = own_reservation.in_transit.get(iid, 0.0)
        require_reserved_at_workshop = (
            own_at_workshop > 1e-9
            if use_projection_coverage
            else live_require_reserved_at_workshop
        )
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
            etas: List[Dict[str, Any]] = list(future_supply_eta.get(iid, []))
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
        "qty": _to_float_strict(
            accepted_product_output(product).remaining_qty,
            field="production_output.remaining_qty",
        ),
        "spec_id": spec_id,
        "components": components,
        "coverage": order_coverage,
        "coverage_status": order_coverage,
        "coverage_label": _ui_coverage_label(order_coverage),
    }
    return payload


def preview_make_work_item_materials(
    db: Session,
    *,
    work_item_id: int,
    item_id: int,
    quantity: float,
    spec_id: int | None,
    ledger_generation_id: int,
    order_number: str,
) -> Dict[str, Any]:
    """Preview one saved MAKE obligation without creating an executor order."""
    item = db.get(Item, int(item_id))
    if item is None:
        raise ValueError("Номенклатура расчётной строки не найдена")
    preview_product = ProductionProduct(
        product_id=-int(work_item_id), order_id=-int(work_item_id), item_id=int(item_id),
        quantity=float(quantity), produced_qty=0, remaining_qty=float(quantity),
        spec_id=int(spec_id) if spec_id is not None else None,
    )
    preview_product.item = item
    preview_product.order = ProductionOrder(order_number=str(order_number or ""))
    payload = preview_materials(
        db, -int(work_item_id), ledger_generation_id=int(ledger_generation_id),
        _product_override=preview_product,
    )
    payload["work_item_id"] = int(work_item_id)
    payload["product_id"] = None
    return payload


def preview_make_work_items_coverage(
    db: Session,
    rows: Sequence[Mapping[str, Any]],
    *,
    ledger_generation_id: int,
) -> Dict[int, Dict[str, str]]:
    """Calculate proposal material coverage in bulk for one Ledger generation.

    The production journal may contain thousands of unmaterialized MAKE rows.
    Calling :func:`preview_make_work_item_materials` once per row would reload
    the same Ledger positions and custody projection thousands of times.  This
    helper keeps the exact same coverage formula, but loads every specification,
    component position and custody hold once for the whole candidate.
    """
    proposals = [
        row for row in rows
        if row.get("work_item_id") is not None and row.get("spec_id") is not None
    ]
    if not proposals:
        return {}

    spec_ids = sorted({int(row["spec_id"]) for row in proposals})
    component_rows = (
        db.query(
            SpecComponent.spec_id,
            SpecComponent.item_id,
            SpecComponent.quantity,
        )
        .filter(SpecComponent.spec_id.in_(spec_ids))
        .order_by(SpecComponent.spec_id.asc(), SpecComponent.component_id.asc())
        .all()
    )
    components_by_spec: Dict[int, List[Tuple[int, float]]] = {}
    component_ids: set[int] = set()
    for spec_id, item_id, quantity in component_rows:
        component_id = int(item_id)
        components_by_spec.setdefault(int(spec_id), []).append(
            (component_id, _to_float_strict(quantity, field="spec_component.quantity"))
        )
        component_ids.add(component_id)

    from .item_ledger import item_ledger_position
    from .production_material_custody_projection import load_material_custody_projection

    positions = item_ledger_position(
        db,
        sorted(component_ids),
        ledger_generation_id=int(ledger_generation_id),
        allow_building_read=True,
    )
    custody = load_material_custody_projection(
        db,
        ledger_generation_id=int(ledger_generation_id),
    )
    reserved_by_item = custody.total_by_item()

    result: Dict[int, Dict[str, str]] = {}
    for row in proposals:
        labels: List[str] = []
        quantity = _to_float_strict(row.get("quantity"), field="proposal.quantity")
        for component_id, qty_per_unit in components_by_spec.get(int(row["spec_id"]), []):
            required = qty_per_unit * quantity
            on_hand = _to_float_strict(
                positions.get(component_id, {}).get("on_hand", 0.0),
                field="item_ledger_position.on_hand",
            )
            available = on_hand - _to_float(reserved_by_item.get(component_id))
            labels.append(_component_coverage_label(required, available))
        coverage = _aggregate_coverage(labels)
        result[int(row["work_item_id"])] = {
            "coverage_status": coverage,
            "coverage_label": _ui_coverage_label(coverage),
        }
    return result


def get_materials_snapshot(db: Session, product_id: int) -> Dict[str, Any]:
    """Read coverage only from the accepted production-journal snapshot."""
    from .. import models
    from .planning_truth import get_latest_read_snapshot
    from .production_control_journal_snapshot import CONSUMER, SNAPSHOT_KEY

    truth = require_accepted_truth(
        db,
        "production_control.material_coverage",
        required_capabilities=(
            CAPABILITY_PHYSICAL_LEDGER,
            CAPABILITY_FUTURE_SUPPLY,
        ),
    )
    generation_id = int(truth.generation_id)
    snapshot = get_latest_read_snapshot(
        db, consumer=CONSUMER, snapshot_key=SNAPSHOT_KEY,
    )
    row = None
    if snapshot is not None:
        row = (
            db.query(models.PlanningReadRow)
            .filter(
                models.PlanningReadRow.snapshot_id == int(snapshot.id),
                models.PlanningReadRow.row_key == f"product:{int(product_id)}",
            )
            .one_or_none()
        )
    material = row.payload.get("material_coverage_snapshot") if row and isinstance(row.payload, dict) else None
    if isinstance(material, dict) and int(material.get("ledger_generation_id") or -1) == generation_id:
        public = dict(material)
        public["truth_status"] = str(snapshot.truth_status)
        public["cutoff"] = snapshot.cutoff.isoformat()
        return public
    raise MaterialCoverageSnapshotUnavailable(
        product_id=int(product_id),
        expected_generation_id=generation_id,
        stored_generation_id=(int(snapshot.ledger_generation_id) if snapshot is not None else None),
    )


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
